"""Acceptance tests for SF-003 Telegram authentication and bot handlers."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from telegram import Chat, Message, Update, User
from telegram.constants import ChatType
from telegram.error import Conflict
from typer.testing import CliRunner

from stockforge.bot.auth import (
    DENIED_GENERIC_MESSAGE,
    GROUP_CHAT_REJECT_MESSAGE,
    is_authorized_user,
    is_private_chat,
)
from stockforge.bot.handlers import health_handler, help_handler, start_handler
from stockforge.bot.service import BotService
from stockforge.cli import app

runner = CliRunner()


def build_mock_update(user_id: int, chat_id: int, chat_type: str = ChatType.PRIVATE) -> Update:
    """Construct mock Update instance with AsyncMock reply_text."""
    update = MagicMock(spec=Update)

    user = MagicMock(spec=User)
    user.id = user_id
    user.first_name = "TestAdmin"

    chat = MagicMock(spec=Chat)
    chat.id = chat_id
    chat.type = chat_type

    message = MagicMock(spec=Message)
    message.reply_text = AsyncMock()

    update.effective_user = user
    update.effective_chat = chat
    update.effective_message = message
    return update


def test_authorized_admin_start_handler(monkeypatch):
    """Verify authorized admin receives greeting and main keyboard on /start."""
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "1001, 1002")
    update = build_mock_update(user_id=1001, chat_id=1001, chat_type=ChatType.PRIVATE)
    context = MagicMock()

    asyncio.run(start_handler(update, context))

    update.effective_message.reply_text.assert_called_once()
    args, kwargs = update.effective_message.reply_text.call_args
    assert "RUPIKA" in args[0]
    assert "reply_markup" in kwargs


def test_authorized_admin_help_handler(monkeypatch):
    """Verify authorized admin receives help documentation on /help."""
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "1001")
    update = build_mock_update(user_id=1001, chat_id=1001, chat_type=ChatType.PRIVATE)
    context = MagicMock()

    asyncio.run(help_handler(update, context))

    update.effective_message.reply_text.assert_called_once()
    args, _ = update.effective_message.reply_text.call_args
    assert "Panduan Perintah" in args[0]
    assert "/research" in args[0]
    assert "/health" in args[0]


def test_authorized_admin_health_handler(monkeypatch, tmp_path):
    """Verify authorized admin receives diagnostic report on /health."""
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "1001")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/health.db")

    update = build_mock_update(user_id=1001, chat_id=1001, chat_type=ChatType.PRIVATE)
    context = MagicMock()

    asyncio.run(health_handler(update, context))

    update.effective_message.reply_text.assert_called_once()
    args, _ = update.effective_message.reply_text.call_args
    assert "Status Kesehatan Sistem" in args[0]
    assert "Basis Data SQLite" in args[0]


def test_unauthorized_user_denied_without_leaking_config(monkeypatch):
    """Verify unauthorized users are denied safely without revealing admin list or config."""
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "1001, 1002")
    # User 9999 is NOT in allowlist
    update = build_mock_update(user_id=9999, chat_id=9999, chat_type=ChatType.PRIVATE)
    context = MagicMock()

    asyncio.run(start_handler(update, context))

    update.effective_message.reply_text.assert_called_once_with(DENIED_GENERIC_MESSAGE)


def test_group_chat_rejected_even_for_admin(monkeypatch):
    """Verify group interactions are rejected even if requested by an admin."""
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "1001")
    # Admin 1001, but in a GROUP chat
    update = build_mock_update(user_id=1001, chat_id=-100123456, chat_type=ChatType.GROUP)
    context = MagicMock()

    asyncio.run(start_handler(update, context))

    update.effective_message.reply_text.assert_called_once_with(GROUP_CHAT_REJECT_MESSAGE)


def test_duplicate_polling_409_conflict_handling(monkeypatch):
    """Verify HTTP 409 Conflict triggers safe shutdown of the bot."""
    service = BotService(token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11")
    app_mock = service.build_application()

    # Find registered error handler
    assert len(app_mock.error_handlers) > 0
    error_handler = list(app_mock.error_handlers.keys())[0]

    mock_context = MagicMock()
    mock_context.error = Conflict("Conflict: terminated by other getUpdates request")
    mock_context.application = MagicMock()
    mock_context.application.updater = MagicMock()
    mock_context.application.updater.stop = AsyncMock()
    mock_context.application.stop = AsyncMock()

    asyncio.run(error_handler(None, mock_context))

    assert service.conflict_detected is True
    mock_context.application.updater.stop.assert_awaited_once()
    mock_context.application.stop.assert_awaited_once()


def test_cli_bot_help():
    """Verify `rupika bot --help` displays usage."""
    result = runner.invoke(app, ["bot", "--help"])
    assert result.exit_code == 0
    assert "Start Telegram bot" in result.output


def test_resilient_httpx_request_retries_on_stale_disconnect():
    """Verify ResilientHTTPXRequest transparently catches and retries RemoteProtocolError."""
    from stockforge.bot.service import ResilientHTTPXRequest
    from telegram.error import NetworkError

    req = ResilientHTTPXRequest(connection_pool_size=4)
    attempts = 0

    async def mock_super_do_request(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise NetworkError("httpx.RemoteProtocolError: Server disconnected without sending a response")
        return (200, b'{"ok": true, "result": []}')

    with patch("telegram.request.HTTPXRequest.do_request", side_effect=mock_super_do_request):
        status, content = asyncio.run(req.do_request("https://api.telegram.org/test", "POST"))
        assert status == 200
        assert b'"ok": true' in content
        assert attempts == 2  # Succeeded on retry without bubbling error

