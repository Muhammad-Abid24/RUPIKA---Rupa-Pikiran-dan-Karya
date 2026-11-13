"""Unit and integration tests for Telegram live progress, failure alerts, and retry controls (SF-019)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from telegram import Chat, Message, Update, User
from telegram.constants import ChatType

from stockforge.bot.progress import (
    TelegramProgressManager,
    compute_render_hash,
    format_progress_message,
)
from stockforge.bot.progress_handlers import (
    failed_command,
    jobs_command,
    providers_command,
    retry_callback_handler,
    retry_command,
    status_command,
)
from stockforge.config import get_settings
from stockforge.db.models import Base, Job, JobItem, TelegramProgressMessage, TelegramUser
from stockforge.errors.codes import ErrorCode, NormalizedProviderError
from stockforge.providers.health import ProviderHealthTracker, ProviderStatus
from stockforge.queue.service import QueueService
from stockforge.queue.tracker import BatchProgressSummary, ItemProgressTracker


@pytest.fixture
def progress_env(tmp_path: Path):
    """Setup isolated SQLite DB and test environment."""
    db_path = tmp_path / "test_progress_alerts.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    queue_service = QueueService(engine=engine)
    tracker = ItemProgressTracker(session_factory)
    progress_manager = TelegramProgressManager(session_factory, throttle_seconds=5.0)

    # Register admin user (Telegram ID 123456)
    with session_factory() as session:
        admin_user = TelegramUser(
            id=1,
            telegram_id=123456,
            role="admin",
            enabled=True,
        )
        session.add(admin_user)
        session.commit()

    return session_factory, engine, queue_service, tracker, progress_manager


def build_mock_command_update(
    user_id: int,
    chat_id: int,
    args: list[str] | None = None,
) -> tuple[Update, MagicMock]:
    """Construct mock Update for bot command tests."""
    update = MagicMock(spec=Update)

    user = MagicMock(spec=User)
    user.id = user_id
    user.first_name = "TestAdmin"

    chat = MagicMock(spec=Chat)
    chat.id = chat_id
    chat.type = ChatType.PRIVATE

    message = MagicMock(spec=Message)
    message.reply_text = AsyncMock()

    update.effective_user = user
    update.effective_chat = chat
    update.effective_message = message
    update.callback_query = None

    context = MagicMock()
    context.args = args or []
    return update, context


def build_mock_callback_update(
    user_id: int,
    chat_id: int,
    data: str,
) -> tuple[Update, MagicMock]:
    """Construct mock Update for inline button callback tests."""
    update = MagicMock(spec=Update)

    user = MagicMock(spec=User)
    user.id = user_id

    chat = MagicMock(spec=Chat)
    chat.id = chat_id
    chat.type = ChatType.PRIVATE

    query = MagicMock()
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    update.effective_user = user
    update.effective_chat = chat
    update.effective_message = None
    update.callback_query = query

    context = MagicMock()
    return update, context


@pytest.mark.anyio
async def test_render_hash_and_throttling_skips_redundant_edits(progress_env):
    """Ensure render hashing and throttling prevent spamming Telegram edit API calls."""
    session_factory, engine, queue_service, tracker, progress_manager = progress_env

    job = queue_service.enqueue_job(job_type="image_batch", operation_id="op_render_test")
    tracker.initialize_job_items(job_id=job.id, total_items=10, initial_stage="generating")

    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=901))
    bot.edit_message_text = AsyncMock()

    summary1 = tracker.get_batch_progress(job.id)

    # 1. First send -> calls send_message
    msg_id1 = await progress_manager.update_progress(
        bot=bot,
        chat_id=123456,
        summary=summary1,
        job_type="image_batch",
    )
    assert msg_id1 == 901
    assert bot.send_message.call_count == 1
    assert bot.edit_message_text.call_count == 0

    # 2. Immediate update with identical content -> skipped by render hash
    msg_id2 = await progress_manager.update_progress(
        bot=bot,
        chat_id=123456,
        summary=summary1,
        job_type="image_batch",
    )
    assert msg_id2 == 901
    assert bot.edit_message_text.call_count == 0  # No API call made!

    # 3. Change state (item 1 completes)
    tracker.update_item_stage(job.id, item_index=1, stage="generating", status="completed")
    summary2 = tracker.get_batch_progress(job.id)

    # Immediate call without force -> throttled (< 5.0s elapsed)
    msg_id3 = await progress_manager.update_progress(
        bot=bot,
        chat_id=123456,
        summary=summary2,
        force=False,
    )
    assert msg_id3 == 901
    assert bot.edit_message_text.call_count == 0  # Throttled!

    # 4. Call with force=True -> calls edit_message_text
    msg_id4 = await progress_manager.update_progress(
        bot=bot,
        chat_id=123456,
        summary=summary2,
        force=True,
    )
    assert msg_id4 == 901
    assert bot.edit_message_text.call_count == 1


@pytest.mark.anyio
async def test_progress_message_recovery_when_deleted(progress_env):
    """If original progress message was deleted, manager catches error and sends a new one."""
    session_factory, engine, queue_service, tracker, progress_manager = progress_env

    job = queue_service.enqueue_job(job_type="image_batch", operation_id="op_recovery_test")
    tracker.initialize_job_items(job_id=job.id, total_items=5, initial_stage="generating")
    summary = tracker.get_batch_progress(job.id)

    bot = MagicMock()
    # First send gives ID 1001
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=1001))
    await progress_manager.update_progress(bot=bot, chat_id=123456, summary=summary)

    # Next update: edit_message_text fails simulating deleted message
    bot.edit_message_text = AsyncMock(side_effect=Exception("Bad Request: message to edit not found"))
    # Second send gives ID 1002
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=1002))

    tracker.update_item_stage(job.id, item_index=1, stage="generating", status="completed")
    new_summary = tracker.get_batch_progress(job.id)

    recovered_msg_id = await progress_manager.update_progress(
        bot=bot,
        chat_id=123456,
        summary=new_summary,
        force=True,
    )
    assert recovered_msg_id == 1002

    # Verify persisted message ID updated in SQLite
    with session_factory() as session:
        rec = session.get(TelegramProgressMessage, job.id)
        assert rec is not None
        assert rec.message_id == 1002


def test_accurate_progress_formatting_invariant():
    """Batch with 6 completed + 1 processing + 3 pending must strictly calculate 60.0%."""
    summary = BatchProgressSummary(
        job_id=42,
        total_items=10,
        completed_count=6,
        processing_count=1,
        pending_count=3,
        failed_count=0,
        cancelled_count=0,
        reconciliation_count=0,
        current_item_index=7,
        current_stage="generating",
        success_rate_percent=60.0,
        is_terminal=False,
        summary_text="Batch #42: 6/10 completed (60.0%)",
    )

    msg = format_progress_message(
        summary=summary,
        job_type="image_batch",
        provider="openai",
        model="dall-e-3",
        estimated_cost_cents=40,
        eta_seconds=75.0,
    )

    assert "Progress Batch #42" in msg
    assert "60.0%" in msg
    assert "70.0%" not in msg  # Invariant!
    assert "• <b>Gambar Aktif:</b> <code>7/10</code>" in msg
    assert "• <b>Engine:</b> <code>openai (dall-e-3)</code>" in msg
    assert "$0.40 USD" in msg
    assert "~1m 15s" in msg


@pytest.mark.anyio
async def test_status_command_authorized_and_unauthorized(progress_env, monkeypatch):
    """Verify /status command displays batch progress for admin and denies unauthorized."""
    session_factory, engine, queue_service, tracker, progress_manager = progress_env
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "123456")

    job = queue_service.enqueue_job(job_type="image_batch", operation_id="op_status_cmd")
    tracker.initialize_job_items(job_id=job.id, total_items=5, initial_stage="generating")

    # 1. Admin request
    update, context = build_mock_command_update(user_id=123456, chat_id=123456, args=[str(job.id)])
    await status_command(update, context, session_factory=session_factory)

    update.effective_message.reply_text.assert_called_once()
    call_text = update.effective_message.reply_text.call_args[0][0]
    assert f"Progress Batch #{job.id}" in call_text
    assert "<b>Berhasil:</b> <code>0</code> / 5" in call_text

    # 2. Unauthorized user request (ID 999999)
    unauth_update, unauth_context = build_mock_command_update(user_id=999999, chat_id=999999, args=[str(job.id)])
    await status_command(unauth_update, unauth_context, session_factory=session_factory)

    unauth_text = unauth_update.effective_message.reply_text.call_args[0][0]
    assert "Akses ditolak" in unauth_text


@pytest.mark.anyio
async def test_failed_command_reports_diagnostic_details(progress_env, monkeypatch):
    """Verify /failed <batch_id> reports failed items with sanitized error text."""
    session_factory, engine, queue_service, tracker, progress_manager = progress_env
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "123456")

    job = queue_service.enqueue_job(job_type="image_batch", operation_id="op_failed_cmd")
    tracker.initialize_job_items(job_id=job.id, total_items=3, initial_stage="generating")
    tracker.update_item_stage(
        job_id=job.id,
        item_index=2,
        stage="generating",
        status="failed",
        last_error="AllFallbacksExhausted: Provider 500 with Bearer sk-secret12345",
    )

    update, context = build_mock_command_update(user_id=123456, chat_id=123456, args=[str(job.id)])
    await failed_command(update, context, session_factory=session_factory)

    update.effective_message.reply_text.assert_called_once()
    call_text = update.effective_message.reply_text.call_args[0][0]
    assert f"Laporan Kegagalan Batch #{job.id}" in call_text
    assert "Gambar #2" in call_text
    assert "sk-secret12345" not in call_text  # Sanitized!
    assert f"/retry {job.id}" in call_text


@pytest.mark.anyio
async def test_retry_command_spend_confirmation_and_isolation(progress_env, monkeypatch):
    """Verify /retry presents confirmation and strictly resets ONLY failed items upon approval."""
    session_factory, engine, queue_service, tracker, progress_manager = progress_env
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "123456")

    job = queue_service.enqueue_job(job_type="image_batch", operation_id="op_retry_flow")
    tracker.initialize_job_items(job_id=job.id, total_items=3, initial_stage="generating")

    # Item 1: completed
    tracker.update_item_stage(job.id, item_index=1, stage="generating", status="completed")
    # Item 2: failed
    tracker.update_item_stage(job.id, item_index=2, stage="generating", status="failed", last_error="Network timeout")
    # Item 3: completed
    tracker.update_item_stage(job.id, item_index=3, stage="generating", status="completed")

    # 1. Trigger /retry command
    cmd_update, cmd_context = build_mock_command_update(user_id=123456, chat_id=123456, args=[str(job.id)])
    await retry_command(cmd_update, cmd_context, session_factory=session_factory, estimated_cost_per_item_cents=5)

    cmd_update.effective_message.reply_text.assert_called_once()
    prompt_text = cmd_update.effective_message.reply_text.call_args[0][0]
    assert "<b>Item Gagal:</b> 1 item" in prompt_text
    assert "$0.05 USD" in prompt_text
    assert "Item yang sudah berhasil <b>TIDAK</b> akan diulang" in prompt_text

    # 2. Trigger retry_confirm callback
    cb_update, cb_context = build_mock_callback_update(
        user_id=123456,
        chat_id=123456,
        data=f"retry_confirm:{job.id}",
    )
    await retry_callback_handler(cb_update, cb_context, session_factory=session_factory)

    cb_update.callback_query.edit_message_text.assert_called_once()
    conf_text = cb_update.callback_query.edit_message_text.call_args[0][0]
    assert "<b>Retry Disetujui:</b> 1 item gagal" in conf_text

    # 3. Verify SQLite state invariants
    with session_factory() as session:
        items = session.query(JobItem).filter_by(job_id=job.id).order_by(JobItem.item_index).all()
        # Item 1 and Item 3 MUST remain completed! Never rerun completed items!
        assert items[0].status == "completed"
        # Item 2 was reset to pending
        assert items[1].status == "pending"
        assert items[1].last_error is None
        assert items[2].status == "completed"

        # Job status was reset to pending
        persisted_job = session.get(Job, job.id)
        assert persisted_job.status == "pending"


@pytest.mark.anyio
async def test_providers_command_health_overview(progress_env, monkeypatch):
    """Verify /providers displays provider availability and remaining cooldown."""
    session_factory, engine, queue_service, tracker, progress_manager = progress_env
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "123456")

    health_tracker = ProviderHealthTracker()
    # Trigger cooldown on stability
    norm_err = NormalizedProviderError(
        code=ErrorCode.INSUFFICIENT_CREDITS,
        retryable=False,
        fallback_allowed=True,
        severity="critical",
        action="cooldown_provider",
        retry_after_seconds=120.0,
    )
    health_tracker.record_error("stability", norm_err)

    update, context = build_mock_command_update(user_id=123456, chat_id=123456)
    await providers_command(update, context, health_tracker=health_tracker)

    update.effective_message.reply_text.assert_called_once()
    text = update.effective_message.reply_text.call_args[0][0]
    assert "Status Kesehatan Provider AI" in text
    assert "<code>OPENAI</code>: 🟢 <b>HEALTHY</b>" in text
    assert "<code>STABILITY</code>: 🟡 <b>COOLDOWN</b>" in text
    assert "Cooldown sisa:" in text


@pytest.mark.anyio
async def test_jobs_command_authorized_and_lists_jobs(progress_env, monkeypatch):
    """Verify /jobs command displays list of recent jobs and queue status."""
    session_factory, engine, queue_service, tracker, progress_manager = progress_env
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "123456")

    # 1. Empty state
    update_empty, ctx_empty = build_mock_command_update(user_id=123456, chat_id=123456)
    await jobs_command(update_empty, ctx_empty, session_factory=session_factory)
    assert update_empty.effective_message.reply_text.called
    empty_text = update_empty.effective_message.reply_text.call_args[0][0]
    assert "Belum ada antrean pekerjaan" in empty_text

    # 2. Populate jobs
    j1 = queue_service.enqueue_job(job_type="image_batch", operation_id="op_jobs_1")
    j2 = queue_service.enqueue_job(job_type="research", operation_id="op_jobs_2")
    with session_factory() as session:
        job1 = session.get(Job, j1.id)
        job1.status = "completed"
        session.commit()

    update, context = build_mock_command_update(user_id=123456, chat_id=123456)
    await jobs_command(update, context, session_factory=session_factory)

    assert update.effective_message.reply_text.called
    text = update.effective_message.reply_text.call_args[0][0]
    assert "Daftar Pekerjaan & Antrean Batch" in text
    assert f"Batch #{j1.id}" in text
    assert f"Batch #{j2.id}" in text
    assert "/status" in text
    assert "/review" in text

    # 3. Unauthorized request
    unauth_update, unauth_ctx = build_mock_command_update(user_id=999999, chat_id=999999)
    await jobs_command(unauth_update, unauth_ctx, session_factory=session_factory)
    unauth_text = unauth_update.effective_message.reply_text.call_args[0][0]
    assert "Akses ditolak" in unauth_text
