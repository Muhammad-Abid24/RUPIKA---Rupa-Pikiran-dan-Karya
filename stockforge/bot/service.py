"""Telegram bot application lifecycle and error handling service (SF-003)."""
from __future__ import annotations

import asyncio
import logging
import socket
from typing import Any, Optional, Tuple
from telegram.error import Conflict, NetworkError, TelegramError, TimedOut
from telegram.ext import Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes
from telegram.request import HTTPXRequest

from stockforge.bot.handlers import health_handler, help_handler, start_handler
from stockforge.bot.review import review_callback_handler, review_command_handler
from stockforge.config import get_settings

logger = logging.getLogger("stockforge.bot.service")


def _build_tcp_keepalive_socket_options() -> list[Tuple[int, int, int]]:
    """Configure TCP Keepalive socket options to keep long-polling connections persistently active."""
    options = [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]
    if hasattr(socket, "TCP_KEEPIDLE"):
        options.append((socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 15))
    elif hasattr(socket, "TCP_KEEPALIVE"):
        options.append((socket.IPPROTO_TCP, socket.TCP_KEEPALIVE, 15))
    if hasattr(socket, "TCP_KEEPINTVL"):
        options.append((socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 5))
    if hasattr(socket, "TCP_KEEPCNT"):
        options.append((socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3))
    return options


class ResilientHTTPXRequest(HTTPXRequest):
    """Custom HTTPXRequest that transparently retries on dropped or stale sockets.
    
    Prevents httpx.RemoteProtocolError and server TCP FIN disconnections from interrupting
    the long-polling loop or surfacing as alarming warnings in user logs.
    """

    async def do_request(self, *args: Any, **kwargs: Any) -> tuple[int, bytes]:
        max_transient_retries = 3
        for attempt in range(max_transient_retries):
            try:
                return await super().do_request(*args, **kwargs)
            except NetworkError as err:
                err_str = str(err).lower()
                is_stale_disconnect = (
                    "remoteprotocolerror" in err_str
                    or "server disconnected without sending a response" in err_str
                    or "connection reset" in err_str
                    or "eof occurred in violation of protocol" in err_str
                    or "broken pipe" in err_str
                )
                if is_stale_disconnect and attempt < (max_transient_retries - 1):
                    logger.debug(
                        "Koneksi stale terputus oleh server (%s). Melakukan retry transparan (percobaan %d/%d)...",
                        err,
                        attempt + 1,
                        max_transient_retries,
                    )
                    await asyncio.sleep(0.3 * (attempt + 1))
                    continue
                raise


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
            connect_timeout=30.0,
            read_timeout=30.0,
            write_timeout=30.0,
            pool_timeout=30.0,
            socket_options=sock_opts,
        )

        get_updates_request = ResilientHTTPXRequest(
            connection_pool_size=8,
            connect_timeout=30.0,
            read_timeout=30.0,
            write_timeout=30.0,
            pool_timeout=30.0,
            socket_options=sock_opts,
        )

        app = (
            ApplicationBuilder()
            .token(self.token)
            .concurrent_updates(True)
            .request(main_request)
            .get_updates_request(get_updates_request)
            .build()
        )

        # Register command handlers
        app.add_handler(CommandHandler("start", start_handler))
        app.add_handler(CommandHandler("help", help_handler))
        app.add_handler(CommandHandler("health", health_handler))
        app.add_handler(CommandHandler("review", review_command_handler))

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
            for cfg_candidate in [Path("config/models.yaml"), Path("/app/config/models.yaml")]:
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

        app.add_handler(CommandHandler("status", _status_handler))
        app.add_handler(CommandHandler("jobs", _jobs_handler))
        app.add_handler(CommandHandler("failed", _failed_handler))
        app.add_handler(CommandHandler("retry", _retry_handler))
        app.add_handler(CommandHandler("providers", _providers_handler))
        app.add_handler(CommandHandler("research", _research_cmd))
        app.add_handler(CommandHandler("researchmode", _researchmode_cmd))
        app.add_handler(CommandHandler("researchhistory", _researchhistory_cmd))
        app.add_handler(CommandHandler("settings", _settings_cmd))

        # Register callback query handlers
        app.add_handler(CallbackQueryHandler(review_callback_handler, pattern=r"^rev:"))
        app.add_handler(CallbackQueryHandler(_retry_cb, pattern=r"^retry_"))
        app.add_handler(CallbackQueryHandler(_research_cb, pattern=r"^rsh:"))


        # Register global error handler with 409 Conflict detection
        async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
            err = context.error
            err_str = str(err).lower() if err else ""
            if isinstance(err, Conflict) or "conflict" in err_str:
                self.conflict_detected = True
                logger.critical(
                    "Konflik polling Telegram terdeteksi (HTTP 409 Conflict): "
                    "instance bot lain sedang aktif. Menghentikan polling secara aman..."
                )
                if context.application and context.application.updater:
                    await context.application.updater.stop()
                if context.application:
                    await context.application.stop()
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
                    await update.effective_message.reply_text(
                        "⚠️ Terjadi kendala saat memproses permintaan Anda. Silakan coba beberapa saat lagi."
                    )
                except Exception as reply_err:
                    logger.debug("Tidak dapat mengirim pesan error fallback ke user: %s", reply_err)

        app.add_error_handler(error_handler)
        return app

    def run_polling(
        self,
        drop_pending_updates: bool = False,
        bootstrap_retries: int = -1,
        poll_interval: float = 0.0,
        timeout: int = 10,
    ) -> None:
        """Start long polling with graceful shutdown on conflict."""
        app = self.build_application()
        logger.info(
            "Memulai Telegram bot polling (drop_pending_updates=%s, bootstrap_retries=%s, timeout=%ds)...",
            drop_pending_updates,
            bootstrap_retries,
            timeout,
        )
        app.run_polling(
            drop_pending_updates=drop_pending_updates,
            bootstrap_retries=bootstrap_retries,
            poll_interval=poll_interval,
            timeout=timeout,
        )


def start_bot(
    token: Optional[str] = None,
    drop_pending_updates: bool = False,
) -> None:
    """Entry point to launch the bot service with connection resilience."""
    import time

    service = BotService(token=token)
    attempt = 0
    while True:
        try:
            service.run_polling(drop_pending_updates=drop_pending_updates)
            if service.conflict_detected:
                logger.error("Bot dihentikan karena konflik polling dengan instance lain.")
                break
            break
        except (TimedOut, NetworkError, OSError) as net_err:
            if service.conflict_detected:
                break
            attempt += 1
            delay = min(2 ** attempt, 30)
            logger.warning(
                "Koneksi polling Telegram terganggu (%s). Menyambung kembali dalam %d detik (percobaan #%d)...",
                net_err,
                delay,
                attempt,
            )
            time.sleep(delay)
        except Exception as ex:
            if "conflict" in str(ex).lower():
                logger.critical("Konflik 409 terdeteksi saat startup: %s", ex)
                break
            logger.error("Galat tak terduga pada bot service: %s", ex, exc_info=True)
            raise

