"""Telegram authentication and chat type access control filters (SF-003)."""
from __future__ import annotations

import functools
import logging
from typing import Any, Callable, Coroutine, List, Optional
from telegram import Chat, Update
from telegram.constants import ChatType
from telegram.ext import ContextTypes

from stockforge.config import get_settings

logger = logging.getLogger("stockforge.bot.auth")

DENIED_GENERIC_MESSAGE = (
    "Akses ditolak: Anda tidak memiliki otorisasi untuk mengakses layanan ini."
)
GROUP_CHAT_REJECT_MESSAGE = (
    "Akses ditolak: RUPIKA hanya dapat digunakan melalui private chat."
)


def get_authorized_admin_ids() -> List[int]:
    """Retrieve numeric allowlisted admin IDs from settings."""
    settings = get_settings()
    return settings.admin_ids_list


def is_authorized_user(user_id: Optional[int], allowed_ids: Optional[List[int]] = None) -> bool:
    """Verify if a numeric Telegram user ID is present in the allowlist."""
    if user_id is None:
        return False
    ids = allowed_ids if allowed_ids is not None else get_authorized_admin_ids()
    return user_id in ids


def is_private_chat(chat: Optional[Chat]) -> bool:
    """Verify that interaction occurs exclusively in a private 1-to-1 chat."""
    if not chat:
        return False
    return chat.type == ChatType.PRIVATE or chat.type == "private"


def require_admin(
    handler_fn: Callable[..., Coroutine[Any, Any, Any]]
) -> Callable[..., Coroutine[Any, Any, Any]]:
    """Decorator enforcing private-chat-only and allowlisted numeric admin ID checks."""

    @functools.wraps(handler_fn)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args: Any, **kwargs: Any) -> Any:
        if not update:
            return

        chat = update.effective_chat
        user = update.effective_user

        is_callback = bool(
            update.callback_query is not None
            and isinstance(getattr(update.callback_query, "data", None), str)
        )

        # 1. Enforce private chat only (reject groups, supergroups, channels)
        if not is_private_chat(chat):
            logger.warning(
                "Interaksi non-private chat ditolak: chat_id=%s, type=%s, user_id=%s",
                chat.id if chat else "unknown",
                chat.type if chat else "unknown",
                user.id if user else "unknown",
            )
            if is_callback and update.callback_query:
                try:
                    await update.callback_query.answer(GROUP_CHAT_REJECT_MESSAGE, show_alert=True)
                except Exception:
                    pass
            elif update.effective_message:
                try:
                    await update.effective_message.reply_text(GROUP_CHAT_REJECT_MESSAGE)
                except Exception:
                    pass
            return

        # 2. Enforce numeric admin ID allowlist
        user_id = user.id if user else None
        if not is_authorized_user(user_id):
            logger.warning("Percobaan akses tidak terotorisasi: user_id=%s", user_id)
            if is_callback and update.callback_query:
                try:
                    await update.callback_query.answer(DENIED_GENERIC_MESSAGE, show_alert=True)
                except Exception:
                    pass
            elif update.effective_message:
                # Deny safely without revealing configuration, tokens, or admin lists
                await update.effective_message.reply_text(DENIED_GENERIC_MESSAGE)
            return

        # Authorized admin in private chat
        return await handler_fn(update, context, *args, **kwargs)

    return wrapper

