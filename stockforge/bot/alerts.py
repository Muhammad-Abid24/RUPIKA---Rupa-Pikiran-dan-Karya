"""Gateway connection monitoring and Telegram admin alert notifications (SF-003)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import html
import logging
import threading
import time
from typing import Dict, List, Optional
import httpx

from stockforge.config import get_settings

logger = logging.getLogger("stockforge.bot.alerts")
WIB = timezone(timedelta(hours=7))


def _get_admin_ids() -> List[int]:
    """Retrieve configured admin Telegram IDs from settings."""
    try:
        return get_settings().admin_ids_list
    except Exception:
        return []


def _get_bot_token() -> Optional[str]:
    """Retrieve Telegram bot token from settings."""
    try:
        settings = get_settings()
        if settings.telegram_bot_token:
            return settings.telegram_bot_token.get_secret_value()
    except Exception:
        pass
    return None


def send_admin_alert_sync(text: str) -> None:
    """Send an HTML alert message to all configured Telegram admin IDs via synchronous HTTP."""
    token = _get_bot_token()
    admin_ids = _get_admin_ids()
    if not token or not admin_ids:
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        with httpx.Client(timeout=8.0) as client:
            for admin_id in admin_ids:
                try:
                    resp = client.post(
                        url,
                        json={
                            "chat_id": admin_id,
                            "text": text,
                            "parse_mode": "HTML",
                        },
                    )
                    if resp.status_code == 200:
                        logger.info("Alert gateway berhasil dikirim ke admin #%s", admin_id)
                    else:
                        logger.warning("Gagal mengirim alert gateway ke admin #%s: %s", admin_id, resp.text[:120])
                except Exception as e:
                    logger.debug("Tidak dapat mengirim alert ke admin #%s: %s", admin_id, e)
    except Exception as e:
        logger.debug("Gagal membuka koneksi HTTP untuk alert: %s", e)


async def send_admin_alert_async(text: str) -> None:
    """Send an HTML alert message to all configured Telegram admin IDs asynchronously."""
    token = _get_bot_token()
    admin_ids = _get_admin_ids()
    if not token or not admin_ids:
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            for admin_id in admin_ids:
                try:
                    resp = await client.post(
                        url,
                        json={
                            "chat_id": admin_id,
                            "text": text,
                            "parse_mode": "HTML",
                        },
                    )
                    if resp.status_code == 200:
                        logger.info("Alert gateway berhasil dikirim ke admin #%s", admin_id)
                    else:
                        logger.warning("Gagal mengirim alert gateway ke admin #%s: %s", admin_id, resp.text[:120])
                except Exception as e:
                    logger.debug("Tidak dapat mengirim alert ke admin #%s: %s", admin_id, e)
    except Exception as e:
        logger.debug("Gagal membuka koneksi HTTP async untuk alert: %s", e)


class GatewayAlertManager:
    """Tracks gateway connectivity state and broadcasts disconnect/reconnect alerts."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Telegram gateway state
        self.tg_disconnected: bool = False
        self.tg_disconnected_at: Optional[float] = None
        self.tg_disconnect_reason: str = ""

        # AI gateway states by provider name
        self.ai_disconnected: Dict[str, bool] = {}
        self.ai_disconnected_at: Dict[str, float] = {}
        self.ai_disconnect_reason: Dict[str, str] = {}

    def on_telegram_disconnected(self, reason: str) -> None:
        """Record and broadcast alert when connection to Telegram gateway drops."""
        with self._lock:
            now = time.time()
            if self.tg_disconnected:
                # Already recorded disconnect, don't spam
                return

            self.tg_disconnected = True
            self.tg_disconnected_at = now
            self.tg_disconnect_reason = reason

        time_wib = datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S WIB")
        logger.error("[ALERT GATEWAY] Koneksi bot dengan Telegram Gateway terputus! Alasan: %s", reason)

        alert_text = (
            "🚨 <b>[ALERT] Koneksi Bot dengan Gateway Terputus</b>\n\n"
            "• <b>Status:</b> <code>TERPUTUS (OFFLINE)</code>\n"
            "• <b>Gateway:</b> <code>Telegram Server API</code>\n"
            f"• <b>Waktu:</b> <code>{time_wib}</code>\n"
            f"• <b>Penyebab:</b> <code>{html.escape(reason[:200])}</code>\n\n"
            "<i>Sistem RUPIKA sedang berusaha menyambung kembali secara otomatis...</i>"
        )
        send_admin_alert_sync(alert_text)

    def on_telegram_reconnected(self) -> None:
        """Record and broadcast alert when connection to Telegram gateway recovers."""
        with self._lock:
            if not self.tg_disconnected:
                return

            duration = (time.time() - self.tg_disconnected_at) if self.tg_disconnected_at else 0.0
            last_reason = self.tg_disconnect_reason
            self.tg_disconnected = False
            self.tg_disconnected_at = None
            self.tg_disconnect_reason = ""

        dur_str = f"{duration:.1f} detik" if duration < 60 else f"{int(duration // 60)} menit {int(duration % 60)} detik"
        time_wib = datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S WIB")
        logger.info("[ALERT GATEWAY] Koneksi bot dengan Telegram Gateway terhubung kembali setelah %s", dur_str)

        alert_text = (
            "🟢 <b>[ALERT] Koneksi Bot dengan Gateway Terhubung Kembali</b>\n\n"
            "• <b>Status:</b> <code>TERHUBUNG (ONLINE)</code>\n"
            "• <b>Gateway:</b> <code>Telegram Server API</code>\n"
            f"• <b>Waktu Pulih:</b> <code>{time_wib}</code>\n"
            f"• <b>Durasi Terputus:</b> <code>{dur_str}</code>\n"
            f"• <b>Penyebab Sebelumnya:</b> <code>{html.escape(last_reason[:150])}</code>\n\n"
            "<i>Koneksi bot telah normal kembali dan siap menerima perintah.</i>"
        )
        send_admin_alert_sync(alert_text)

    def on_ai_gateway_disconnected(self, provider_name: str, url: str, reason: str) -> None:
        """Record and broadcast alert when connection to AI provider gateway drops."""
        with self._lock:
            if self.ai_disconnected.get(provider_name):
                return
            self.ai_disconnected[provider_name] = True
            self.ai_disconnected_at[provider_name] = time.time()
            self.ai_disconnect_reason[provider_name] = reason

        time_wib = datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S WIB")
        logger.error("[ALERT GATEWAY] Koneksi dengan AI Gateway (%s) terputus! Alasan: %s", provider_name, reason)

        alert_text = (
            "🚨 <b>[ALERT] Koneksi Bot dengan AI Gateway Terputus</b>\n\n"
            "• <b>Status:</b> <code>TERPUTUS (OFFLINE)</code>\n"
            f"• <b>Gateway AI:</b> <code>{html.escape(provider_name)} ({html.escape(url)})</code>\n"
            f"• <b>Waktu:</b> <code>{time_wib}</code>\n"
            f"• <b>Penyebab:</b> <code>{html.escape(reason[:200])}</code>\n\n"
            "<i>Model AI sedang tidak dapat dihubungi. Bot akan mencoba menyambung kembali saat request berikutnya.</i>"
        )
        send_admin_alert_sync(alert_text)

    def on_ai_gateway_reconnected(self, provider_name: str, url: str) -> None:
        """Record and broadcast alert when connection to AI provider gateway recovers."""
        with self._lock:
            if not self.ai_disconnected.get(provider_name):
                return
            duration = (time.time() - self.ai_disconnected_at.get(provider_name, time.time()))
            last_reason = self.ai_disconnect_reason.get(provider_name, "Koneksi tidak stabil")
            self.ai_disconnected[provider_name] = False
            self.ai_disconnected_at.pop(provider_name, None)
            self.ai_disconnect_reason.pop(provider_name, None)

        dur_str = f"{duration:.1f} detik" if duration < 60 else f"{int(duration // 60)} menit {int(duration % 60)} detik"
        time_wib = datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S WIB")
        logger.info("[ALERT GATEWAY] Koneksi dengan AI Gateway (%s) terhubung kembali setelah %s", provider_name, dur_str)

        alert_text = (
            "🟢 <b>[ALERT] Koneksi Bot dengan AI Gateway Terhubung Kembali</b>\n\n"
            "• <b>Status:</b> <code>TERHUBUNG (ONLINE)</code>\n"
            f"• <b>Gateway AI:</b> <code>{html.escape(provider_name)} ({html.escape(url)})</code>\n"
            f"• <b>Waktu Pulih:</b> <code>{time_wib}</code>\n"
            f"• <b>Durasi Terputus:</b> <code>{dur_str}</code>\n"
            f"• <b>Penyebab Sebelumnya:</b> <code>{html.escape(last_reason[:150])}</code>\n\n"
            "<i>Layanan model AI telah pulih dan siap memproses permintaan gambar/riset kembali.</i>"
        )
        send_admin_alert_sync(alert_text)


# Global singleton alert manager
gateway_alerts = GatewayAlertManager()

