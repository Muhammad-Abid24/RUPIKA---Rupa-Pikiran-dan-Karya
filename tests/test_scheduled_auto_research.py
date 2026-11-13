"""Unit and integration tests for opt-in scheduled Auto Research and budget guard (SF-022)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import zoneinfo

from stockforge.db.models import (
    Base,
    Job,
    ResearchCandidate,
    ResearchRun,
    ResearchSetting,
    TelegramOutbox,
    TelegramUser,
)
from stockforge.ops.outbox import OutboxService
from stockforge.research import (
    AutoResearchScheduler,
    ResearchSettingsManager,
    format_schedule_key,
    is_cron_match,
)


@pytest.fixture
def auto_research_env(tmp_path: Path):
    """Setup isolated SQLite DB and AutoResearchScheduler."""
    db_path = tmp_path / "test_auto_research.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    settings_mgr = ResearchSettingsManager(session_factory)
    outbox_svc = OutboxService(session_factory)
    scheduler = AutoResearchScheduler(
        session_factory=session_factory,
        settings_manager=settings_mgr,
        outbox_service=outbox_svc,
        admin_chat_id=555555,
    )

    # Seed admin user (id=1, telegram_id=555555)
    with session_factory() as session:
        admin = TelegramUser(id=1, telegram_id=555555, role="admin", enabled=True)
        session.add(admin)
        session.commit()

    return session_factory, engine, settings_mgr, outbox_svc, scheduler


def test_cron_evaluator_timezone_awareness():
    """Verify 5-field cron parsing matches correct time in Asia/Jakarta."""
    # Every Monday at 08:00 Asia/Jakarta
    cron_expr = "0 8 * * 1"
    jakarta_tz = zoneinfo.ZoneInfo("Asia/Jakarta")

    # Monday 2026-11-16 08:00 Jakarta time (which is 2026-11-16 01:00 UTC)
    dt_match_jakarta = datetime(2026, 11, 16, 8, 0, tzinfo=jakarta_tz)
    assert is_cron_match(cron_expr, dt_match_jakarta, "Asia/Jakarta") is True

    # Same time in UTC (01:00 UTC == 08:00 Jakarta)
    dt_match_utc = datetime(2026, 11, 16, 1, 0, tzinfo=timezone.utc)
    assert is_cron_match(cron_expr, dt_match_utc, "Asia/Jakarta") is True

    # Tuesday 2026-11-17 08:00 Jakarta time -> False (not Monday)
    dt_wrong_day = datetime(2026, 11, 17, 8, 0, tzinfo=jakarta_tz)
    assert is_cron_match(cron_expr, dt_wrong_day, "Asia/Jakarta") is False

    # Monday 2026-11-16 08:05 Jakarta time -> False (not 08:00)
    dt_wrong_minute = datetime(2026, 11, 16, 8, 5, tzinfo=jakarta_tz)
    assert is_cron_match(cron_expr, dt_wrong_minute, "Asia/Jakarta") is False


def test_scheduled_run_requires_prior_spend_authorization(auto_research_env):
    """Auto research will not execute if prior spend has not been approved by an admin."""
    session_factory, engine, settings_mgr, outbox_svc, scheduler = auto_research_env

    # Configure auto mode but WITHOUT prior admin spend approval (approved_by is None)
    with session_factory() as session:
        setting = settings_mgr.get_or_create_settings(session)
        setting.mode = "auto"
        setting.auto_enabled = True
        setting.auto_paid_research_approved_by = None  # Missing spend approval!
        session.commit()

    test_time = datetime(2026, 11, 16, 1, 0, tzinfo=timezone.utc)
    run = scheduler.evaluate_and_run(as_of=test_time)

    # Must be blocked and disabled
    assert run is None
    with session_factory() as session:
        s = settings_mgr.get_or_create_settings(session)
        assert s.auto_enabled is False

        # Alert sent to outbox
        alert = session.query(TelegramOutbox).first()
        assert alert is not None
        assert "Diperlukan otorisasi anggaran" in alert.payload_json


def test_scheduled_run_executes_within_budget_and_persists_candidates(auto_research_env):
    """When properly authorized and scheduled, auto research executes and persists candidates."""
    session_factory, engine, settings_mgr, outbox_svc, scheduler = auto_research_env

    # Authorize and configure auto schedule
    settings_mgr.configure_auto_schedule(
        cron_expr="0 8 * * 1",
        timezone_str="Asia/Jakarta",
        max_niches=5,
        daily_limit_cents=100,
        approved_by_user_id=1,
        auto_enabled=True,
    )

    # Evaluate at scheduled time (Monday 08:00 Jakarta / 01:00 UTC)
    scheduled_time = datetime(2026, 11, 16, 1, 0, tzinfo=timezone.utc)
    run = scheduler.evaluate_and_run(as_of=scheduled_time)

    assert run is not None
    assert run.trigger == "scheduled"
    assert "auto_research_20261116_0800" in run.schedule_key
    assert run.status == "awaiting_niche_selection"
    assert run.actual_cost_cents == 10

    # Verify candidates persisted with source citations
    with session_factory() as session:
        candidates = session.query(ResearchCandidate).filter_by(research_run_id=run.id).all()
        assert len(candidates) >= 2
        assert any(c.evidence_quality == "verified" for c in candidates)

        # INVARIANT: Zero image jobs enqueued!
        assert session.query(Job).count() == 0

        # Outbox notification was enqueued
        outbox_items = session.query(TelegramOutbox).all()
        assert len(outbox_items) == 1
        assert "Laporan Auto Research Terjadwal" in outbox_items[0].payload_json


def test_downtime_recovery_skips_catch_up_storm_and_prevents_duplicate_runs(auto_research_env):
    """Anti-storm check: repeated calls for the same schedule occurrence are safely skipped."""
    session_factory, engine, settings_mgr, outbox_svc, scheduler = auto_research_env

    settings_mgr.configure_auto_schedule(
        cron_expr="0 8 * * 1",
        timezone_str="Asia/Jakarta",
        max_niches=5,
        daily_limit_cents=100,
        approved_by_user_id=1,
        auto_enabled=True,
    )

    scheduled_time = datetime(2026, 11, 16, 1, 0, tzinfo=timezone.utc)

    # First execution succeeds
    run1 = scheduler.evaluate_and_run(as_of=scheduled_time)
    assert run1 is not None

    # Immediate second call for the same occurrence is skipped by schedule_key uniqueness
    run2 = scheduler.evaluate_and_run(as_of=scheduled_time)
    assert run2 is None

    # Database contains exactly 1 research run
    with session_factory() as session:
        assert session.query(ResearchRun).count() == 1


def test_budget_exhaustion_pauses_auto_mode(auto_research_env):
    """Exceeding research daily limit automatically pauses auto_enabled and sends alert."""
    session_factory, engine, settings_mgr, outbox_svc, scheduler = auto_research_env

    # Configure small limit of 15 cents
    settings_mgr.configure_auto_schedule(
        cron_expr="0 8 * * 1",
        timezone_str="Asia/Jakarta",
        max_niches=5,
        daily_limit_cents=15,
        approved_by_user_id=1,
        auto_enabled=True,
    )

    # Pre-exhaust research budget today by spending 10 cents
    with session_factory() as session:
        prior_run = ResearchRun(
            trigger="manual",
            topic="Pre-existing spend",
            status="completed",
            actual_cost_cents=10,
            started_at=datetime.now(timezone.utc),
        )
        session.add(prior_run)
        session.commit()

    # Now scheduled run needs 10 cents (10 + 10 = 20 > 15) -> must exceed budget!
    scheduled_time = datetime(2026, 11, 16, 1, 0, tzinfo=timezone.utc)
    run = scheduler.evaluate_and_run(as_of=scheduled_time)

    assert run is None
    with session_factory() as session:
        setting = settings_mgr.get_or_create_settings(session)
        assert setting.auto_enabled is False

        # Alert sent to outbox
        alert = session.query(TelegramOutbox).filter(TelegramOutbox.event_key.like("%budget_exceeded%")).first()
        assert alert is not None
        assert "Auto Research Dijeda" in alert.payload_json
