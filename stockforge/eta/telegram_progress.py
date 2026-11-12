"""Telegram live progress formatter and rate-limited updater (SF-023)."""
from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from stockforge.eta.models import ConfidenceLevel, EtaEstimate, EtaStatus
from stockforge.eta.tracker import JobEtaTracker


class TelegramProgressFormatter:
    """Formats standardized, secret-safe Telegram live progress messages."""

    @staticmethod
    def format_duration(seconds: float) -> str:
        """Format seconds into human-readable string: e.g. '1m 20s' or '45s'."""
        sec = max(0, int(round(seconds)))
        m, s = divmod(sec, 60)
        h, m = divmod(m, 60)
        if h > 0:
            return f"{h}j {m}m"
        if m > 0:
            return f"{m}m {s}s"
        return f"{s}s"

    @classmethod
    def format_progress_message(
        cls,
        tracker: JobEtaTracker,
        tz_name: str = "Asia/Jakarta",
    ) -> str:
        """Construct the live progress text for a batch."""
        counts = tracker.get_counts()
        active_idx, active_stage = tracker.get_active_stage_and_item()
        eta = tracker.calculate_eta()

        # Display timezone
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = timezone.utc

        now_local = eta.updated_at.astimezone(tz)
        ts_str = now_local.strftime("%Y-%m-%d %H:%M:%S")

        # Stage line
        stage_str = active_stage.value if active_stage else "standby"

        lines = [
            f"📦 **Batch #{tracker.job_id}**",
            f"🎯 Progress: **image {active_idx}/{tracker.total_items}** ({stage_str})",
            f"📊 Status: Selesai: {counts['completed']} | Gagal: {counts['failed']} | Proses: {counts['processing']} | Menunggu: {counts['pending']}",
            "",
        ]

        # ETA details
        if eta.status == EtaStatus.AVAILABLE:
            min_str = cls.format_duration(eta.remaining_seconds_min)
            max_str = cls.format_duration(eta.remaining_seconds_max)
            conf_str = "TINGGI" if eta.confidence == ConfidenceLevel.HIGH else (
                "SEDANG" if eta.confidence == ConfidenceLevel.MEDIUM else "RENDAH"
            )
            lines.append(f"⏱️ **Estimasi sisa**: {min_str} – {max_str} (Confidence: {conf_str})")
        elif eta.status == EtaStatus.UNAVAILABLE_INSUFFICIENT_DATA:
            lines.append("⏱️ **Estimasi sisa**: belum dapat diperkirakan — perlu inspeksi (mengumpulkan data durasi)")
        elif eta.status == EtaStatus.SUSPENDED_APPROVAL:
            lines.append("⏱️ **Estimasi sisa**: DITANGGUHKAN — menunggu persetujuan manual operator")
        elif eta.status == EtaStatus.SUSPENDED_PAUSE:
            lines.append("⏱️ **Estimasi sisa**: DITANGGUHKAN — batch dijeda oleh operator")
        elif eta.status == EtaStatus.SUSPENDED_PROVIDER_OUTAGE:
            lines.append(f"⏱️ **Estimasi sisa**: DITANGGUHKAN — {eta.reason}")
        elif eta.status == EtaStatus.SUSPENDED_AMBIGUOUS_CALL:
            lines.append("⏱️ **Estimasi sisa**: DITANGGUHKAN — rekonsiliasi pembayaran ambigu")
        elif eta.status == EtaStatus.COMPLETED:
            lines.append("⏱️ **Estimasi sisa**: Selesai")
        elif eta.status == EtaStatus.CANCELLED:
            lines.append("⏱️ **Estimasi sisa**: Dibatalkan")
        elif eta.status == EtaStatus.FAILED:
            lines.append("⏱️ **Estimasi sisa**: Gagal")

        lines.append(f"🕒 Terakhir diperbarui: {ts_str} ({tz_name})")
        return "\n".join(lines)


class ThrottledProgressUpdater:
    """Throttles Telegram message edits to avoid Telegram 429 rate limit exceptions."""

    def __init__(self, min_edit_interval_seconds: float = 5.0) -> None:
        self.min_interval = max(1.0, min_edit_interval_seconds)
        self.last_edit_monotonic: Optional[float] = None
        self.last_render_hash: Optional[str] = None

    def should_edit(self, text: str, force: bool = False) -> bool:
        """Evaluate if an edit should be sent.
        
        Rules:
        - If force is True: always allows edit.
        - If text is unchanged from last edit: suppresses redundant edit.
        - If elapsed time since last edit < min_interval: suppresses edit to protect rate limit.
        """
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

        if force:
            self.last_render_hash = text_hash
            self.last_edit_monotonic = time.monotonic()
            return True

        # Redundant content check
        if self.last_render_hash == text_hash:
            return False

        # Throttling interval check
        now = time.monotonic()
        if self.last_edit_monotonic is not None:
            elapsed = now - self.last_edit_monotonic
            if elapsed < self.min_interval:
                return False

        self.last_render_hash = text_hash
        self.last_edit_monotonic = now
        return True
