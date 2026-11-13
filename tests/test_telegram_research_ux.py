"""Unit and integration tests for Telegram niche selection and explicit production plan approval (SF-021)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from telegram import Chat, Message, Update, User
from telegram.constants import ChatType

from stockforge.bot.research_handlers import (
    research_callback_handler,
    research_command,
    researchhistory_command,
    researchmode_command,
    settings_command,
)
from stockforge.db.models import (
    Base,
    Job,
    NicheSelection,
    ProductionPlan,
    ResearchCandidate,
    ResearchRun,
    TelegramUser,
)
from stockforge.queue.service import QueueService
from stockforge.research.gate import GenerationGate
from stockforge.research.planner import ProductionPlanner


@pytest.fixture
def tg_research_env(tmp_path: Path):
    """Setup isolated SQLite DB and test environment."""
    db_path = tmp_path / "test_tg_research.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    queue_service = QueueService(engine=engine)
    gate = GenerationGate(session_factory, queue_service)
    planner = ProductionPlanner(session_factory)

    # Seed admin user (telegram_id: 777777)
    with session_factory() as session:
        admin = TelegramUser(id=1, telegram_id=777777, role="admin", enabled=True)
        session.add(admin)
        session.commit()

    return session_factory, engine, queue_service, gate, planner


def build_mock_command_update(
    user_id: int,
    chat_id: int,
    args: list[str] | None = None,
) -> tuple[Update, MagicMock]:
    """Construct mock Update for command tests."""
    update = MagicMock(spec=Update)

    user = MagicMock(spec=User)
    user.id = user_id
    user.first_name = "AdminResearcher"

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
    query.message = MagicMock()
    query.message.reply_markup = None
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()

    update.effective_user = user
    update.effective_chat = chat
    update.effective_message = None
    update.callback_query = query

    context = MagicMock()
    return update, context


@pytest.mark.anyio
async def test_research_command_creates_candidates_with_sources(tg_research_env, monkeypatch):
    """Verify /research persists candidates with provenance and sends interactive selection buttons."""
    session_factory, engine, queue_service, gate, planner = tg_research_env
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "777777")

    update, context = build_mock_command_update(user_id=777777, chat_id=777777, args=["Indonesian", "Coffee"])
    await research_command(update, context, session_factory=session_factory)

    update.effective_message.reply_text.assert_called_once()
    msg_text = update.effective_message.reply_text.call_args[0][0]
    reply_markup = update.effective_message.reply_text.call_args[1]["reply_markup"]

    assert "Hasil Riset Niche: Indonesian Coffee" in msg_text
    assert "Memilih niche TIDAK otomatis memulai pembuatan gambar" in msg_text

    # Verify buttons
    buttons = [btn for row in reply_markup.inline_keyboard for btn in row]
    button_callbacks = [b.callback_data for b in buttons]
    assert any("rsh:sel:" in cb for cb in button_callbacks)
    assert any("rsh:review:" in cb for cb in button_callbacks)

    # Verify database persistence
    with session_factory() as session:
        run = session.query(ResearchRun).first()
        assert run is not None
        assert run.topic == "Indonesian Coffee"
        assert len(run.candidates) >= 2
        # Check source awareness
        assert any(c.evidence_quality == "verified" for c in run.candidates)


@pytest.mark.anyio
async def test_select_niche_does_not_approve_or_generate(tg_research_env, monkeypatch):
    """INVARIANT: Selecting a niche toggles selection in DB; it NEVER triggers generation or enqueues jobs."""
    session_factory, engine, queue_service, gate, planner = tg_research_env
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "777777")

    # Seed run and candidate
    with session_factory() as session:
        run = ResearchRun(trigger="manual", topic="Batik", status="awaiting_niche_selection")
        session.add(run)
        session.commit()
        cand = ResearchCandidate(research_run_id=run.id, niche_name="Batik Pesisir", rationale="Strong sales")
        session.add(cand)
        session.commit()
        run_id, cand_id = run.id, cand.id

    # 1. Trigger selection callback
    cb_update, cb_context = build_mock_callback_update(
        user_id=777777,
        chat_id=777777,
        data=f"rsh:sel:{run_id}:{cand_id}",
    )
    await research_callback_handler(
        cb_update,
        cb_context,
        session_factory=session_factory,
        queue_service=queue_service,
        gate=gate,
        planner=planner,
    )

    # Verify selection persisted in SQLite
    with session_factory() as session:
        sel = session.query(NicheSelection).filter_by(research_run_id=run_id, candidate_id=cand_id).first()
        assert sel is not None
        assert sel.decision == "selected"

        # Strict Invariant: Zero jobs enqueued!
        assert session.query(Job).count() == 0

    # 2. Deselect (toggle off)
    await research_callback_handler(
        cb_update,
        cb_context,
        session_factory=session_factory,
        queue_service=queue_service,
        gate=gate,
        planner=planner,
    )
    with session_factory() as session:
        sel_after = session.query(NicheSelection).filter_by(research_run_id=run_id, candidate_id=cand_id).first()
        assert sel_after is None


@pytest.mark.anyio
async def test_review_plan_and_explicit_production_approval_flow(tg_research_env, monkeypatch):
    """End-to-end review -> plan snapshot -> explicit separate approval -> job enqueued exactly once."""
    session_factory, engine, queue_service, gate, planner = tg_research_env
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "777777")

    with session_factory() as session:
        run = ResearchRun(trigger="manual", topic="Ceramics", status="awaiting_niche_selection")
        session.add(run)
        session.commit()
        c1 = ResearchCandidate(research_run_id=run.id, niche_name="Modern Ceramic Tableware", rationale="High demand")
        session.add(c1)
        session.commit()
        # Admin selects candidate
        sel = NicheSelection(research_run_id=run.id, candidate_id=c1.id, telegram_user_id=1, decision="selected")
        session.add(sel)
        session.commit()
        run_id = run.id

    # 1. Click 'Review Plan'
    rev_update, rev_context = build_mock_callback_update(user_id=777777, chat_id=777777, data=f"rsh:review:{run_id}")
    await research_callback_handler(
        rev_update,
        rev_context,
        session_factory=session_factory,
        queue_service=queue_service,
        gate=gate,
        planner=planner,
    )

    rev_update.callback_query.edit_message_text.assert_called_once()
    plan_text = rev_update.callback_query.edit_message_text.call_args[0][0]
    markup = rev_update.callback_query.edit_message_text.call_args[1]["reply_markup"]

    assert "Rencana Produksi Siap Ditinjau" in plan_text
    assert "Modern Ceramic Tableware" in plan_text
    assert "<b>Masa Berlaku Otorisasi:</b> 24 jam" in plan_text

    # Extract approve callback data from inline button
    appr_button = markup.inline_keyboard[0][0]
    appr_cb_data = appr_button.callback_data
    assert appr_cb_data.startswith("rsh:appr:")

    # Verify plan was created in database in awaiting_approval status
    with session_factory() as session:
        plan = session.query(ProductionPlan).filter_by(research_run_id=run_id).first()
        assert plan is not None
        assert plan.status == "awaiting_approval"
        # Zero jobs enqueued yet!
        assert session.query(Job).count() == 0

    # 2. Click 'Approve Production' (Separate explicit confirmation step)
    appr_update, appr_context = build_mock_callback_update(
        user_id=777777,
        chat_id=777777,
        data=appr_cb_data,
    )
    await research_callback_handler(
        appr_update,
        appr_context,
        session_factory=session_factory,
        queue_service=queue_service,
        gate=gate,
        planner=planner,
    )

    appr_update.callback_query.edit_message_text.assert_called_once()
    success_text = appr_update.callback_query.edit_message_text.call_args[0][0]
    assert "Produksi Berhasil Diotorisasi & Dijadwalkan" in success_text

    # Verify exactly 1 Job was enqueued in SQLite
    with session_factory() as session:
        jobs = session.query(Job).all()
        assert len(jobs) == 1
        assert jobs[0].type == "image_batch"
        assert jobs[0].status == "pending"

        # Plan status must be consumed
        persisted_plan = session.get(ProductionPlan, plan.id)
        assert persisted_plan.status == "consumed"
        assert persisted_plan.consumed_job_id == jobs[0].id

    # 3. Duplicate click / replay attack is safely blocked!
    appr_update2, appr_context2 = build_mock_callback_update(
        user_id=777777,
        chat_id=777777,
        data=appr_cb_data,
    )
    await research_callback_handler(
        appr_update2,
        appr_context2,
        session_factory=session_factory,
        queue_service=queue_service,
        gate=gate,
        planner=planner,
    )

    replay_text = appr_update2.callback_query.edit_message_text.call_args[0][0]
    assert "sudah pernah dieksekusi" in replay_text

    # No second job was created!
    with session_factory() as session:
        assert session.query(Job).count() == 1


@pytest.mark.anyio
async def test_researchmode_and_history_and_settings_commands(tg_research_env, monkeypatch):
    """Verify operational info commands /researchmode, /researchhistory, and /settings."""
    session_factory, engine, queue_service, gate, planner = tg_research_env
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "777777")

    # 1. /researchmode
    u1, c1 = build_mock_command_update(user_id=777777, chat_id=777777)
    await researchmode_command(u1, c1, session_factory=session_factory)
    u1.effective_message.reply_text.assert_called_once()
    t1 = u1.effective_message.reply_text.call_args[0][0]
    assert "Konfigurasi Mode Riset RUPIKA" in t1
    assert "MANUAL" in t1

    # 2. /researchhistory
    u2, c2 = build_mock_command_update(user_id=777777, chat_id=777777)
    await researchhistory_command(u2, c2, session_factory=session_factory)
    u2.effective_message.reply_text.assert_called_once()
    t2 = u2.effective_message.reply_text.call_args[0][0]
    assert "Belum ada riwayat sesi riset" in t2

    # 3. /settings
    u3, c3 = build_mock_command_update(user_id=777777, chat_id=777777)
    await settings_command(u3, c3, session_factory=session_factory)
    u3.effective_message.reply_text.assert_called_once()
    t3 = u3.effective_message.reply_text.call_args[0][0]
    assert "Pengaturan Operasional RUPIKA" in t3
    assert "REQUIRE_HUMAN_APPROVAL=True" in t3
    assert "ALLOW_ADOBE_AUTO_SUBMISSION=False" in t3
