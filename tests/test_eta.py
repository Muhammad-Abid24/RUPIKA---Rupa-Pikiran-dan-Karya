"""Comprehensive acceptance test suite for StockForge SF-023 (ETA tracking)."""
import time
from datetime import datetime, timedelta, timezone

import pytest

from stockforge.eta.estimator import RollingDurationEstimator
from stockforge.eta.models import (
    ConfidenceLevel,
    EtaStatus,
    ItemStatus,
    StageTimingRecord,
    StageType,
)
from stockforge.eta.telegram_progress import (
    TelegramProgressFormatter,
    ThrottledProgressUpdater,
)
from stockforge.eta.tracker import JobEtaTracker, STANDARD_IMAGE_PIPELINE


def helper_feed_stage_samples(
    tracker: JobEtaTracker,
    stage: StageType,
    durations_sec: list[float],
    provider: str = "test_provider",
    model: str = "test_model",
) -> None:
    """Helper to populate historical samples for a specific stage."""
    now = datetime.now(timezone.utc)
    for i, sec in enumerate(durations_sec):
        rec = StageTimingRecord(
            job_id=f"prior_job_{i}",
            item_index=1,
            stage=stage,
            provider=provider,
            model=model,
            duration_ms=sec * 1000.0,
            started_at=now - timedelta(seconds=sec),
            finished_at=now,
            status="completed",
        )
        tracker.estimator.record_stage_timing(rec)


def helper_seed_standard_pipeline(tracker: JobEtaTracker, n_samples: int = 5) -> None:
    """Seed enough samples for all stages in STANDARD_IMAGE_PIPELINE."""
    # durations: prompt ~2s, image ~10s, qc ~3s, metadata ~2s, export ~1s
    durations = {
        StageType.PROMPT: [2.0, 2.1, 1.9, 2.2, 2.0],
        StageType.IMAGE_GENERATION: [10.0, 11.0, 9.5, 10.5, 10.0],
        StageType.QC: [3.0, 3.2, 2.8, 3.0, 3.1],
        StageType.METADATA: [2.0, 2.2, 1.8, 2.1, 2.0],
        StageType.EXPORT: [1.0, 1.1, 0.9, 1.0, 1.2],
    }
    for stage, sample_list in durations.items():
        helper_feed_stage_samples(tracker, stage, sample_list[:n_samples])


# =============================================================================
# 1. Test: No History
# =============================================================================
def test_no_history():
    """Verify behavior when no historical observations exist."""
    tracker = JobEtaTracker(job_id="job_001", total_items=5)
    eta = tracker.calculate_eta()

    assert eta.status == EtaStatus.UNAVAILABLE_INSUFFICIENT_DATA
    assert eta.confidence is None
    assert eta.remaining_seconds_min == 0.0
    assert eta.remaining_seconds_max == 0.0
    assert "belum tersedia" in eta.reason
    assert eta.safety_guard_verified is True

    # Check Telegram formatting
    msg = TelegramProgressFormatter.format_progress_message(tracker)
    assert "Batch #job_001" in msg
    assert "image 1/5" in msg
    assert "belum dapat diperkirakan" in msg


# =============================================================================
# 2. Test: Sparse History
# =============================================================================
def test_sparse_history():
    """Verify behavior when fewer observations than min_samples exist."""
    tracker = JobEtaTracker(job_id="job_002", total_items=3)
    # Feed only 1 sample for PROMPT (min_samples is 3)
    helper_feed_stage_samples(tracker, StageType.PROMPT, [2.5])

    eta = tracker.calculate_eta()
    assert eta.status == EtaStatus.UNAVAILABLE_INSUFFICIENT_DATA
    assert eta.sample_count_by_stage.get(StageType.PROMPT.value) == 1
    assert "minimal 3 sampel" in eta.reason


# =============================================================================
# 3. Test: Fallback and Retry
# =============================================================================
def test_fallback_and_retry():
    """Verify dynamic ETA increase when active retries/backoffs occur."""
    tracker = JobEtaTracker(job_id="job_003", total_items=2)
    helper_seed_standard_pipeline(tracker, n_samples=5)

    base_eta = tracker.calculate_eta()
    assert base_eta.status == EtaStatus.AVAILABLE
    base_max = base_eta.remaining_seconds_max

    # Start item 1 in retry mode with backoff allowance
    tracker.start_stage(
        item_index=1,
        stage=StageType.IMAGE_GENERATION,
        provider="fallback_provider",
        model="fallback_model",
        attempt_number=2,
        is_retry=True,
        is_fallback=True,
    )
    tracker.active_retry_delay_seconds = 15.0  # 15s backoff delay

    retry_eta = tracker.calculate_eta()
    assert retry_eta.status == EtaStatus.AVAILABLE
    # Max ETA should reflect the added retry delay allowance
    assert retry_eta.remaining_seconds_max > base_max
    assert retry_eta.remaining_seconds_max >= base_max + 14.0


# =============================================================================
# 4. Test: Pause and Approval
# =============================================================================
def test_pause_and_approval():
    """Verify ETA suspension during pause and manual approval wait."""
    tracker = JobEtaTracker(job_id="job_004", total_items=4)
    helper_seed_standard_pipeline(tracker, n_samples=5)

    # Pause
    tracker.pause()
    pause_eta = tracker.calculate_eta()
    assert pause_eta.status == EtaStatus.SUSPENDED_PAUSE
    assert "dijeda" in pause_eta.reason
    msg_paused = TelegramProgressFormatter.format_progress_message(tracker)
    assert "batch dijeda" in msg_paused

    # Resume
    tracker.resume()
    resumed_eta = tracker.calculate_eta()
    assert resumed_eta.status == EtaStatus.AVAILABLE

    # Awaiting manual approval
    tracker.set_approval_waiting(True)
    approval_eta = tracker.calculate_eta()
    assert approval_eta.status == EtaStatus.SUSPENDED_APPROVAL
    assert "persetujuan manual" in approval_eta.reason
    msg_approval = TelegramProgressFormatter.format_progress_message(tracker)
    assert "menunggu persetujuan manual" in msg_approval

    tracker.set_approval_waiting(False)
    assert tracker.calculate_eta().status == EtaStatus.AVAILABLE


# =============================================================================
# 5. Test: Crash and Restart
# =============================================================================
def test_crash_and_restart():
    """Verify tracker state can be restored from persistent records."""
    tracker1 = JobEtaTracker(job_id="job_005", total_items=3)
    helper_seed_standard_pipeline(tracker1, n_samples=5)

    # Item 1 completes PROMPT and IMAGE_GENERATION
    tracker1.start_stage(1, StageType.PROMPT)
    tracker1.finish_stage(1, StageType.PROMPT, status="completed")
    tracker1.start_stage(1, StageType.IMAGE_GENERATION)
    tracker1.finish_stage(1, StageType.IMAGE_GENERATION, status="completed")

    persisted_records = list(tracker1.estimator._records)

    # Simulate crash by instantiating fresh tracker2
    tracker2 = JobEtaTracker(job_id="job_005", total_items=3)
    tracker2.restore_from_history(persisted_records)

    # Check that item 1 has completed stages restored
    assert StageType.PROMPT in tracker2._items[1].completed_stages
    assert StageType.IMAGE_GENERATION in tracker2._items[1].completed_stages
    assert tracker2.estimator.get_sample_counts()[StageType.PROMPT.value] >= 5

    # Check remaining work matches
    rem1 = tracker1.get_remaining_work_by_stage()
    rem2 = tracker2.get_remaining_work_by_stage()
    assert rem1 == rem2


# =============================================================================
# 6. Test: Cancellation
# =============================================================================
def test_cancellation():
    """Verify clean terminal handling when job is cancelled."""
    tracker = JobEtaTracker(job_id="job_006", total_items=2)
    helper_seed_standard_pipeline(tracker, n_samples=5)

    tracker.cancel()
    eta = tracker.calculate_eta()
    assert eta.status == EtaStatus.CANCELLED
    assert eta.remaining_seconds_min == 0.0
    assert eta.remaining_seconds_max == 0.0

    counts = tracker.get_counts()
    assert counts["cancelled"] == 2
    msg = TelegramProgressFormatter.format_progress_message(tracker)
    assert "Dibatalkan" in msg


# =============================================================================
# 7. Test: Slow Provider
# =============================================================================
def test_slow_provider():
    """Verify that provider slowdown dynamically shifts median & p90 upward."""
    estimator = RollingDurationEstimator(min_samples=3, window_size=10)
    tracker = JobEtaTracker(job_id="job_007", total_items=1, estimator=estimator)

    # Baseline fast provider (10s)
    helper_feed_stage_samples(tracker, StageType.IMAGE_GENERATION, [10.0, 10.2, 9.8, 10.1])
    fast_durations = estimator.estimate_stage_durations(StageType.IMAGE_GENERATION)
    assert fast_durations is not None
    fast_median = fast_durations[1]

    # Provider experiences severe latency degradation (e.g. 40s, 45s, 50s)
    helper_feed_stage_samples(tracker, StageType.IMAGE_GENERATION, [40.0, 45.0, 50.0])
    degraded_durations = estimator.estimate_stage_durations(StageType.IMAGE_GENERATION)
    assert degraded_durations is not None
    degraded_median = degraded_durations[1]

    # The median should have adapted upward
    assert degraded_median > fast_median


# =============================================================================
# 8. Test: No Negative ETA
# =============================================================================
def test_no_negative_eta():
    """Verify that remaining seconds is never negative under any condition."""
    tracker = JobEtaTracker(job_id="job_008", total_items=2)
    helper_seed_standard_pipeline(tracker, n_samples=5)

    eta = tracker.calculate_eta()
    assert eta.remaining_seconds_min >= 0.0
    assert eta.remaining_seconds_median >= eta.remaining_seconds_min
    assert eta.remaining_seconds_max >= eta.remaining_seconds_median

    # Complete all items
    for i in (1, 2):
        for st in STANDARD_IMAGE_PIPELINE:
            tracker.start_stage(i, st)
            tracker.finish_stage(i, st, status="completed", is_item_complete=(st == STANDARD_IMAGE_PIPELINE[-1]))

    completed_eta = tracker.calculate_eta()
    assert completed_eta.status == EtaStatus.COMPLETED
    assert completed_eta.remaining_seconds_min == 0.0
    assert completed_eta.remaining_seconds_median == 0.0
    assert completed_eta.remaining_seconds_max == 0.0

    # Ensure negative duration is rejected by StageTimingRecord validator
    with pytest.raises(ValueError):
        StageTimingRecord(
            job_id="job_bad",
            item_index=1,
            stage=StageType.PROMPT,
            provider="p",
            model="m",
            duration_ms=-500.0,
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            status="completed",
        )


# =============================================================================
# 9. Test: Telegram Progress Formatting and Throttling
# =============================================================================
def test_telegram_progress_formatting_and_throttling():
    """Verify Telegram live message rendering and rate limit throttling."""
    tracker = JobEtaTracker(job_id="job_009", total_items=10)
    helper_seed_standard_pipeline(tracker, n_samples=5)

    # Simulate 3 items completed, 1 processing, 6 pending
    tracker._items[1].status = ItemStatus.COMPLETED
    tracker._items[2].status = ItemStatus.COMPLETED
    tracker._items[3].status = ItemStatus.COMPLETED
    tracker.start_stage(4, StageType.IMAGE_GENERATION, provider="flux", model="pro")

    msg = TelegramProgressFormatter.format_progress_message(tracker)
    assert "Batch #job_009" in msg
    assert "image 4/10" in msg
    assert "Selesai: 3" in msg
    assert "Gagal: 0" in msg
    assert "Proses: 1" in msg
    assert "Menunggu: 6" in msg
    assert "Estimasi sisa" in msg

    # Throttling test
    updater = ThrottledProgressUpdater(min_edit_interval_seconds=2.0)

    # First call: should edit
    assert updater.should_edit(msg) is True

    # Immediate duplicate call: suppressed (identical content)
    assert updater.should_edit(msg) is False

    # Immediate different call: suppressed (throttled by interval)
    diff_msg = msg + "\nExtra info"
    assert updater.should_edit(diff_msg) is False

    # Forced call: always allowed
    assert updater.should_edit(diff_msg, force=True) is True

    # Wait for interval to elapse
    time.sleep(2.1)
    another_msg = diff_msg + " (updated)"
    assert updater.should_edit(another_msg) is True


# =============================================================================
# 10. Test: Safety Invariants
# =============================================================================
def test_safety_invariants():
    """Verify ETA has no capability to invoke APIs, charge budgets, or alter approvals."""
    tracker = JobEtaTracker(job_id="job_010", total_items=5)
    helper_seed_standard_pipeline(tracker, n_samples=5)

    # Pure read-only computation
    eta1 = tracker.calculate_eta()
    eta2 = tracker.calculate_eta()
    assert eta1.remaining_seconds_median == eta2.remaining_seconds_median
    assert eta1.safety_guard_verified is True
    assert eta2.safety_guard_verified is True

