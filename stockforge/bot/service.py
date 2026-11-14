"""Telegram bot application lifecycle and error handling service (SF-003)."""
from __future__ import annotations

import asyncio
import html
import logging
import socket
from typing import Any, Optional, Tuple
import httpx
from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import Conflict, NetworkError, TelegramError, TimedOut
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    TypeHandler,
    filters,
)
from telegram.request import HTTPXRequest

from stockforge.bot.alerts import gateway_alerts
from stockforge.bot.handlers import (
    health_handler,
    help_handler,
    start_handler,
    unknown_message_handler,
)
from stockforge.bot.review import review_callback_handler, review_command_handler
from stockforge.config import get_settings

logger = logging.getLogger("stockforge.bot.service")


def _build_tcp_keepalive_socket_options() -> list[Tuple[int, int, int]]:
    """Configure TCP Keepalive socket options to keep long-polling connections persistently active."""
    options: list[Tuple[int, int, int]] = [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]
    tcp_keepidle = getattr(socket, "TCP_KEEPIDLE", None)
    tcp_keepalive = getattr(socket, "TCP_KEEPALIVE", None)
    tcp_keepintvl = getattr(socket, "TCP_KEEPINTVL", None)
    tcp_keepcnt = getattr(socket, "TCP_KEEPCNT", None)

    if tcp_keepidle is not None:
        options.append((socket.IPPROTO_TCP, tcp_keepidle, 15))
    elif tcp_keepalive is not None:
        options.append((socket.IPPROTO_TCP, tcp_keepalive, 15))
    if tcp_keepintvl is not None:
        options.append((socket.IPPROTO_TCP, tcp_keepintvl, 5))
    if tcp_keepcnt is not None:
        options.append((socket.IPPROTO_TCP, tcp_keepcnt, 3))
    return options


class ResilientHTTPXRequest(HTTPXRequest):
    """Custom HTTPXRequest that transparently retries on dropped or stale sockets.
    
    Prevents httpx.RemoteProtocolError and server TCP FIN disconnections from interrupting
    the long-polling loop or surfacing as alarming warnings in user logs.
    """

    async def do_request(self, *args: Any, **kwargs: Any) -> tuple[int, bytes]:
        max_transient_retries = 5
        for attempt in range(max_transient_retries):
            try:
                return await super().do_request(*args, **kwargs)
            except (NetworkError, TimedOut) as err:
                err_str = str(err).lower()
                is_transient = (
                    "remoteprotocolerror" in err_str
                    or "server disconnected without sending a response" in err_str
                    or "connection reset" in err_str
                    or "eof occurred in violation of protocol" in err_str
                    or "broken pipe" in err_str
                    or "handshake" in err_str
                    or "connect" in err_str
                    or "timed out" in err_str
                )
                if is_transient and attempt < (max_transient_retries - 1):
                    logger.debug(
                        "Koneksi terganggu ke server Telegram (%s). Menyambung kembali otomatis (percobaan %d/%d)...",
                        err,
                        attempt + 1,
                        max_transient_retries,
                    )
                    await asyncio.sleep(0.3 * (attempt + 1))
                    continue
                raise
        return await super().do_request(*args, **kwargs)


class BotService:
    """Manages Telegram bot application configuration, handlers, and lifecycle."""

    def __init__(self, token: Optional[str] = None) -> None:
        settings = get_settings()
        raw_token = token or (
            settings.telegram_bot_token.get_secret_value()
            if settings.telegram_bot_token
            else None
        )
        self.token = raw_token
        self.conflict_detected = False
        self.conflict_count = 0

    def build_application(self) -> Application:
        """Configure Application with handlers and conflict-aware error handling."""
        if not self.token:
            raise ValueError(
                "TELEGRAM_BOT_TOKEN belum dikonfigurasi. "
                "Harap isi TELEGRAM_BOT_TOKEN di .env atau berikan parameter token."
            )

        sock_opts = _build_tcp_keepalive_socket_options()

        main_request = ResilientHTTPXRequest(
            connection_pool_size=16,
            connect_timeout=15.0,
            read_timeout=30.0,
            write_timeout=30.0,
            pool_timeout=15.0,
            socket_options=sock_opts,
            httpx_kwargs={
                "limits": httpx.Limits(
                    max_connections=16,
                    max_keepalive_connections=4,
                    keepalive_expiry=10.0,
                )
            },
        )

        get_updates_request = ResilientHTTPXRequest(
            connection_pool_size=1,
            connect_timeout=15.0,
            read_timeout=35.0,  # Must comfortably exceed polling timeout (10s) to avoid client drop triggering 409
            write_timeout=15.0,
            pool_timeout=15.0,
            socket_options=sock_opts,
            httpx_kwargs={
                "limits": httpx.Limits(
                    max_connections=1,
                    max_keepalive_connections=0,
                    keepalive_expiry=5.0,
                )
            },
        )

        async def _on_post_init(application: Application) -> None:
            gateway_alerts.on_telegram_reconnected()

        app = (
            ApplicationBuilder()
            .token(self.token)
            .defaults(Defaults(block=False))
            .concurrent_updates(True)
            .post_init(_on_post_init)
            .request(main_request)
            .get_updates_request(get_updates_request)
            .build()
        )

        # 0. Global logger and typing indicator for all incoming user interactions
        async def _log_all_incoming_updates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            user = update.effective_user
            chat = update.effective_chat
            msg = update.effective_message
            text_preview = (
                (msg.text[:40] + "...")
                if (msg and msg.text and len(msg.text) > 40)
                else (msg.text if msg else None)
            )
            cb_data = update.callback_query.data if update.callback_query else None
            logger.info(
                "Update Telegram #%s diterima (chat=%s, user=%s, text=%r, callback=%r)",
                update.update_id,
                chat.id if chat else None,
                user.id if user else None,
                text_preview,
                cb_data,
            )

            # If an update is successfully received, ensure reconnect state is published if previously severed
            if gateway_alerts.tg_disconnected:
                gateway_alerts.on_telegram_reconnected()

            # Instantly indicate to the user that RUPIKA is active and preparing a response (..typing)
            if chat and context.bot:
                try:
                    await context.bot.send_chat_action(chat_id=chat.id, action=ChatAction.TYPING)
                except Exception as act_err:
                    logger.debug("Tidak dapat mengirim typing chat action global: %s", act_err)

        app.add_handler(TypeHandler(Update, _log_all_incoming_updates), group=-1)

        # Register command handlers (non-blocking so slow calls never freeze concurrent commands)
        app.add_handler(CommandHandler("start", start_handler, block=False))
        app.add_handler(CommandHandler("help", help_handler, block=False))
        app.add_handler(CommandHandler("health", health_handler, block=False))
        app.add_handler(CommandHandler("review", review_command_handler, block=False))

        from stockforge.bot.progress_handlers import (
            failed_command,
            jobs_command,
            providers_command,
            retry_callback_handler,
            retry_command,
            status_command,
        )
        from stockforge.bot.research_handlers import (
            research_callback_handler,
            research_command,
            researchhistory_command,
            researchmode_command,
            settings_command,
        )
        from stockforge.queue.service import QueueService
        from stockforge.research.gate import GenerationGate
        from stockforge.research.planner import ProductionPlanner
        from stockforge.providers.registry import ProviderRegistry
        from stockforge.providers.router import ProviderRouter
        from pathlib import Path

        def get_sf():
            from stockforge.db.session import create_session_factory
            return create_session_factory()

        def get_queue():
            from stockforge.config import get_settings
            from stockforge.db.session import create_db_engine
            return QueueService(engine=create_db_engine(get_settings().database_url))

        def get_router():
            import os
            from pathlib import Path
            config_dir = Path(os.environ.get("CONFIG_DIR", "config"))
            for cfg_candidate in [config_dir / "models.yaml", Path("config/models.yaml"), Path("/app/config/models.yaml")]:
                if cfg_candidate.exists():
                    reg = ProviderRegistry()
                    r = ProviderRouter(reg)
                    try:
                        r.load_from_yaml(cfg_candidate)
                        return r
                    except Exception as e:
                        logger.warning("Gagal memuat konfigurasi provider models.yaml: %s", e)
            return None

        async def _status_handler(u, c):
            await status_command(u, c, session_factory=get_sf())

        async def _jobs_handler(u, c):
            await jobs_command(u, c, session_factory=get_sf())

        async def _failed_handler(u, c):
            await failed_command(u, c, session_factory=get_sf())

        async def _retry_handler(u, c):
            await retry_command(u, c, session_factory=get_sf())

        async def _retry_cb(u, c):
            await retry_callback_handler(u, c, session_factory=get_sf())

        async def _providers_handler(u, c):
            await providers_command(u, c)

        async def _research_cmd(u, c):
            await research_command(u, c, session_factory=get_sf(), router=get_router())

        async def _research_cb(u, c):
            sf = get_sf()
            q = get_queue()
            g = GenerationGate(sf, q)
            p = ProductionPlanner(sf)
            await research_callback_handler(u, c, session_factory=sf, queue_service=q, gate=g, planner=p)

        async def _researchmode_cmd(u, c):
            await researchmode_command(u, c, session_factory=get_sf())

        async def _researchhistory_cmd(u, c):
            await researchhistory_command(u, c, session_factory=get_sf())

        async def _settings_cmd(u, c):
            await settings_command(u, c, session_factory=get_sf())

        app.add_handler(CommandHandler("status", _status_handler, block=False))
        app.add_handler(CommandHandler("jobs", _jobs_handler, block=False))
        app.add_handler(CommandHandler("failed", _failed_handler, block=False))
        app.add_handler(CommandHandler("retry", _retry_handler, block=False))
        app.add_handler(CommandHandler("providers", _providers_handler, block=False))
        app.add_handler(CommandHandler("research", _research_cmd, block=False))
        app.add_handler(CommandHandler("researchmode", _researchmode_cmd, block=False))
        app.add_handler(CommandHandler("researchhistory", _researchhistory_cmd, block=False))
        app.add_handler(CommandHandler("settings", _settings_cmd, block=False))

        # Register callback query handlers
        app.add_handler(CallbackQueryHandler(review_callback_handler, pattern=r"^rev:", block=False))
        app.add_handler(CallbackQueryHandler(_retry_cb, pattern=r"^retry_", block=False))
        app.add_handler(CallbackQueryHandler(_research_cb, pattern=r"^rsh:", block=False))

        # Register fallback message handlers for unknown commands and any incoming message
        app.add_handler(MessageHandler(filters.COMMAND, unknown_message_handler, block=False))
        app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, unknown_message_handler, block=False))

        # Register global error handler with 409 Conflict detection
        async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
            err = context.error
            err_str = str(err).lower() if err else ""
            if isinstance(err, Conflict) or "conflict" in err_str:
                self.conflict_detected = True
                gateway_alerts.on_telegram_disconnected("Konflik sesi polling Telegram aktif (HTTP 409 Conflict)")
                logger.critical(
                    "Konflik polling Telegram terdeteksi (HTTP 409 Conflict): "
                    "instance bot lain sedang aktif. Menyegarkan koneksi polling..."
                )
                if context.application and context.application.updater:
                    try:
                        await context.application.updater.stop()
                    except Exception:
                        pass
                if context.application:
                    try:
                        await context.application.stop()
                    except Exception:
                        pass
            elif (
                isinstance(err, (TimedOut, NetworkError))
                or "timed out" in err_str
                or "remoteprotocolerror" in err_str
                or "disconnected without sending a response" in err_str
            ):
                logger.debug(
                    "Koneksi polling Telegram disegarkan kembali (%s). Polling aktif menyambung otomatis...",
                    err,
                )
            elif isinstance(err, TelegramError):
                logger.error("Terjadi galat Telegram: %s", err)
            else:
                logger.error("Galat tak terduga pada bot Telegram: %s", err, exc_info=True)

            # Inform user if this error occurred during handling an update
            from telegram import Update
            if update and isinstance(update, Update) and update.effective_message:
                try:
                    err_hint = html.escape(str(err)[:180]) if err else "Terjadi kendala internal"
                    await update.effective_message.reply_text(
                        f"⚠️ <b>Terjadi kendala saat memproses permintaan:</b>\n\n"
                        f"<code>{err_hint}</code>\n\n"
                        f"<i>Silakan coba beberapa saat lagi.</i>",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception as reply_err:
                    logger.debug("Tidak dapat mengirim pesan error fallback ke user: %s", reply_err)

        app.add_error_handler(error_handler)
        return app

    def run_polling(
        self,
        drop_pending_updates: bool = False,
        bootstrap_retries: int = -1,
        poll_interval: float = 0.5,
        timeout: int = 10,
    ) -> None:
        """Start long polling with graceful shutdown on conflict."""
        app = self.build_application()
        logger.info(
            "Memulai Telegram bot polling (drop_pending_updates=%s, bootstrap_retries=%s, poll_interval=%.1fs, timeout=%ds)...",
            drop_pending_updates,
            bootstrap_retries,
            poll_interval,
            timeout,
        )
        app.run_polling(
            drop_pending_updates=drop_pending_updates,
            bootstrap_retries=bootstrap_retries,
            poll_interval=poll_interval,
            timeout=timeout,
            close_loop=False,
        )


def start_bot(
    token: Optional[str] = None,
    drop_pending_updates: bool = False,
) -> None:
    """Entry point to launch the bot service with continuous resilience and auto-restart."""
    import time

    attempt = 0
    while True:
        loop = None
        try:
            # Ensure a fresh, non-closed asyncio event loop is active for this thread
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            service = BotService(token=token)
            service.run_polling(drop_pending_updates=drop_pending_updates)
            if service.conflict_detected:
                gateway_alerts.on_telegram_disconnected("Konflik sesi polling Telegram aktif (HTTP 409 Conflict)")
                logger.warning("Bot mendeteksi konflik polling 409. Istirahat 10 detik agar koneksi lama Telegram kedaluwarsa...")
                time.sleep(10)
                continue
            logger.info("Polling Telegram selesai secara normal. Menyambung kembali polling dalam 1 detik...")
            time.sleep(1)
        except (TimedOut, NetworkError, OSError) as net_err:
            attempt += 1
            delay = min(2 ** attempt, 15)
            gateway_alerts.on_telegram_disconnected(f"Gangguan jaringan ke server Telegram ({net_err})")
            logger.warning(
                "Koneksi polling Telegram terganggu (%s). Menyambung kembali dalam %d detik (percobaan #%d)...",
                net_err,
                delay,
                attempt,
            )
            time.sleep(delay)
        except Exception as ex:
            if "conflict" in str(ex).lower():
                gateway_alerts.on_telegram_disconnected(f"Konflik sesi polling Telegram ({ex})")
                logger.warning("Konflik 409 terdeteksi saat startup: %s. Menunggu 10 detik sebelum re-polling...", ex)
                time.sleep(10)
                continue
            gateway_alerts.on_telegram_disconnected(f"Galat bot ({ex})")
            logger.error("Galat tak terduga pada bot service: %s. Memulai ulang dalam 3 detik...", ex, exc_info=True)
            time.sleep(3)
        finally:
            if loop is not None:
                try:
                    if not loop.is_closed():
                        loop.close()
                except Exception:
                    pass

