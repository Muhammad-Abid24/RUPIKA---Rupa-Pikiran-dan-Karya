"""Acceptance tests for SF-016 Durable per-image progress and stage tracking."""
from __future__ import annotations

from pathlib import Path
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from stockforge.db.models import Base, Job, JobAttempt, JobItem
from stockforge.queue.service import QueueService
from stockforge.queue.tracker import (
    VALID_ITEM_STATUSES,
    VALID_STAGES,
    ItemProgressTracker,
    sanitize_error_text,
)
from stockforge.queue.worker import Worker


@pytest.fixture
def queue_env(tmp_path: Path):
    """Setup isolated SQLite DB and queue service."""
    db_path = tmp_path / "test_progress.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    queue_service = QueueService(engine=engine)
    tracker = ItemProgressTracker(session_factory=session_factory)
    return session_factory, engine, queue_service, tracker


# ---------------------------------------------------------------------------
# Test 1: Initialize 10 items and valid stage state transitions
# ---------------------------------------------------------------------------
def test_initialize_and_stage_transitions(queue_env):
    session_factory, engine, queue_service, tracker = queue_env

    job = queue_service.enqueue_job(job_type="image_batch", operation_id="op_init_10")
    items = tracker.initialize_job_items(job_id=job.id, total_items=10, initial_stage="prompt")

    assert len(items) == 10
    assert items[0].item_index == 1
    assert items[-1].item_index == 10
    assert all(i.status == "pending" for i in items)
    assert all(i.stage == "prompt" for i in items)

    # Transition item 1 through stages: prompt -> generating -> qc -> metadata
    for stg in ["generating", "download", "qc", "metadata"]:
        updated = tracker.update_item_stage(job.id, item_index=1, stage=stg, status="processing")
        assert updated.stage == stg
        assert updated.status == "processing"

    # Complete item 1
    completed_item = tracker.update_item_stage(job.id, item_index=1, stage="metadata", status="completed")
    assert completed_item.status == "completed"

    # Invariant: Terminal completed status cannot be regressed back to pending or processing
    regressed = tracker.update_item_stage(job.id, item_index=1, stage="generating", status="pending")
    assert regressed.status == "completed"

    # Invalid stage or status raises ValueError
    with pytest.raises(ValueError):
        tracker.update_item_stage(job.id, item_index=2, stage="invalid_stage_xyz", status="processing")

    with pytest.raises(ValueError):
        tracker.update_item_stage(job.id, item_index=2, stage="generating", status="invalid_status_abc")


# ---------------------------------------------------------------------------
# Test 2: Accurate batch counters (6 complete + 1 processing + 3 pending != 70%)
# ---------------------------------------------------------------------------
def test_accurate_batch_counters_invariant(queue_env):
    session_factory, _, queue_service, tracker = queue_env

    job = queue_service.enqueue_job(job_type="image_batch", operation_id="op_count_10")
    tracker.initialize_job_items(job_id=job.id, total_items=10, initial_stage="generating")

    # Set 6 items to completed
    for i in range(1, 7):
        tracker.update_item_stage(job.id, item_index=i, stage="generating", status="completed")

    # Set 1 item (item 7) to processing
    tracker.update_item_stage(job.id, item_index=7, stage="generating", status="processing")

    # Leave items 8, 9, 10 as pending

    progress = tracker.get_batch_progress(job.id)

    # Acceptance criteria verification:
    assert progress.total_items == 10
    assert progress.completed_count == 6
    assert progress.processing_count == 1
    assert progress.pending_count == 3
    assert progress.failed_count == 0

    # CRITICAL INVARIANT: 6 complete + 1 generating + 3 pending MUST NOT be reported as 70%
    assert progress.success_rate_percent == 60.0
    assert progress.is_terminal is False
    assert progress.current_item_index == 7
    assert progress.current_stage == "generating"

    # Summary text displays active counters correctly
    assert "Gambar 7/10" in progress.summary_text
    assert "6 selesai" in progress.summary_text
    assert "1 proses" in progress.summary_text
    assert "3 antre" in progress.summary_text


# ---------------------------------------------------------------------------
# Test 3: Worker crash & restart resilience — Never rerun successful images
# ---------------------------------------------------------------------------
def test_worker_crash_and_restart_resilience(queue_env):
    session_factory, engine, queue_service, tracker = queue_env

    job = queue_service.enqueue_job(job_type="image_batch", operation_id="op_crash_5")
    tracker.initialize_job_items(job_id=job.id, total_items=5, initial_stage="generating")

    # Phase 1: Items 1 and 2 finish successfully
    tracker.update_item_stage(job.id, item_index=1, stage="generating", status="completed")
    tracker.update_item_stage(job.id, item_index=2, stage="generating", status="completed")

    # Item 3 is interrupted mid-processing when worker process crashes
    tracker.update_item_stage(job.id, item_index=3, stage="generating", status="processing")
    # Items 4 and 5 remain pending

    # Phase 2: Worker 2 restarts and plans resumption
    to_resume = tracker.prepare_resumption_plan(job.id)

    # INVARIANT: Completed items (1 and 2) are NEVER re-executed!
    assert 1 not in to_resume
    assert 2 not in to_resume
    # Interrupted item 3 and pending items 4, 5 are resumed
    assert to_resume == [3, 4, 5]

    # Verify item 3 was reset from 'processing' back to 'pending'
    with session_factory() as session:
        item3 = session.scalars(
            select(JobItem).where(JobItem.job_id == job.id, JobItem.item_index == 3)
        ).first()
        assert item3.status == "pending"

    # Phase 3: Execute the remaining items with a Worker instance
    executed_items: list[int] = []

    def mock_processor(j_id: int, idx: int) -> None:
        executed_items.append(idx)

    # Claim job with worker 2
    worker2 = Worker(queue_service=queue_service, worker_id="worker-restart-2")
    job_claimed = queue_service.claim_job(worker_id="worker-restart-2")
    assert job_claimed is not None

    status = worker2.execute_job_items(job_id=job.id, item_processor=mock_processor)
    assert status == "completed"

    # Verified: processor was ONLY called for items 3, 4, 5!
    assert executed_items == [3, 4, 5]

    # Batch is now 100% complete
    final_prog = tracker.get_batch_progress(job.id)
    assert final_prog.completed_count == 5
    assert final_prog.is_terminal is True
    assert final_prog.success_rate_percent == 100.0


# ---------------------------------------------------------------------------
# Test 4: Attempt recording with operational metadata and cost tracking
# ---------------------------------------------------------------------------
def test_record_item_attempt(queue_env):
    session_factory, _, queue_service, tracker = queue_env

    job = queue_service.enqueue_job(job_type="image_batch", operation_id="op_attempt_1")
    tracker.initialize_job_items(job_id=job.id, total_items=1)

    attempt = tracker.record_item_attempt(
        job_id=job.id,
        item_index=1,
        role="image",
        provider="mock-image-provider",
        model="flux-dev-commercial",
        attempt_number=1,
        outcome="success",
        request_id="req-123456",
        operation_id="op-sub-1",
        http_status=200,
        estimated_cost_cents=5,
        actual_cost_cents=5,
    )

    assert attempt.id is not None
    assert attempt.attempt_number == 1
    assert attempt.role == "image"
    assert attempt.actual_cost_cents == 5
    assert attempt.http_status == 200

    with session_factory() as session:
        loaded = session.get(JobAttempt, attempt.id)
        assert loaded is not None
        assert loaded.request_id == "req-123456"


# ---------------------------------------------------------------------------
# Test 5: Error text sanitization (no leaked keys or tokens)
# ---------------------------------------------------------------------------
def test_error_text_sanitization():
    dirty_error = (
        "HTTP 401 Unauthorized from provider: Bearer sk-abcdef1234567890 "
        "failed with Token=my_super_secret_token_12345"
    )
    cleaned = sanitize_error_text(dirty_error)
    assert "sk-abcdef1234567890" not in cleaned
    assert "my_super_secret_token_12345" not in cleaned
    assert "***REDACTED***" in cleaned


# ---------------------------------------------------------------------------
# Test 6: Batch reports 'completed_with_errors' when some items fail
# ---------------------------------------------------------------------------
def test_batch_completed_with_errors_when_some_items_fail(queue_env):
    session_factory, _, queue_service, tracker = queue_env

    job = queue_service.enqueue_job(job_type="image_batch", operation_id="op_fail_1")
    tracker.initialize_job_items(job_id=job.id, total_items=3)

    def failing_processor(j_id: int, idx: int) -> None:
        if idx == 2:
            raise RuntimeError("Simulated provider failure on item 2")

    worker = Worker(queue_service=queue_service, worker_id="worker-test-fail")
    _ = queue_service.claim_job(worker_id="worker-test-fail")

    status = worker.execute_job_items(job_id=job.id, item_processor=failing_processor)
    assert status == "completed_with_errors"

    progress = tracker.get_batch_progress(job.id)
    assert progress.total_items == 3
    assert progress.completed_count == 2
    assert progress.failed_count == 1
    assert progress.is_terminal is True

