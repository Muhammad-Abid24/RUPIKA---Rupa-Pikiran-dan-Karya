"""Telegram bot application lifecycle and error handling service (SF-003)."""
from __future__ import annotations

import logging
from typing import Optional
from telegram.error import Conflict, TelegramError
from telegram.ext import Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes

from stockforge.bot.handlers import health_handler, help_handler, start_handler
from stockforge.bot.review import review_callback_handler, review_command_handler
from stockforge.config import get_settings

logger = logging.getLogger("stockforge.bot.service")


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

    def build_application(self) -> Application:
        """Configure Application with handlers and conflict-aware error handling."""
        if not self.token:
            raise ValueError(
                "TELEGRAM_BOT_TOKEN belum dikonfigurasi. "
                "Harap isi TELEGRAM_BOT_TOKEN di .env atau berikan parameter token."
            )

        app = ApplicationBuilder().token(self.token).build()

        # Register command handlers
        app.add_handler(CommandHandler("start", start_handler))
        app.add_handler(CommandHandler("help", help_handler))
        app.add_handler(CommandHandler("health", health_handler))
        app.add_handler(CommandHandler("review", review_command_handler))

        from stockforge.bot.progress_handlers import (
            failed_command,
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
            if isinstance(err, Conflict) or (err and "conflict" in str(err).lower()):
                self.conflict_detected = True
                logger.critical(
                    "Konflik polling Telegram terdeteksi (HTTP 409 Conflict): "
                    "instance bot lain sedang aktif. Menghentikan polling secara aman..."
                )
                if context.application and context.application.updater:
                    await context.application.updater.stop()
                if context.application:
                    await context.application.stop()
            elif isinstance(err, TelegramError):
                logger.error("Terjadi galat Telegram: %s", err)
            else:
                logger.error("Galat tak terduga pada bot Telegram: %s", err, exc_info=True)

        app.add_error_handler(error_handler)
        return app

    def run_polling(self, drop_pending_updates: bool = True) -> None:
        """Start long polling with graceful shutdown on conflict."""
        app = self.build_application()
        logger.info("Memulai Telegram bot polling (drop_pending_updates=%s)...", drop_pending_updates)
        app.run_polling(drop_pending_updates=drop_pending_updates)


def start_bot(token: Optional[str] = None, drop_pending_updates: bool = True) -> None:
    """Entry point to launch the bot service."""
    service = BotService(token=token)
    service.run_polling(drop_pending_updates=drop_pending_updates)

