"""Telegram live progress reporting, render hash deduplication, and edit throttling (SF-019)."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import html
import logging
from typing import Any, Callable, Dict, Optional
from sqlalchemy import select
from sqlalchemy.orm import Session

from stockforge.db.models import TelegramProgressMessage, utc_now
from stockforge.queue.tracker import BatchProgressSummary

logger = logging.getLogger("stockforge.bot.progress")


def compute_render_hash(text: str) -> str:
    """Compute deterministic SHA-256 hash of rendered message content."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def format_progress_message(
    summary: BatchProgressSummary,
    job_type: str = "image_batch",
    provider: Optional[str] = None,
    model: Optional[str] = None,
    estimated_cost_cents: Optional[int] = None,
    eta_seconds: Optional[float] = None,
) -> str:
    """Format safe and strictly accurate HTML progress message."""
    status_icon = "⏳"
    status_label = "Memproses"
    if summary.is_terminal:
        if summary.failed_count > 0:
            status_icon = "⚠️"
            status_label = "Selesai Sebagian (Ada Kegagalan)"
        elif summary.cancelled_count > 0:
            status_icon = "🛑"
            status_label = "Dibatalkan"
        else:
            status_icon = "✅"
            status_label = "Selesai Sempurna"

    current_idx_str = f"{summary.current_item_index}/{summary.total_items}" if summary.current_item_index else f"-/{summary.total_items}"
    current_stage_str = summary.current_stage.upper() if summary.current_stage else "IDLE"

    # Progress bar representation
    filled = int(summary.success_rate_percent / 10)
    bar = "█" * filled + "░" * (10 - filled)

    lines = [
        f"{status_icon} <b>Progress Batch #{summary.job_id} ({html.escape(job_type)})</b>",
        f"<b>Status:</b> {html.escape(status_label)}",
        f"<b>Kemajuan:</b> <code>[{bar}]</code> {summary.success_rate_percent:.1f}%",
        "",
        f"• <b>Gambar Aktif:</b> <code>{html.escape(current_idx_str)}</code> (Tahap: <code>{html.escape(current_stage_str)}</code>)",
        f"• <b>Berhasil:</b> <code>{summary.completed_count}</code> / {summary.total_items}",
        f"• <b>Sedang Proses:</b> <code>{summary.processing_count}</code>",
        f"• <b>Menunggu:</b> <code>{summary.pending_count}</code>",
        f"• <b>Gagal:</b> <code>{summary.failed_count}</code>",
    ]

    if summary.cancelled_count > 0:
        lines.append(f"• <b>Dibatalkan:</b> <code>{summary.cancelled_count}</code>")
    if summary.reconciliation_count > 0:
        lines.append(f"• <b>Butuh Rekonsiliasi:</b> <code>{summary.reconciliation_count}</code>")

    # Provider and model info
    if provider:
        p_str = provider
        if model:
            p_str += f" ({model})"
        lines.append(f"• <b>Engine:</b> <code>{html.escape(p_str)}</code>")

    # Cost and ETA only if derived from real data
    if estimated_cost_cents is not None:
        cost_usd = estimated_cost_cents / 100.0
        lines.append(f"• <b>Biaya Komitmen:</b> <code>${cost_usd:.2f} USD</code>")

    if eta_seconds is not None and not summary.is_terminal:
        mins, secs = divmod(int(eta_seconds), 60)
        lines.append(f"• <b>Estimasi Selesai:</b> ~{mins}m {secs}s")

    return "\n".join(lines)


class TelegramProgressManager:
    """Manages single-message-per-batch progress edits with render hashing and throttling."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        throttle_seconds: float = 5.0,
    ) -> None:
        self.session_factory = session_factory
        self.throttle_seconds = throttle_seconds

    async def update_progress(
        self,
        bot: Any,
        chat_id: int,
        summary: BatchProgressSummary,
        job_type: str = "image_batch",
        provider: Optional[str] = None,
        model: Optional[str] = None,
        estimated_cost_cents: Optional[int] = None,
        eta_seconds: Optional[float] = None,
        force: bool = False,
    ) -> Optional[int]:
        """Update or send single progress message per batch with render hash and throttling."""
        text = format_progress_message(
            summary=summary,
            job_type=job_type,
            provider=provider,
            model=model,
            estimated_cost_cents=estimated_cost_cents,
            eta_seconds=eta_seconds,
        )
        current_hash = compute_render_hash(text)
        now = utc_now()

        with self.session_factory() as session:
            record = session.get(TelegramProgressMessage, summary.job_id)

            if record:
                # 1. Skip if text has not changed
                if record.last_render_hash == current_hash:
                    logger.debug("Progress message text tidak berubah (hash match), lewati edit")
                    return record.message_id

                # 2. Skip if throttled and not force or terminal
                rec_time = record.last_updated_at
                if rec_time.tzinfo is None:
                    rec_time = rec_time.replace(tzinfo=timezone.utc)
                elapsed = (now - rec_time).total_seconds()
                if not force and not summary.is_terminal and elapsed < self.throttle_seconds:
                    logger.debug("Progress message throttled (elapsed %.1fs < %.1fs)", elapsed, self.throttle_seconds)
                    return record.message_id

                # 3. Attempt editMessageText
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=record.message_id,
                        text=text,
                        parse_mode="HTML",
                    )
                    record.last_render_hash = current_hash
                    record.last_updated_at = now
                    session.commit()
                    return record.message_id
                except Exception as e:
                    err_msg = str(e).lower()
                    if "not modified" in err_msg:
                        # Harmless: content already matches
                        return record.message_id
                    logger.warning("Gagal mengedit pesan progress #%s: %s. Mengirim pesan baru...", record.message_id, e)
                    # Message likely deleted by user, fall through to create a new one

            # 4. Send new message and persist record
            try:
                sent = await bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode="HTML",
                )
                new_msg_id = sent.message_id

                if record:
                    record.message_id = new_msg_id
                    record.chat_id = chat_id
                    record.last_render_hash = current_hash
                    record.last_updated_at = now
                else:
                    new_record = TelegramProgressMessage(
                        job_id=summary.job_id,
                        chat_id=chat_id,
                        message_id=new_msg_id,
                        last_render_hash=current_hash,
                        last_updated_at=now,
                    )
                    session.add(new_record)
                session.commit()
                return new_msg_id
            except Exception as e:
                logger.error("Gagal mengirim pesan progress baru: %s", e)
                return None
