"""Command handlers for /status, /failed, /retry, and /providers (SF-019)."""
from __future__ import annotations

import html
import logging
from typing import Callable, Optional
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from sqlalchemy import select
from sqlalchemy.orm import Session

from stockforge.bot.auth import require_admin
from stockforge.bot.progress import format_progress_message
from stockforge.db.models import Job, JobItem
from stockforge.providers.health import ProviderHealthTracker, default_health_tracker
from stockforge.providers.router import ProviderRouter
from stockforge.queue.tracker import ItemProgressTracker

logger = logging.getLogger("stockforge.bot.progress_handlers")


def parse_job_id_from_args(context: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    """Extract integer batch/job ID from command args."""
    if context.args and len(context.args) > 0:
        try:
            return int(context.args[0])
        except (ValueError, TypeError):
            return None
    return None


@require_admin
async def status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session_factory: Callable[[], Session],
) -> None:
    """Handle /status <batch_id> showing live progress counters and stage."""
    job_id = parse_job_id_from_args(context)
    if job_id is None:
        # Default to latest job
        with session_factory() as session:
            latest = session.scalar(select(Job).order_by(Job.id.desc()).limit(1))
            if latest:
                job_id = latest.id

    if job_id is None:
        if update.effective_message:
            await update.effective_message.reply_text("Tidak ada batch aktif atau ditemukan.")
        return

    tracker = ItemProgressTracker(session_factory)
    progress = tracker.get_batch_progress(job_id)

    with session_factory() as session:
        job = session.get(Job, job_id)
        job_type = job.type if job else "image_batch"
        cost_cents = job.reserved_usd_cents if job else 0

    text = format_progress_message(
        summary=progress,
        job_type=job_type,
        estimated_cost_cents=cost_cents,
    )

    if update.effective_message:
        sent = await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)
        if sent and update.effective_chat and not progress.is_terminal:
            try:
                from stockforge.bot.progress import compute_render_hash
                from stockforge.db.models import TelegramProgressMessage, utc_now
                with session_factory() as session:
                    rec = session.get(TelegramProgressMessage, job_id)
                    if rec:
                        rec.chat_id = update.effective_chat.id
                        rec.message_id = sent.message_id
                        rec.last_render_hash = compute_render_hash(text)
                        rec.last_updated_at = utc_now()
                    else:
                        new_rec = TelegramProgressMessage(
                            job_id=job_id,
                            chat_id=update.effective_chat.id,
                            message_id=sent.message_id,
                            last_render_hash=compute_render_hash(text),
                            last_updated_at=utc_now(),
                        )
                        session.add(new_rec)
                    session.commit()
            except Exception as ex:
                logger.warning("Gagal mendaftarkan TelegramProgressMessage untuk /status: %s", ex)


@require_admin
async def failed_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session_factory: Callable[[], Session],
) -> None:
    """Handle /failed <batch_id> displaying diagnostic failure reports for a batch."""
    job_id = parse_job_id_from_args(context)
    if job_id is None:
        if update.effective_message:
            await update.effective_message.reply_text("Gunakan format: `/failed <batch_id>`", parse_mode=ParseMode.MARKDOWN)
        return

    with session_factory() as session:
        failed_items = session.scalars(
            select(JobItem).where(JobItem.job_id == job_id, JobItem.status == "failed").order_by(JobItem.item_index)
        ).all()

        if not failed_items:
            if update.effective_message:
                await update.effective_message.reply_text(f"✅ Tidak ada item yang gagal pada Batch #{job_id}.")
            return

        lines = [f"⚠️ <b>Laporan Kegagalan Batch #{job_id} ({len(failed_items)} item gagal):</b>\n"]
        for it in failed_items:
            prov = it.provider or "unknown"
            err = it.last_error or "Unknown error"
            lines.append(
                f"• <b>Gambar #{it.item_index}</b> (Tahap: <code>{it.stage}</code> | Engine: <code>{html.escape(prov)}</code>)\n"
                f"  └ <i>Galat:</i> <code>{html.escape(err[:150])}</code>"
            )

        lines.append(f"\n💡 Ketik <code>/retry {job_id}</code> untuk mengulang item-item yang gagal di atas.")
        text = "\n".join(lines)

    if update.effective_message:
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


@require_admin
async def jobs_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session_factory: Callable[[], Session],
) -> None:
    """Handle /jobs command displaying queue status and recent batch history."""
    STATUS_ICONS = {
        "running": "⏳",
        "reserved": "⏳",
        "pending": "🕒",
        "completed": "✅",
        "completed_with_errors": "⚠️",
        "failed": "❌",
        "cancelled": "🛑",
        "needs_reconciliation": "🔄",
    }

    try:
        from sqlalchemy.orm import selectinload

        with session_factory() as session:
            stmt = select(Job).options(selectinload(Job.items)).order_by(Job.id.desc()).limit(10)
            jobs = list(session.scalars(stmt).all())
            if not jobs:
                if update.effective_message:
                    await update.effective_message.reply_text(
                        "📭 Belum ada antrean pekerjaan atau batch yang terdaftar.\n\n"
                        "Gunakan /research untuk memulai riset dan produksi gambar baru."
                    )
                return

            active_count = sum(1 for j in jobs if j.status in ("running", "reserved", "pending"))

            lines = [
                "📋 <b>Daftar Pekerjaan & Antrean Batch</b>",
                f"<i>Menampilkan {len(jobs)} batch terakhir ({active_count} aktif)</i>\n",
            ]

            for j in jobs:
                icon = STATUS_ICONS.get(j.status, "📦")
                created_str = j.created_at.strftime("%Y-%m-%d %H:%M") if j.created_at else "-"
                total_items = len(j.items) if j.items else 1
                completed_items = sum(1 for it in j.items if it.status == "completed") if j.items else (1 if j.status == "completed" else 0)
                failed_items = sum(1 for it in j.items if it.status == "failed") if j.items else (1 if j.status == "failed" else 0)

                line = (
                    f"{icon} <b>Batch #{j.id}</b> (<code>{html.escape(j.type)}</code>)\n"
                    f"   └ Status: <code>{html.escape(j.status.upper())}</code> | "
                    f"Item: {completed_items}/{total_items} sukses"
                )
                if failed_items > 0:
                    line += f" ({failed_items} gagal)"
                line += f" | {created_str}"
                lines.append(line)

            lines.append(
                "\n💡 <i>Gunakan <code>/status &lt;id&gt;</code> untuk memantau kemajuan batch atau <code>/review &lt;id&gt;</code> untuk meninjau aset.</i>"
            )
            text = "\n".join(lines)

        if update.effective_message:
            await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)
    except Exception as ex:
        logger.error("Terjadi galat pada jobs_command: %s", ex, exc_info=True)
        if update.effective_message:
            await update.effective_message.reply_text(
                f"❌ <b>Gagal mengambil daftar antrean (/jobs):</b>\n<code>{html.escape(str(ex))}</code>",
                parse_mode=ParseMode.HTML,
            )


@require_admin
async def retry_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session_factory: Callable[[], Session],
    estimated_cost_per_item_cents: int = 4,
) -> None:
    """Handle /retry <batch_id> requiring explicit confirmation and spend approval."""
    job_id = parse_job_id_from_args(context)
    if job_id is None:
        if update.effective_message:
            await update.effective_message.reply_text("Gunakan format: `/retry <batch_id>`", parse_mode=ParseMode.MARKDOWN)
        return

    with session_factory() as session:
        failed_count = session.query(JobItem).filter_by(job_id=job_id, status="failed").count()
        if failed_count == 0:
            if update.effective_message:
                await update.effective_message.reply_text(
                    f"ℹ️ Tidak ada item gagal pada Batch #{job_id}. Semua item berhasil atau belum dieksekusi."
                )
            return

        total_cost_cents = failed_count * estimated_cost_per_item_cents
        cost_usd = total_cost_cents / 100.0

    keyboard = [
        [
            InlineKeyboardButton(
                f"✅ Konfirmasi Retry (${cost_usd:.2f} USD)",
                callback_data=f"retry_confirm:{job_id}",
            ),
            InlineKeyboardButton("❌ Batal", callback_data=f"retry_cancel:{job_id}"),
        ]
    ]

    text = (
        f"🔄 <b>Konfirmasi Pengulangan Batch #{job_id}</b>\n\n"
        f"• <b>Item Gagal:</b> {failed_count} item\n"
        f"• <b>Estimasi Tambahan Biaya:</b> <code>${cost_usd:.2f} USD</code>\n"
        f"• <b>Kebijakan:</b> Item yang sudah berhasil <b>TIDAK</b> akan diulang.\n\n"
        "Apakah Anda menyetujui komitmen biaya dan penjadwalan ulang item yang gagal?"
    )

    if update.effective_message:
        await update.effective_message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


@require_admin
async def retry_callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session_factory: Callable[[], Session],
) -> None:
    """Handle inline button callback for /retry spend confirmation."""
    query = update.callback_query
    if not query or not query.data:
        return

    await query.answer()
    data = query.data

    if data.startswith("retry_cancel:"):
        await query.edit_message_text("❌ Permintaan retry dibatalkan oleh operator.")
        return

    if not data.startswith("retry_confirm:"):
        return

    job_id_str = data.split(":")[1]
    try:
        job_id = int(job_id_str)
    except ValueError:
        await query.edit_message_text("Batch ID tidak valid.")
        return

    with session_factory() as session:
        job = session.get(Job, job_id)
        if not job:
            await query.edit_message_text(f"Batch #{job_id} tidak ditemukan.")
            return

        # Query failed items
        failed_items = session.scalars(
            select(JobItem).where(JobItem.job_id == job_id, JobItem.status == "failed")
        ).all()

        if not failed_items:
            await query.edit_message_text(f"Tidak ada item gagal yang tersisa pada Batch #{job_id}.")
            return

        # Reset failed items to pending
        count = 0
        for item in failed_items:
            item.status = "pending"
            item.last_error = None
            count += 1

        # Reset job status to pending
        job.status = "pending"
        session.commit()

    await query.edit_message_text(
        f"✅ <b>Retry Disetujui:</b> {count} item gagal pada Batch #{job_id} telah "
        "direset ke antrean <code>pending</code>.\n"
        "Item yang sudah selesai sebelumnya tetap dipertahankan.",
        parse_mode=ParseMode.HTML,
    )


@require_admin
async def providers_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    health_tracker: Optional[ProviderHealthTracker] = None,
) -> None:
    """Handle /providers command displaying live provider availability and health."""
    tracker = health_tracker or default_health_tracker
    providers = ["openai", "stability", "fal", "anthropic", "gemini"]

    lines = ["🌐 <b>Status Kesehatan Provider AI (SF-019)</b>\n"]
    for p in providers:
        rec = tracker._records.get(p.lower())
        status = rec.status.value if rec else "HEALTHY"
        icon = "🟢" if status == "HEALTHY" else ("🟡" if status == "COOLDOWN" else "🔴")

        desc = f"• <code>{p.upper()}</code>: {icon} <b>{status}</b>"
        if rec and rec.status.value == "COOLDOWN":
            remaining = tracker.get_cooldown_remaining(p)
            desc += f" (Cooldown sisa: {remaining:.1f}s)"
            if rec.reason:
                desc += f"\n  └ <i>Alasan:</i> {html.escape(rec.reason)}"
        elif rec and rec.status.value == "DISABLED":
            if rec.reason:
                desc += f"\n  └ <i>Alasan:</i> {html.escape(rec.reason)}"
        lines.append(desc)

    text = "\n".join(lines)
    if update.effective_message:
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)

