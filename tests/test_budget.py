"""Acceptance tests for SF-008 Budget reservation, settlement, release, and caps."""
from __future__ import annotations

import concurrent.futures
from datetime import datetime, timezone
from pathlib import Path
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from stockforge.budget.service import (
    BudgetExceededError,
    BudgetService,
    InvalidLedgerOperationError,
    UnknownCostError,
)
from stockforge.db.models import Base, BudgetLedger, Job


@pytest.fixture
def test_db(tmp_path: Path):
    """Setup isolated SQLite database with WAL mode."""
    db_path = tmp_path / "test_budget.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"timeout": 15},
    )
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA journal_mode = WAL;")
        conn.exec_driver_sql("PRAGMA foreign_keys = ON;")
        conn.commit()

    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    return session_factory


def _create_dummy_job(session_factory, job_id: int) -> None:
    with session_factory() as session:
        job = Job(
            id=job_id,
            type="image_batch",
            status="pending",
            operation_id=f"op-test-{job_id}",
        )
        session.add(job)
        session.commit()


def test_integer_cents_reserve_settle_release_cycle(test_db):
    """Verify standard 3-phase budget cycle: reserve -> settle / release in exact integer cents."""
    _create_dummy_job(test_db, 1)
    _create_dummy_job(test_db, 2)

    service = BudgetService(session_factory=test_db, daily_limit_cents=300, monthly_limit_cents=5000)

    # 1. Job 1 reserves 20 cents
    reserve_entry = service.reserve_budget(job_id=1, estimated_cost_cents=20)
    assert reserve_entry.kind == "reserve"
    assert reserve_entry.amount_cents == 20

    with test_db() as s:
        assert service.get_active_reservation_for_job(s, job_id=1) == 20
        assert service.get_daily_committed_cents(s) == 20

    # 2. Job 1 settles with actual cost 18 cents
    settle_entry = service.settle_budget(job_id=1, actual_cost_cents=18)
    assert settle_entry.kind == "settle"
    assert settle_entry.amount_cents == 18

    with test_db() as s:
        # Reservation should now be 0, settled today should be 18
        assert service.get_active_reservation_for_job(s, job_id=1) == 0
        assert service.get_daily_committed_cents(s) == 18

    # 3. Job 2 reserves 30 cents, then releases completely (failed job)
    service.reserve_budget(job_id=2, estimated_cost_cents=30)
    with test_db() as s:
        assert service.get_daily_committed_cents(s) == 18 + 30  # 48 cents

    release_entry = service.release_budget(job_id=2)
    assert release_entry.kind == "release"
    assert release_entry.amount_cents == 30

    with test_db() as s:
        assert service.get_active_reservation_for_job(s, job_id=2) == 0
        # Only settled 18 cents remains committed
        assert service.get_daily_committed_cents(s) == 18


def test_unknown_cost_fail_closed(test_db):
    """Verify that unknown cost or <=0 fails closed with UnknownCostError."""
    _create_dummy_job(test_db, 1)
    service = BudgetService(session_factory=test_db)

    with pytest.raises(UnknownCostError):
        service.reserve_budget(job_id=1, estimated_cost_cents=None)

    with pytest.raises(UnknownCostError):
        service.reserve_budget(job_id=1, estimated_cost_cents=0)

    with pytest.raises(UnknownCostError):
        service.reserve_budget(job_id=1, estimated_cost_cents=-5)


def test_daily_budget_cap_enforced(test_db):
    """Verify that reservations exceeding daily cap are rejected."""
    _create_dummy_job(test_db, 1)
    _create_dummy_job(test_db, 2)

    # Daily cap: 100 cents ($1.00)
    service = BudgetService(session_factory=test_db, daily_limit_cents=100)

    # Reserve 70 cents -> OK
    service.reserve_budget(job_id=1, estimated_cost_cents=70)

    # Reserve 40 cents -> 70 + 40 = 110 > 100 -> BudgetExceededError
    with pytest.raises(BudgetExceededError) as exc_info:
        service.reserve_budget(job_id=2, estimated_cost_cents=40)

    assert exc_info.value.limit_cents == 100
    assert exc_info.value.current_committed_cents == 70
    assert exc_info.value.requested_cents == 40


def test_concurrent_reservations_cannot_exceed_cap(test_db):
    """Verify acceptance criterion: concurrent job requests cannot exceed cap."""
    # Create 10 dummy jobs
    for i in range(1, 11):
        _create_dummy_job(test_db, i)

    # Daily cap: 100 cents ($1.00). Each job requests 20 cents.
    # At most 5 jobs can succeed (5 * 20 = 100); the other 5 MUST be rejected.
    service = BudgetService(session_factory=test_db, daily_limit_cents=100)

    success_count = 0
    exceeded_count = 0

    def attempt_reservation(job_id: int):
        try:
            service.reserve_budget(job_id=job_id, estimated_cost_cents=20)
            return True, None
        except BudgetExceededError as e:
            return False, e
        except Exception as e:
            return False, e

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(attempt_reservation, j_id) for j_id in range(1, 11)]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]

    for success, err in results:
        if success:
            success_count += 1
        elif isinstance(err, BudgetExceededError):
            exceeded_count += 1

    assert success_count == 5, f"Tepat 5 job harus berhasil direservasi (ditemukan: {success_count})"
    assert exceeded_count == 5, f"Tepat 5 job harus ditolak dengan BudgetExceededError (ditemukan: {exceeded_count})"

    with test_db() as s:
        committed = service.get_daily_committed_cents(s)
        assert committed == 100, "Total komitmen tidak boleh melampaui 100 sen"


def test_ambiguous_billable_timeout_reconciliation_flow(test_db):
    """Verify acceptance criterion: ambiguous billable timeout moves to reconciliation without releasing funds."""
    _create_dummy_job(test_db, 1)
    service = BudgetService(session_factory=test_db, daily_limit_cents=100)

    # 1. Job reserves 50 cents
    service.reserve_budget(job_id=1, estimated_cost_cents=50)

    # 2. Timeout ambigu saat panggilan berbayar
    service.handle_ambiguous_timeout(job_id=1, error_context="Provider HTTP 504 Gateway Timeout")

    with test_db() as s:
        job = s.get(Job, 1)
        assert job.status == "needs_reconciliation"
        # Dana reservasi TETAP DITAHAN!
        assert service.get_active_reservation_for_job(s, job_id=1) == 50
        assert service.get_daily_committed_cents(s) == 50

    # 3. New job attempting to reserve 60 cents should fail because 50 is still held (50+60=110 > 100)
    _create_dummy_job(test_db, 2)
    with pytest.raises(BudgetExceededError):
        service.reserve_budget(job_id=2, estimated_cost_cents=60)

    # 4. Operator reconciles job #1: turns out provider DID bill 45 cents
    service.reconcile_job(job_id=1, was_billed=True, actual_cost_cents=45)

    with test_db() as s:
        job = s.get(Job, 1)
        assert job.status == "completed"
        # Reservation cleared, actual spend settled at 45 cents
        assert service.get_active_reservation_for_job(s, job_id=1) == 0
        assert service.get_daily_committed_cents(s) == 45


def test_double_reservation_prevented(test_db):
    """Verify that a job cannot reserve budget twice simultaneously."""
    _create_dummy_job(test_db, 1)
    service = BudgetService(session_factory=test_db)

    service.reserve_budget(job_id=1, estimated_cost_cents=25)
    with pytest.raises(InvalidLedgerOperationError, match="sudah memiliki reservasi aktif"):
        service.reserve_budget(job_id=1, estimated_cost_cents=25)

