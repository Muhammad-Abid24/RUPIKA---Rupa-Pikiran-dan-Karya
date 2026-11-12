"""Acceptance tests for SF-004 Durable job queue and worker."""
import concurrent.futures
import time
from datetime import timedelta
from pathlib import Path
import pytest
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from stockforge.cli import app
from stockforge.db.models import Base, Job, JobAttempt, JobItem, JobEvent, utc_now
from stockforge.db.session import create_db_engine
from stockforge.queue.service import QueueService
from stockforge.queue.worker import Worker

runner = CliRunner()


@pytest.fixture
def queue_service(tmp_path: Path) -> QueueService:
    """Fixture providing initialized QueueService on a temporary SQLite database."""
    db_file = tmp_path / "queue_test.db"
    engine = create_db_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    return QueueService(engine)


def test_competing_workers_cannot_claim_same_job(queue_service: QueueService):
    """Verify atomicity: two competing workers cannot claim the same job."""
    # Enqueue exactly 1 job
    job = queue_service.enqueue_job(job_type="image_batch", operation_id="op_single_claim")
    assert job.status == "pending"

    claimed_results = []

    def try_claim(worker_name: str):
        # Dedicated connection per thread to simulate distinct worker processes
        res = queue_service.claim_job(worker_id=worker_name, lease_seconds=30)
        return worker_name, res

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(try_claim, "worker-A")
        f2 = executor.submit(try_claim, "worker-B")
        r1 = f1.result()
        r2 = f2.result()
        claimed_results.extend([r1, r2])

    successful_claims = [w for w, job in claimed_results if job is not None]
    unsuccessful_claims = [w for w, job in claimed_results if job is None]

    assert len(successful_claims) == 1, f"Expected exactly 1 claim, got: {successful_claims}"
    assert len(unsuccessful_claims) == 1, f"Expected exactly 1 failed claim, got: {unsuccessful_claims}"

    # Verify database state
    with Session(queue_service.engine) as session:
        db_job = session.get(Job, job.id)
        assert db_job.status == "running"
        assert db_job.lease_owner in ("worker-A", "worker-B")
        assert db_job.attempts == 1


def test_heartbeat_extends_lease(queue_service: QueueService):
    """Verify worker heartbeat successfully extends lease_until."""
    job = queue_service.enqueue_job(job_type="image_batch", operation_id="op_heartbeat")
    claimed = queue_service.claim_job(worker_id="worker-1", lease_seconds=10)
    assert claimed is not None
    initial_lease = claimed.lease_until

    # Perform heartbeat with 120s extension
    extended = queue_service.heartbeat(job_id=job.id, worker_id="worker-1", extend_seconds=120)
    assert extended is True

    with Session(queue_service.engine) as session:
        refreshed = session.get(Job, job.id)
        assert refreshed.lease_until > initial_lease


def test_cooperative_cancellation(queue_service: QueueService):
    """Verify worker halts execution when job is cancelled cooperatively."""
    job = queue_service.enqueue_job(job_type="image_batch", operation_id="op_cancel")

    # Seed 3 items for the job
    with Session(queue_service.engine) as session:
        for idx in range(1, 4):
            session.add(JobItem(job_id=job.id, item_index=idx, stage="prompt", status="pending"))
        session.commit()

    worker = Worker(queue_service=queue_service, worker_id="worker-cancel-test")
    processed_items = []

    def mock_processor(job_id: int, item_index: int):
        processed_items.append(item_index)
        if item_index == 1:
            # Request cancellation right after processing item 1
            queue_service.cancel_job(job_id)

    outcome = worker.run_once(item_processor=mock_processor)
    assert outcome == job.id

    # Item 1 was processed, item 2 and 3 should be skipped due to cancellation
    assert processed_items == [1]

    with Session(queue_service.engine) as session:
        refreshed = session.get(Job, job.id)
        assert refreshed.status == "cancelled"


def test_crash_recovery_does_not_replay_ambiguous_paid_calls(queue_service: QueueService):
    """Verify stale running jobs with in-flight attempts are moved to needs_reconciliation."""
    now = utc_now()
    past_time = now - timedelta(seconds=120)

    # Simulate a job that was claimed, made a paid attempt, and then worker crashed
    with Session(queue_service.engine) as session:
        job = Job(
            type="image_batch",
            operation_id="op_stale_paid",
            status="running",
            lease_owner="crashed-worker",
            lease_until=past_time,  # lease expired
            attempts=1,
            max_attempts=3,
            created_at=past_time,
            updated_at=past_time,
        )
        session.add(job)
        session.flush()

        # Record an in-flight billable attempt
        attempt = JobAttempt(
            job_id=job.id,
            attempt_number=1,
            role="image",
            provider="fal",
            model="flux",
            operation_id="op_stale_paid_item1",
            outcome="in_flight",
            started_at=past_time,
        )
        session.add(attempt)
        session.commit()
        job_id = job.id

    # Run crash recovery
    recovered_ids = queue_service.recover_stale_jobs()
    assert job_id in recovered_ids

    # Critical invariant: status MUST be needs_reconciliation, never pending or retried blindly!
    with Session(queue_service.engine) as session:
        refreshed = session.get(Job, job_id)
        assert refreshed.status == "needs_reconciliation"
        assert refreshed.lease_owner is None

        # Check audit event
        events = session.query(JobEvent).filter(JobEvent.job_id == job_id).all()
        assert len(events) == 1
        assert events[0].event_type == "needs_reconciliation"
        assert "ambigu" in events[0].payload_json or "Crash recovery" in events[0].payload_json


def test_crash_recovery_resets_unattempted_job(queue_service: QueueService):
    """Verify stale running jobs with NO attempts are safely reset to pending."""
    now = utc_now()
    past_time = now - timedelta(seconds=120)

    with Session(queue_service.engine) as session:
        job = Job(
            type="image_batch",
            operation_id="op_stale_unattempted",
            status="running",
            lease_owner="crashed-early",
            lease_until=past_time,
            attempts=1,
            max_attempts=3,
            created_at=past_time,
            updated_at=past_time,
        )
        session.add(job)
        session.commit()
        job_id = job.id

    recovered_ids = queue_service.recover_stale_jobs()
    assert job_id in recovered_ids

    with Session(queue_service.engine) as session:
        refreshed = session.get(Job, job_id)
        # Safe to retry because no billable API calls occurred
        assert refreshed.status == "pending"
        assert refreshed.lease_owner is None


def test_outbox_event_idempotency(queue_service: QueueService):
    """Verify unique idempotency key on telegram_outbox."""
    evt = queue_service.publish_outbox_event(
        chat_id=1234,
        event_key="evt_unique_outbox_1",
        payload={"batch_id": 1, "status": "completed"},
    )
    assert evt.id is not None
    assert evt.event_key == "evt_unique_outbox_1"

    # Duplicate key must be rejected by unique constraint
    with pytest.raises(Exception):
        queue_service.publish_outbox_event(
            chat_id=1234,
            event_key="evt_unique_outbox_1",
            payload={"batch_id": 1, "status": "duplicate"},
        )


def test_cli_worker_help():
    """Verify `rupika worker --help` displays usage and options."""
    result = runner.invoke(app, ["worker", "--help"])
    assert result.exit_code == 0
    assert "Start worker daemon" in result.output
    assert "--lease" in result.output
    assert "--interval" in result.output

