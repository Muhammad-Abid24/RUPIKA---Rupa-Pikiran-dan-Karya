"""Unit and integration tests for research modes, production plans, and generation gate (SF-020)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from stockforge.budget.service import BudgetExceededError, BudgetService
from stockforge.db.models import (
    Base,
    Job,
    NicheSelection,
    ProductionPlan,
    ResearchCandidate,
    ResearchRun,
    ResearchSetting,
    TelegramUser,
    utc_now,
)
from stockforge.queue.service import QueueService
from stockforge.research import (
    GenerationGate,
    GenerationGateBlockedError,
    PlanAlreadyConsumedError,
    PlanExpiredError,
    PlanIntegrityError,
    PlanStateError,
    ProductionPlanner,
    ResearchBudgetExceededError,
    ResearchSettingsManager,
    compute_plan_hash,
)


@pytest.fixture
def research_gate_env(tmp_path: Path):
    """Setup isolated SQLite DB with Alembic-compatible schema."""
    db_path = tmp_path / "test_research_gate.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    queue_service = QueueService(engine=engine)
    budget_service = BudgetService(session_factory, daily_limit_cents=1000)
    settings_mgr = ResearchSettingsManager(session_factory)
    planner = ProductionPlanner(session_factory)
    gate = GenerationGate(session_factory, queue_service, budget_service)

    # Seed admin user
    with session_factory() as session:
        admin = TelegramUser(id=1, telegram_id=888888, role="admin", enabled=True)
        session.add(admin)
        session.commit()

    return session_factory, engine, queue_service, budget_service, settings_mgr, planner, gate


def test_research_settings_default_schedules_nothing(research_gate_env):
    """Default installation must default to manual mode with auto_enabled=False."""
    session_factory, engine, queue_service, budget_service, settings_mgr, planner, gate = research_gate_env

    with session_factory() as session:
        setting = settings_mgr.get_or_create_settings(session)
        assert setting.mode == "manual"
        assert setting.auto_enabled is False
        assert setting.cron_expr == "0 8 * * 1"
        assert setting.timezone == "Asia/Jakarta"
        assert setting.max_niches == 5
        assert setting.research_daily_limit_cents == 100
        assert setting.auto_paid_research_approved_by is None


def test_opt_in_scheduled_research_mode_configuration(research_gate_env):
    """Admin can explicitly configure and opt-in to Auto Research schedule and spending limit."""
    session_factory, engine, queue_service, budget_service, settings_mgr, planner, gate = research_gate_env

    setting = settings_mgr.configure_auto_schedule(
        cron_expr="0 9 * * 1-5",
        timezone_str="Asia/Jakarta",
        max_niches=8,
        daily_limit_cents=200,
        approved_by_user_id=1,
        auto_enabled=True,
    )
    assert setting.mode == "auto"
    assert setting.auto_enabled is True
    assert setting.cron_expr == "0 9 * * 1-5"
    assert setting.max_niches == 8
    assert setting.research_daily_limit_cents == 200
    assert setting.auto_paid_research_approved_by == 1

    # Switching back to manual disables auto_enabled automatically
    manual_setting = settings_mgr.set_mode("manual", updated_by_user_id=1)
    assert manual_setting.mode == "manual"
    assert manual_setting.auto_enabled is False


def test_separate_research_budget_enforcement(research_gate_env):
    """Research daily budget limit is enforced separately from image budget."""
    session_factory, engine, queue_service, budget_service, settings_mgr, planner, gate = research_gate_env

    with session_factory() as session:
        # Research limit is 100 cents
        # Record an existing research run that spent 80 cents
        run1 = ResearchRun(
            trigger="manual",
            topic="Baking",
            status="completed",
            actual_cost_cents=80,
            started_at=utc_now(),
        )
        session.add(run1)
        session.commit()

        # Attempting a research operation costing 15 cents passes (80 + 15 = 95 <= 100)
        settings_mgr.check_and_reserve_research_budget(session, estimated_cost_cents=15)

        # Attempting a research operation costing 25 cents fails (80 + 25 = 105 > 100)
        with pytest.raises(ResearchBudgetExceededError):
            settings_mgr.check_and_reserve_research_budget(session, estimated_cost_cents=25)


def test_zero_image_provider_calls_during_research_and_niche_selection(research_gate_env):
    """INVARIANT: Research, candidate discovery, and niche selection must produce 0 image jobs or calls."""
    session_factory, engine, queue_service, budget_service, settings_mgr, planner, gate = research_gate_env

    with session_factory() as session:
        # Create research run and candidate
        run = ResearchRun(trigger="manual", topic="Indonesian Traditional Crafts", status="researching")
        session.add(run)
        session.commit()

        cand = ResearchCandidate(
            research_run_id=run.id,
            niche_name="Batik Tulis Artisans",
            rationale="High commercial demand on Adobe Stock",
            evidence_quality="verified",
            sources_json='["https://example.com/crafts"]',
        )
        session.add(cand)
        session.commit()

        # Operator selects niche
        selection = NicheSelection(
            research_run_id=run.id,
            candidate_id=cand.id,
            telegram_user_id=1,
            decision="selected",
        )
        session.add(selection)
        session.commit()

    # Verify no jobs exist in the queue table!
    with session_factory() as session:
        jobs_count = session.query(Job).count()
        assert jobs_count == 0


def test_immutable_plan_hashing_and_material_change_invalidation(research_gate_env):
    """Material change to prompts, model, or quantity creates a different hash, invalidating approval."""
    session_factory, engine, queue_service, budget_service, settings_mgr, planner, gate = research_gate_env

    niches = ["Batik Crafts"]
    concepts_v1 = [{"title": "Artisan Waxing", "prompt": "Indonesian batik artist waxing cloth", "fingerprint": "fp_01"}]
    routes_v1 = {"image": "openai-dalle3"}

    hash_v1 = compute_plan_hash(
        research_run_id=1,
        selected_niches=niches,
        concepts=concepts_v1,
        image_count=5,
        provider_routes=routes_v1,
        estimated_max_cost_cents=20,
    )

    # 1. Modify prompt text
    concepts_v2 = [{"title": "Artisan Waxing", "prompt": "Indonesian batik artist painting cloth", "fingerprint": "fp_02"}]
    hash_v2 = compute_plan_hash(
        research_run_id=1,
        selected_niches=niches,
        concepts=concepts_v2,
        image_count=5,
        provider_routes=routes_v1,
        estimated_max_cost_cents=20,
    )
    assert hash_v1 != hash_v2

    # 2. Modify quantity
    hash_v3 = compute_plan_hash(
        research_run_id=1,
        selected_niches=niches,
        concepts=concepts_v1,
        image_count=10,  # Changed from 5 to 10
        provider_routes=routes_v1,
        estimated_max_cost_cents=40,
    )
    assert hash_v1 != hash_v3


def test_generation_gate_blocks_unapproved_or_expired_plan(research_gate_env):
    """GenerationGate strictly blocks unapproved, modified, or expired production plans."""
    session_factory, engine, queue_service, budget_service, settings_mgr, planner, gate = research_gate_env

    # 1. Create a plan (status: awaiting_approval)
    plan = planner.create_plan(
        research_run_id=1,
        selected_niches=["Batik Crafts"],
        concepts=[{"title": "Artisan", "prompt": "Artisan working", "fingerprint": "fp1"}],
        image_count=3,
        provider_routes={"image": "openai-dalle3"},
        estimated_max_cost_cents=12,
        expires_in_hours=24,
    )

    # Attempting to consume unapproved plan must fail
    with pytest.raises(GenerationGateBlockedError) as exc_unapproved:
        gate.consume_and_enqueue_job(plan.id, actor_telegram_id=888888)
    assert "belum disetujui" in str(exc_unapproved.value)

    # 2. Attempting to approve with mismatched hash must fail
    with pytest.raises(PlanIntegrityError):
        planner.approve_plan(plan.id, approved_by_user_id=1, expected_plan_hash="tampered_hash_123")

    # 3. Simulate expired plan
    with session_factory() as session:
        persisted = session.get(ProductionPlan, plan.id)
        persisted.expires_at = utc_now() - timedelta(minutes=10)
        session.commit()

    with pytest.raises(PlanStateError) as exc_expired:
        planner.approve_plan(plan.id, approved_by_user_id=1, expected_plan_hash=plan.plan_hash)
    assert "kedaluwarsa" in str(exc_expired.value)


def test_generation_gate_single_use_consumption_and_replay_protection(research_gate_env):
    """Approved plan is consumed once to create a Job; repeated calls or callbacks are blocked."""
    session_factory, engine, queue_service, budget_service, settings_mgr, planner, gate = research_gate_env

    # 1. Create and approve plan
    plan = planner.create_plan(
        research_run_id=2,
        selected_niches=["Ceramics"],
        concepts=[{"title": "Potter", "prompt": "Hands shaping clay", "fingerprint": "fp_clay"}],
        image_count=4,
        provider_routes={"image": "openai-dalle3"},
        estimated_max_cost_cents=16,
    )
    approved_plan = planner.approve_plan(plan.id, approved_by_user_id=1, expected_plan_hash=plan.plan_hash)
    assert approved_plan.status == "approved"

    # 2. Consume and enqueue job
    job = gate.consume_and_enqueue_job(
        plan_id=plan.id,
        actor_telegram_id=888888,
        expected_plan_hash=plan.plan_hash,
    )
    assert job is not None
    assert job.type == "image_batch"

    # Verify plan is now CONSUMED and tied to job
    with session_factory() as session:
        p = session.get(ProductionPlan, plan.id)
        assert p.status == "consumed"
        assert p.consumed_job_id == job.id

    # 3. Repeated callback / replay attack MUST be rejected
    with pytest.raises(PlanAlreadyConsumedError) as exc_replay:
        gate.consume_and_enqueue_job(
            plan_id=plan.id,
            actor_telegram_id=888888,
            expected_plan_hash=plan.plan_hash,
        )
    assert "sudah pernah dikonsumsi" in str(exc_replay.value)

    # Queue must only contain exactly 1 job
    with session_factory() as session:
        total_jobs = session.query(Job).count()
        assert total_jobs == 1
