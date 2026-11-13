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
