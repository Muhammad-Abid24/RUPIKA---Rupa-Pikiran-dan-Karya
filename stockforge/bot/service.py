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

        # Register callback query handlers
        app.add_handler(CallbackQueryHandler(review_callback_handler, pattern=r"^rev:"))


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

    def run_polling(self) -> None:
        """Start long polling with graceful shutdown on conflict."""
        app = self.build_application()
        logger.info("Memulai Telegram bot polling...")
        app.run_polling(drop_pending_updates=False)


def start_bot(token: Optional[str] = None) -> None:
    """Entry point to launch the bot service."""
    service = BotService(token=token)
    service.run_polling()

