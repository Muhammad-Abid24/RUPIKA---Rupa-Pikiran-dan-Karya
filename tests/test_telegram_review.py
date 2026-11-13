"""Acceptance tests for SF-011 Telegram review and approval."""
from __future__ import annotations

import asyncio
import io
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
import pytest
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from telegram import CallbackQuery, Chat, Message, Update, User
from telegram.constants import ChatType

from stockforge.bot.auth import DENIED_GENERIC_MESSAGE, GROUP_CHAT_REJECT_MESSAGE
from stockforge.bot.review import (
    ReviewService,
    get_review_service,
    review_callback_handler,
    review_command_handler,
)
from stockforge.db.models import Asset, AssetMetadata, Base, QcResult, Review, TelegramUser


@pytest.fixture
def review_env(tmp_path: Path):
    """Setup isolated SQLite database and storage directory."""
    db_path = tmp_path / "test_review.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    storage_dir = tmp_path / "storage"
    raw_dir = storage_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    service = ReviewService(session_factory=session_factory)
    return session_factory, service, raw_dir


def _create_test_asset_with_qc(
    session_factory,
    raw_dir: Path,
    asset_id: int = 1,
    status: str = "qc_passed",
) -> Asset:
    """Helper creating a valid asset with metadata and QC results in SQLite."""
    img_path = raw_dir / f"stock_asset_{asset_id}.jpg"
    img = Image.new("RGB", (2000, 2000), color="darkblue")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    img_path.write_bytes(buf.getvalue())

    with session_factory() as session:
        asset = Asset(
            id=asset_id,
            filename=img_path.name,
            raw_path=str(img_path),
            sha256=f"sha256_{asset_id}_{img_path.name}",
            phash=f"phash_{asset_id}",
            width=2000,
            height=2000,
            status=status,
        )
        session.add(asset)

        metadata = AssetMetadata(
            asset_id=asset_id,
            title=f"Sample Test Asset #{asset_id}",
            keywords_json=json.dumps(["nature", "mountains", "landscape", "scenic"]),
            category_id=11,  # Landscapes
            content_type="photo",
        )
        session.add(metadata)

        qc_det = QcResult(
            asset_id=asset_id,
            check_name="jpeg_integrity",
            passed=True,
            score=1.0,
            details_json=json.dumps({"format": "JPEG"}),
        )
        session.add(qc_det)

        qc_vis = QcResult(
            asset_id=asset_id,
            check_name="vision_qc",
            passed=True,
            score=0.95,
            details_json=json.dumps({"issues": [], "passed": True}),
        )
        session.add(qc_vis)

        session.commit()
        session.refresh(asset)
        return asset


def build_mock_command_update(
    user_id: int,
    chat_id: int,
    chat_type: str = ChatType.PRIVATE,
    args: list[str] | None = None,
) -> tuple[Update, MagicMock]:
    """Construct mock Update for a bot command."""
    update = MagicMock(spec=Update)

    user = MagicMock(spec=User)
    user.id = user_id
    user.first_name = "AdminUser"

    chat = MagicMock(spec=Chat)
    chat.id = chat_id
    chat.type = chat_type

    message = MagicMock(spec=Message)
    message.reply_text = AsyncMock()
    message.reply_photo = AsyncMock()
    message.reply_document = AsyncMock()

    update.effective_user = user
    update.effective_chat = chat
    update.effective_message = message
    update.callback_query = None

    context = MagicMock()
    context.bot.send_chat_action = AsyncMock()
    context.args = args or []
    return update, context



def build_mock_callback_update(
    user_id: int,
    chat_id: int,
    callback_data: str,
    chat_type: str = ChatType.PRIVATE,
    caption: str | None = "Original caption text",
) -> tuple[Update, MagicMock]:
    """Construct mock Update for an inline button callback query."""
    update = MagicMock(spec=Update)

    user = MagicMock(spec=User)
    user.id = user_id
    user.first_name = "AdminUser"

    chat = MagicMock(spec=Chat)
    chat.id = chat_id
    chat.type = chat_type

    message = MagicMock(spec=Message)
    message.caption = caption
    message.text = None if caption else "Original text"
    message.reply_document = AsyncMock()
    message.reply_text = AsyncMock()

    query = MagicMock(spec=CallbackQuery)
    query.data = callback_data
    query.message = message
    query.answer = AsyncMock()
    query.edit_message_caption = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()

    update.effective_user = user
    update.effective_chat = chat
    update.effective_message = message
    update.callback_query = query

    context = MagicMock()
    return update, context


# ---------------------------------------------------------------------------
# Test 1: Non-admin cannot access review command or callback
# ---------------------------------------------------------------------------
def test_non_admin_cannot_review(review_env, monkeypatch):
    session_factory, service, raw_dir = review_env
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "999")  # Admin is 999 only
    _create_test_asset_with_qc(session_factory, raw_dir, asset_id=1)

    # Injected service
    monkeypatch.setattr("stockforge.bot.review.get_review_service", lambda: service)

    # Non-admin user (id=123) attempts command
    update, context = build_mock_command_update(user_id=123, chat_id=123)
    asyncio.run(review_command_handler(update, context))

    update.effective_message.reply_text.assert_called_once_with(DENIED_GENERIC_MESSAGE)

    # Non-admin user attempts callback click
    token = service.generate_token(1)
    cb_update, cb_context = build_mock_callback_update(
        user_id=123,
        chat_id=123,
        callback_data=f"rev:app:1:{token}",
    )
    asyncio.run(review_callback_handler(cb_update, cb_context))

    cb_update.callback_query.answer.assert_called_once_with(
        DENIED_GENERIC_MESSAGE, show_alert=True
    )

    # Verify no review record was created in database
    with session_factory() as session:
        revs = session.query(Review).all()
        assert len(revs) == 0


# ---------------------------------------------------------------------------
# Test 2: Group chat interaction rejected
# ---------------------------------------------------------------------------
def test_group_chat_rejected(review_env, monkeypatch):
    session_factory, service, raw_dir = review_env
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "1001")
    monkeypatch.setattr("stockforge.bot.review.get_review_service", lambda: service)

    # Admin attempts in group chat
    update, context = build_mock_command_update(
        user_id=1001, chat_id=-10012345, chat_type=ChatType.GROUP
    )
    asyncio.run(review_command_handler(update, context))

    update.effective_message.reply_text.assert_called_once_with(GROUP_CHAT_REJECT_MESSAGE)


# ---------------------------------------------------------------------------
# Test 3: Admin review command displays preview and inline keyboard
# ---------------------------------------------------------------------------
def test_admin_review_command_presents_preview(review_env, monkeypatch):
    session_factory, service, raw_dir = review_env
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "1001")
    monkeypatch.setattr("stockforge.bot.review.get_review_service", lambda: service)

    asset = _create_test_asset_with_qc(session_factory, raw_dir, asset_id=42)

    update, context = build_mock_command_update(user_id=1001, chat_id=1001, args=["42"])
    asyncio.run(review_command_handler(update, context))

    # Should send photo since raw_path image exists
    assert update.effective_message.reply_photo.called
    args, kwargs = update.effective_message.reply_photo.call_args
    caption = kwargs.get("caption", "")
    assert "Review Aset Stok #42" in caption
    assert "2000x2000 px" in caption
    assert "Landscapes" in caption
    assert "reply_markup" in kwargs

    # Verify inline keyboard buttons
    keyboard = kwargs["reply_markup"].inline_keyboard
    actions = [btn.callback_data for row in keyboard for btn in row]
    assert any("rev:app:42:" in a for a in actions)
    assert any("rev:rej:42:" in a for a in actions)
    assert any("rev:reg:42:" in a for a in actions)
    assert any("rev:res:42:" in a for a in actions)


# ---------------------------------------------------------------------------
# Test 4: Approval state transition and metadata review timestamp update
# ---------------------------------------------------------------------------
def test_admin_approval_and_persistence(review_env, monkeypatch):
    session_factory, service, raw_dir = review_env
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "1001")
    monkeypatch.setattr("stockforge.bot.review.get_review_service", lambda: service)

    asset = _create_test_asset_with_qc(session_factory, raw_dir, asset_id=10)
    token = service.generate_token(10)

    cb_update, cb_context = build_mock_callback_update(
        user_id=1001,
        chat_id=1001,
        callback_data=f"rev:app:10:{token}",
    )
    asyncio.run(review_callback_handler(cb_update, cb_context))

    # Caption updated with approval note and buttons removed
    cb_update.callback_query.edit_message_caption.assert_called_once()
    _, kwargs = cb_update.callback_query.edit_message_caption.call_args
    assert "APPROVED" in kwargs["caption"]
    assert kwargs["reply_markup"] is None

    # Check database status
    with session_factory() as session:
        updated_asset = session.get(Asset, 10)
        assert updated_asset.status == "approved"
        assert updated_asset.metadata_entry.metadata_reviewed_at is not None

        review_rec = session.query(Review).filter_by(asset_id=10).first()
        assert review_rec is not None
        assert review_rec.decision == "approved"
        user_rec = session.get(TelegramUser, review_rec.telegram_user_id)
        assert user_rec is not None
        assert user_rec.telegram_id == 1001



# ---------------------------------------------------------------------------
# Test 5: Double-click protection (CRITICAL ACCEPTANCE CRITERION)
# ---------------------------------------------------------------------------
def test_double_click_cannot_approve_twice(review_env, monkeypatch):
    session_factory, service, raw_dir = review_env
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "1001")
    monkeypatch.setattr("stockforge.bot.review.get_review_service", lambda: service)

    asset = _create_test_asset_with_qc(session_factory, raw_dir, asset_id=20)
    token = service.generate_token(20)

    # 1. First click: Approval succeeds
    cb_update_1, cb_context_1 = build_mock_callback_update(
        user_id=1001,
        chat_id=1001,
        callback_data=f"rev:app:20:{token}",
    )
    asyncio.run(review_callback_handler(cb_update_1, cb_context_1))

    with session_factory() as session:
        revs_after_1 = session.query(Review).filter_by(asset_id=20).all()
        assert len(revs_after_1) == 1

    # 2. Second rapid click on same button
    cb_update_2, cb_context_2 = build_mock_callback_update(
        user_id=1001,
        chat_id=1001,
        callback_data=f"rev:app:20:{token}",
    )
    asyncio.run(review_callback_handler(cb_update_2, cb_context_2))

    # Verify second click answered with an alert warning and removed markup
    answer_calls = cb_update_2.callback_query.answer.call_args_list
    assert any("Aksi diabaikan" in str(call) for call in answer_calls)
    cb_update_2.callback_query.edit_message_reply_markup.assert_called_once_with(reply_markup=None)

    # Verify that NO duplicate row was inserted into reviews table
    with session_factory() as session:
        revs_after_2 = session.query(Review).filter_by(asset_id=20).all()
        assert len(revs_after_2) == 1


# ---------------------------------------------------------------------------
# Test 6: Rejection and Regeneration actions
# ---------------------------------------------------------------------------
def test_reject_and_regenerate_actions(review_env, monkeypatch):
    session_factory, service, raw_dir = review_env
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "1001")
    monkeypatch.setattr("stockforge.bot.review.get_review_service", lambda: service)

    # Test Rejection
    _create_test_asset_with_qc(session_factory, raw_dir, asset_id=30)
    token_30 = service.generate_token(30)
    cb_rej, cb_ctx_rej = build_mock_callback_update(
        user_id=1001, chat_id=1001, callback_data=f"rev:rej:30:{token_30}"
    )
    asyncio.run(review_callback_handler(cb_rej, cb_ctx_rej))

    with session_factory() as session:
        asset_30 = session.get(Asset, 30)
        assert asset_30.status == "rejected"
        rev_30 = session.query(Review).filter_by(asset_id=30).first()
        assert rev_30.decision == "rejected"

    # Test Regeneration
    _create_test_asset_with_qc(session_factory, raw_dir, asset_id=31)
    token_31 = service.generate_token(31)
    cb_reg, cb_ctx_reg = build_mock_callback_update(
        user_id=1001, chat_id=1001, callback_data=f"rev:reg:31:{token_31}"
    )
    asyncio.run(review_callback_handler(cb_reg, cb_ctx_reg))

    with session_factory() as session:
        asset_31 = session.get(Asset, 31)
        assert asset_31.status == "regenerating"
        rev_31 = session.query(Review).filter_by(asset_id=31).first()
        assert rev_31.decision == "regenerate"

        # Verify new regeneration Job, JobEvent, and JobItem were properly enqueued
        from stockforge.db.models import Job, JobEvent, JobItem
        regen_job = session.query(Job).filter(Job.type == "image_batch").order_by(Job.id.desc()).first()
        assert regen_job is not None
        assert regen_job.status == "pending"
        assert "regen_31_" in regen_job.operation_id

        regen_ev = session.query(JobEvent).filter(JobEvent.job_id == regen_job.id).first()
        assert regen_ev is not None
        assert regen_ev.event_type == "regeneration_request"
        pdata = json.loads(regen_ev.payload_json)
        assert pdata["original_asset_id"] == 31
        assert "prompt" in pdata

        items = session.query(JobItem).filter(JobItem.job_id == regen_job.id).all()
        assert len(items) == 1
        assert items[0].stage == "generating"


# ---------------------------------------------------------------------------
# Test 7: Callback TTL Expiration
# ---------------------------------------------------------------------------
def test_callback_ttl_expiration(review_env, monkeypatch):
    session_factory, service, raw_dir = review_env
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "1001")
    monkeypatch.setattr("stockforge.bot.review.get_review_service", lambda: service)

    _create_test_asset_with_qc(session_factory, raw_dir, asset_id=40)
    # Stale token from 2 days ago
    stale_ts = int(time.time()) - 172800
    stale_token = f"40_{stale_ts}"

    cb_update, cb_context = build_mock_callback_update(
        user_id=1001,
        chat_id=1001,
        callback_data=f"rev:app:40:{stale_token}",
    )
    asyncio.run(review_callback_handler(cb_update, cb_context))

    # Alert displayed and buttons removed
    answer_calls = cb_update.callback_query.answer.call_args_list
    assert any("TTL habis" in str(call) for call in answer_calls)
    cb_update.callback_query.edit_message_reply_markup.assert_called_once_with(reply_markup=None)

    # Status remains unchanged
    with session_factory() as session:
        asset_40 = session.get(Asset, 40)
        assert asset_40.status == "qc_passed"


# ---------------------------------------------------------------------------
# Test 8: Request full-resolution image document
# ---------------------------------------------------------------------------
def test_request_full_resolution_image(review_env, monkeypatch):
    session_factory, service, raw_dir = review_env
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "1001")
    monkeypatch.setattr("stockforge.bot.review.get_review_service", lambda: service)

    asset = _create_test_asset_with_qc(session_factory, raw_dir, asset_id=50)
    token = service.generate_token(50)

    cb_update, cb_context = build_mock_callback_update(
        user_id=1001,
        chat_id=1001,
        callback_data=f"rev:res:50:{token}",
    )
    asyncio.run(review_callback_handler(cb_update, cb_context))

    cb_update.callback_query.message.reply_document.assert_called_once()
    _, kwargs = cb_update.callback_query.message.reply_document.call_args
    assert kwargs["filename"] == asset.filename
    assert "2000x2000 px" in kwargs["caption"]


def test_get_review_service_default_initialization(tmp_path: Path, monkeypatch):
    """Verify get_review_service initializes cleanly with settings database_url string."""
    import stockforge.bot.review as review_mod
    review_mod._default_review_service = None

    db_path = tmp_path / "default_init.db"
    db_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", db_url)

    from stockforge.db.models import Base
    from stockforge.db.session import create_db_engine
    Base.metadata.create_all(create_db_engine(db_url))

    # Call get_review_service with None, ensuring it builds sessionmaker without error
    srv = get_review_service()
    assert srv is not None
    # Verify DB query works through the created sessionmaker without AttributeError: 'str' object has no attribute 'connect'
    assert srv.get_pending_asset(1) is None


def test_review_when_generation_in_progress(review_env, monkeypatch):
    """Verify /review notifies operator when image generation job is currently in progress."""
    session_factory, service, raw_dir = review_env
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "1001")
    monkeypatch.setattr("stockforge.bot.review.get_review_service", lambda: service)

    from stockforge.db.models import Job

    # Create an active running job with no finished assets
    with session_factory() as session:
        job = Job(
            id=7,
            type="image_batch",
            status="running",
            operation_id="op_test_running_7",
        )
        session.add(job)
        session.commit()

    # Admin calls /review without arguments
    update, context = build_mock_command_update(user_id=1001, chat_id=1001, args=[])
    asyncio.run(review_command_handler(update, context))

    assert update.effective_message.reply_text.called
    text = update.effective_message.reply_text.call_args[0][0]
    assert "Proses generate gambar belum selesai" in text
    assert "/status 7" in text

    # Admin calls /review 7 (referencing batch 7)
    update7, context7 = build_mock_command_update(user_id=1001, chat_id=1001, args=["7"])
    asyncio.run(review_command_handler(update7, context7))

    assert update7.effective_message.reply_text.called
    text7 = update7.effective_message.reply_text.call_args[0][0]
    assert "Proses generate gambar belum selesai" in text7
    assert "/status 7" in text7


def test_review_by_job_id(review_env, monkeypatch):
    """Verify /review <job_id> resolves the asset created by that batch job."""
    session_factory, service, raw_dir = review_env
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "1001")
    monkeypatch.setattr("stockforge.bot.review.get_review_service", lambda: service)

    from stockforge.db.models import Job

    with session_factory() as session:
        job = Job(
            id=12,
            type="image_batch",
            status="completed",
            operation_id="op_test_12",
        )
        session.add(job)
        session.commit()

    # Asset ID is 99, but its job_id is 12
    asset = _create_test_asset_with_qc(session_factory, raw_dir, asset_id=99)
    with session_factory() as session:
        a = session.get(Asset, 99)
        a.job_id = 12
        session.commit()

    # Admin calls /review 12
    update, context = build_mock_command_update(user_id=1001, chat_id=1001, args=["12"])
    asyncio.run(review_command_handler(update, context))

    assert update.effective_message.reply_photo.called
    caption = update.effective_message.reply_photo.call_args[1].get("caption", "")
    assert "Review Aset Stok #99" in caption


def test_review_sends_typing_action(review_env, monkeypatch):
    """Verify that /review triggers Telegram typing chat action immediately."""
    session_factory, service, raw_dir = review_env
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "1001")
    monkeypatch.setattr("stockforge.bot.review.get_review_service", lambda: service)

    update, context = build_mock_command_update(user_id=1001, chat_id=1001)
    asyncio.run(review_command_handler(update, context))

    from telegram.constants import ChatAction
    context.bot.send_chat_action.assert_called_once_with(
        chat_id=1001,
        action=ChatAction.TYPING,
    )


def test_review_displays_error_to_user_on_failure(review_env, monkeypatch):
    """Verify that when an error occurs during review, the error cause is displayed to user."""
    session_factory, service, raw_dir = review_env
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "1001")

    # Mock service that raises an exception
    faulty_service = MagicMock()
    faulty_service.get_pending_asset.side_effect = RuntimeError("Database disk I/O failure")
    monkeypatch.setattr("stockforge.bot.review.get_review_service", lambda: faulty_service)

    update, context = build_mock_command_update(user_id=1001, chat_id=1001)
    asyncio.run(review_command_handler(update, context))

    # Error must be sent to user so they are aware
    assert update.effective_message.reply_text.called
    error_sent = update.effective_message.reply_text.call_args[0][0]
    assert "Terjadi galat saat memproses review aset" in error_sent
    assert "Database disk I/O failure" in error_sent


def test_review_exports_txt_for_lengthy_metadata(review_env, monkeypatch):
    """Verify that lengthy metadata (>6 keywords) exports remainder as .txt attachment."""
    session_factory, service, raw_dir = review_env
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "1001")
    monkeypatch.setattr("stockforge.bot.review.get_review_service", lambda: service)

    # Asset with 20 keywords
    asset = _create_test_asset_with_qc(session_factory, raw_dir, asset_id=77)
    many_keywords = [f"keyword_{i}" for i in range(1, 21)]
    with session_factory() as s:
        meta = s.query(AssetMetadata).filter_by(asset_id=77).first()
        meta.keywords_json = json.dumps(many_keywords)
        s.commit()

    update, context = build_mock_command_update(user_id=1001, chat_id=1001, args=["77"])
    asyncio.run(review_command_handler(update, context))

    # 1. Preview photo is sent
    assert update.effective_message.reply_photo.called
    caption = update.effective_message.reply_photo.call_args[1].get("caption", "")
    assert "Review Aset Stok #77" in caption
    assert ".txt" in caption  # Mention of .txt attachment in caption

    # 2. Document is attached with full .txt report
    assert update.effective_message.reply_document.called
    doc_kwargs = update.effective_message.reply_document.call_args[1]
    assert doc_kwargs["filename"] == "metadata_asset_77.txt"
    doc_io = doc_kwargs["document"]
    txt_content = doc_io.getvalue().decode("utf-8")
    assert "RUPIKA / STOCKFORGE - METADATA & QUALITY CONTROL REPORT" in txt_content
    assert "Total Keywords  : 20" in txt_content
    assert "keyword_20" in txt_content  # Complete list included


def test_regeneration_flow_executes_worker_and_produces_new_asset(review_env, monkeypatch):
    """Verify full end-to-end flow: user selects regenerate, job enqueued, worker processes it."""
    session_factory, service, raw_dir = review_env
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "1001")
    monkeypatch.setattr("stockforge.bot.review.get_review_service", lambda: service)

    # 1. Create asset #88
    _create_test_asset_with_qc(session_factory, raw_dir, asset_id=88)
    token = service.generate_token(88)

    cb_reg, cb_ctx_reg = build_mock_callback_update(
        user_id=1001, chat_id=1001, callback_data=f"rev:reg:88:{token}"
    )
    asyncio.run(review_callback_handler(cb_reg, cb_ctx_reg))

    with session_factory() as s:
        asset_88 = s.get(Asset, 88)
        assert asset_88.status == "regenerating"

        from stockforge.db.models import Job, JobEvent
        new_job = s.query(Job).filter(Job.type == "image_batch").order_by(Job.id.desc()).first()
        assert new_job is not None
        assert new_job.status == "pending"

        ev = s.query(JobEvent).filter(JobEvent.job_id == new_job.id).first()
        assert ev.event_type == "regeneration_request"

    # 2. Worker executes new job
    from stockforge.queue.service import QueueService
    from stockforge.queue.worker import Worker

    engine = session_factory().get_bind()
    qs = QueueService(engine)
    worker = Worker(queue_service=qs, worker_id="test-worker")
    monkeypatch.setattr(worker, "_post_telegram", lambda *args, **kwargs: None)

    def mock_processor(job_id: int, item_index: int) -> int:
        new_asset = _create_test_asset_with_qc(session_factory, raw_dir, asset_id=89, status="qc_passed")
        return new_asset.id

    processed_job_id = worker.run_once(item_processor=mock_processor)
    assert processed_job_id == new_job.id

    with session_factory() as s:
        finished_job = s.get(Job, new_job.id)
        assert finished_job.status == "completed"
        new_asset = s.get(Asset, 89)
        assert new_asset.status == "qc_passed"


def test_regeneration_improves_prompt_for_anatomy_and_reserves_budget(review_env, monkeypatch):
    """Verify that when regenerating an asset with anatomy defects, prompt is improved and budget is reserved."""
    from stockforge.db.models import utc_now
    session_factory, service, raw_dir = review_env
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "1001")
    monkeypatch.setattr("stockforge.bot.review.get_review_service", lambda: service)

    # Create asset #99 with anatomy defects in QC
    _create_test_asset_with_qc(session_factory, raw_dir, asset_id=99, status="qc_failed")
    with session_factory() as s:
        qc_anatomy = QcResult(
            asset_id=99,
            check_name="vision_qc",
            passed=False,
            score=0.72,
            details_json=json.dumps({
                "passed": False,
                "confidence": 0.72,
                "model": "rupika-qc",
                "anatomy": {
                    "has_humans": True,
                    "hands_and_fingers_defect": True,
                    "face_defect": True,
                    "interaction_defect": False,
                    "details": "Malformed left hand with 6 fingers and asymmetric pupils"
                },
                "issues": ["anatomical_distortion_fingers", "extra digit on left hand"],
                "notes": "Hand has six fingers with distorted knuckles",
            }),
            checked_at=utc_now(),
        )
        s.add(qc_anatomy)
        s.commit()

    token = service.generate_token(99)
    cb_reg, cb_ctx_reg = build_mock_callback_update(
        user_id=1001, chat_id=1001, callback_data=f"rev:reg:99:{token}"
    )
    asyncio.run(review_callback_handler(cb_reg, cb_ctx_reg))

    with session_factory() as s:
        from stockforge.db.models import BudgetLedger, Job, JobEvent
        new_job = s.query(Job).filter(Job.type == "image_batch").order_by(Job.id.desc()).first()
        assert new_job is not None
        assert new_job.reserved_usd_cents == 5

        # Verify BudgetLedger reservation entry
        ledger = s.query(BudgetLedger).filter(BudgetLedger.job_id == new_job.id, BudgetLedger.kind == "reserve").first()
        assert ledger is not None
        assert ledger.amount_cents == 5

        # Verify JobEvent payload contains improved prompt with anatomy clarity
        ev = s.query(JobEvent).filter(JobEvent.job_id == new_job.id, JobEvent.event_type == "regeneration_request").first()
        assert ev is not None
        payload = json.loads(ev.payload_json)
        improved_prompt = payload["prompt"]
        assert "5 distinct fingers" in improved_prompt or "anatomically correct" in improved_prompt
        assert "facial" in improved_prompt or "symmetry" in improved_prompt




