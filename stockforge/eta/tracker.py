"""Job ETA tracker managing batch lifecycle and stage transitions (SF-023)."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

from stockforge.eta.estimator import RollingDurationEstimator
from stockforge.eta.models import (
    ConfidenceLevel,
    EtaEstimate,
    EtaStatus,
    ItemStatus,
    StageTimingRecord,
    StageType,
)


# Standard stages executed per image item
STANDARD_IMAGE_PIPELINE: List[StageType] = [
    StageType.PROMPT,
    StageType.IMAGE_GENERATION,
    StageType.QC,
    StageType.METADATA,
    StageType.EXPORT,
]


class ItemStageState:
    """Tracks state and progress of an individual item in a batch."""
    def __init__(self, index: int) -> None:
        self.index = index
        self.status: ItemStatus = ItemStatus.PENDING
        self.completed_stages: Set[StageType] = set()
        self.current_stage: Optional[StageType] = None
        self.current_stage_start_monotonic: Optional[float] = None
        self.current_stage_start_utc: Optional[datetime] = None
        self.active_provider: str = ""
        self.active_model: str = ""
        self.attempt_number: int = 1
        self.is_retry: bool = False
        self.is_fallback: bool = False
        self.last_error: str = ""


class JobEtaTracker:
    """Manages runtime ETA calculations and stage timing persistence for a batch job.
    
    Invariants:
    - Suspends ETA during manual approval, pause, provider outage, or ambiguous paid calls.
    - Recalculates remaining duration dynamically upon stage transitions, retries, and fallbacks.
    - Preserves timing measurements across restarts via load_records().
    - Never triggers billable API requests, overrides approvals, or submits to Adobe Stock.
    - Guarantees non-negative remaining durations.
    """

    def __init__(
        self,
        job_id: str,
        total_items: int,
        estimator: Optional[RollingDurationEstimator] = None,
        pipeline_stages: Optional[List[StageType]] = None,
    ) -> None:
        self.job_id = job_id
        self.total_items = max(1, total_items)
        self.estimator = estimator or RollingDurationEstimator()
        self.pipeline_stages = list(pipeline_stages or STANDARD_IMAGE_PIPELINE)

        # Items state
        self._items: Dict[int, ItemStageState] = {
            i: ItemStageState(i) for i in range(1, self.total_items + 1)
        }

        # Suspension / status flags
        self.is_paused: bool = False
        self.is_awaiting_approval: bool = False
        self.provider_outage: bool = False
        self.provider_outage_details: str = ""
        self.ambiguous_paid_call: bool = False
        self.is_cancelled: bool = False
        self.is_failed: bool = False

        # Additional delay allowances (e.g. from active retries/backoffs)
        self.active_retry_delay_seconds: float = 0.0

        # Persisted stage timing records collected by this job
        self.job_timing_records: List[StageTimingRecord] = []

    # -------------------------------------------------------------------------
    # Suspension and Status Controls
    # -------------------------------------------------------------------------

    def pause(self) -> None:
        """Pause job execution; suspends ETA."""
        self.is_paused = True

    def resume(self) -> None:
        """Resume job execution; clears pause suspension."""
        self.is_paused = False

    def set_approval_waiting(self, waiting: bool) -> None:
        """Indicate whether the job is waiting for explicit manual approval."""
        self.is_awaiting_approval = waiting

    def set_provider_outage(self, outage: bool, details: str = "") -> None:
        """Indicate provider outage/unavailability."""
        self.provider_outage = outage
        self.provider_outage_details = details

    def set_ambiguous_call(self, ambiguous: bool) -> None:
        """Indicate an in-flight paid call requires reconciliation before retry."""
        self.ambiguous_paid_call = ambiguous

    def cancel(self) -> None:
        """Mark job as cancelled."""
        self.is_cancelled = True
        for item in self._items.values():
            if item.status not in (ItemStatus.COMPLETED, ItemStatus.FAILED):
                item.status = ItemStatus.CANCELLED

    def fail(self, reason: str = "") -> None:
        """Mark job as failed."""
        self.is_failed = True

    # -------------------------------------------------------------------------
    # Stage Transitions
    # -------------------------------------------------------------------------

    def start_stage(
        self,
        item_index: int,
        stage: StageType,
        provider: str = "default",
        model: str = "default",
        attempt_number: int = 1,
        is_retry: bool = False,
        is_fallback: bool = False,
    ) -> None:
        """Record the start of a stage for an item."""
        if item_index not in self._items:
            raise KeyError(f"Invalid item_index {item_index}; job has {self.total_items} items")

        item = self._items[item_index]
        item.status = ItemStatus.PROCESSING
        item.current_stage = stage
        item.current_stage_start_monotonic = time.monotonic()
        item.current_stage_start_utc = datetime.now(timezone.utc)
        item.active_provider = provider
        item.active_model = model
        item.attempt_number = attempt_number
        item.is_retry = is_retry
        item.is_fallback = is_fallback

    def finish_stage(
        self,
        item_index: int,
        stage: StageType,
        status: str = "completed",
        is_item_complete: bool = False,
        error_message: str = "",
    ) -> StageTimingRecord:
        """Record completion or failure of a stage and persist timing telemetry."""
        if item_index not in self._items:
            raise KeyError(f"Invalid item_index {item_index}")

        item = self._items[item_index]
        now_utc = datetime.now(timezone.utc)
        now_monotonic = time.monotonic()

        # Calculate monotonic duration if started, else fallback
        if item.current_stage_start_monotonic is not None:
            elapsed_ms = max(0.0, (now_monotonic - item.current_stage_start_monotonic) * 1000.0)
        else:
            elapsed_ms = 0.0

        started_utc = item.current_stage_start_utc or now_utc

        record = StageTimingRecord(
            job_id=self.job_id,
            item_index=item_index,
            stage=stage,
            provider=item.active_provider or "unknown",
            model=item.active_model or "unknown",
            attempt_number=item.attempt_number,
            duration_ms=elapsed_ms,
            started_at=started_utc,
            finished_at=now_utc,
            status=status,
            is_retry=item.is_retry,
            is_fallback=item.is_fallback,
        )

        self.job_timing_records.append(record)
        self.estimator.record_stage_timing(record)

        # Update item state
        if status == "completed":
            item.completed_stages.add(stage)
            item.last_error = ""
            if is_item_complete or (item.completed_stages >= set(self.pipeline_stages)):
                item.status = ItemStatus.COMPLETED
                item.current_stage = None
        else:
            item.last_error = error_message
            if status == "failed":
                item.status = ItemStatus.FAILED
                item.current_stage = None

        item.current_stage_start_monotonic = None
        item.current_stage_start_utc = None
        return record

    # -------------------------------------------------------------------------
    # Batch Counters
    # -------------------------------------------------------------------------

    def get_counts(self) -> Dict[str, int]:
        """Return counts of items by state."""
        completed = sum(1 for it in self._items.values() if it.status == ItemStatus.COMPLETED)
        failed = sum(1 for it in self._items.values() if it.status == ItemStatus.FAILED)
        processing = sum(1 for it in self._items.values() if it.status == ItemStatus.PROCESSING)
        cancelled = sum(1 for it in self._items.values() if it.status == ItemStatus.CANCELLED)
        pending = sum(1 for it in self._items.values() if it.status == ItemStatus.PENDING)
        return {
            "total": self.total_items,
            "completed": completed,
            "failed": failed,
            "processing": processing,
            "cancelled": cancelled,
            "pending": pending,
        }

    def get_active_stage_and_item(self) -> tuple[int, Optional[StageType]]:
        """Return current 1-based active item index and stage."""
        for idx in range(1, self.total_items + 1):
            it = self._items[idx]
            if it.status == ItemStatus.PROCESSING:
                return (idx, it.current_stage)
            if it.status == ItemStatus.PENDING:
                return (idx, None)
        return (self.total_items, None)

    # -------------------------------------------------------------------------
    # Remaining Work Calculation
    # -------------------------------------------------------------------------

    def get_remaining_work_by_stage(self) -> Dict[StageType, int]:
        """Compute the count of incomplete stages across all non-terminal items."""
        remaining: Dict[StageType, int] = {st: 0 for st in self.pipeline_stages}
        for item in self._items.values():
            if item.status in (ItemStatus.COMPLETED, ItemStatus.FAILED, ItemStatus.CANCELLED):
                continue
            for stage in self.pipeline_stages:
                if stage not in item.completed_stages:
                    remaining[stage] += 1
        return remaining

    # -------------------------------------------------------------------------
    # ETA Calculation
    # -------------------------------------------------------------------------

    def calculate_eta(self) -> EtaEstimate:
        """Compute the current ETA based on job state and observed stage telemetry."""
        now_utc = datetime.now(timezone.utc)
        counts = self.get_counts()
        sample_counts = self.estimator.get_sample_counts()

        # Terminal conditions
        if self.is_cancelled:
            return EtaEstimate(
                status=EtaStatus.CANCELLED,
                remaining_seconds_min=0.0,
                remaining_seconds_median=0.0,
                remaining_seconds_max=0.0,
                reason="Job dibatalkan oleh operator",
                updated_at=now_utc,
                sample_count_by_stage=sample_counts,
            )

        if self.is_failed:
            return EtaEstimate(
                status=EtaStatus.FAILED,
                remaining_seconds_min=0.0,
                remaining_seconds_median=0.0,
                remaining_seconds_max=0.0,
                reason="Job gagal mencapai status terminal sukses",
                updated_at=now_utc,
                sample_count_by_stage=sample_counts,
            )

        if counts["completed"] + counts["failed"] >= self.total_items:
            return EtaEstimate(
                status=EtaStatus.COMPLETED,
                confidence=ConfidenceLevel.HIGH,
                remaining_seconds_min=0.0,
                remaining_seconds_median=0.0,
                remaining_seconds_max=0.0,
                reason="Semua item telah diproses",
                updated_at=now_utc,
                sample_count_by_stage=sample_counts,
            )

        # Suspension checks
        if self.is_awaiting_approval:
            return EtaEstimate(
                status=EtaStatus.SUSPENDED_APPROVAL,
                remaining_seconds_min=0.0,
                remaining_seconds_median=0.0,
                remaining_seconds_max=0.0,
                reason="ETA ditangguhkan: menunggu persetujuan manual operator",
                updated_at=now_utc,
                sample_count_by_stage=sample_counts,
            )

        if self.is_paused:
            return EtaEstimate(
                status=EtaStatus.SUSPENDED_PAUSE,
                remaining_seconds_min=0.0,
                remaining_seconds_median=0.0,
                remaining_seconds_max=0.0,
                reason="ETA ditangguhkan: batch dijeda",
                updated_at=now_utc,
                sample_count_by_stage=sample_counts,
            )

        if self.provider_outage:
            return EtaEstimate(
                status=EtaStatus.SUSPENDED_PROVIDER_OUTAGE,
                remaining_seconds_min=0.0,
                remaining_seconds_median=0.0,
                remaining_seconds_max=0.0,
                reason=f"ETA ditangguhkan: gangguan provider ({self.provider_outage_details or 'outage'})",
                updated_at=now_utc,
                sample_count_by_stage=sample_counts,
            )

        if self.ambiguous_paid_call:
            return EtaEstimate(
                status=EtaStatus.SUSPENDED_AMBIGUOUS_CALL,
                remaining_seconds_min=0.0,
                remaining_seconds_median=0.0,
                remaining_seconds_max=0.0,
                reason="ETA ditangguhkan: rekonsiliasi transaksi pembayaran ambigu",
                updated_at=now_utc,
                sample_count_by_stage=sample_counts,
            )

        # Calculate remaining work
        remaining_stages = self.get_remaining_work_by_stage()
        active_retries = sum(
            1 for it in self._items.values() if it.status == ItemStatus.PROCESSING and it.is_retry
        )

        estimate = self.estimator.estimate_remaining(
            remaining_work_by_stage=remaining_stages,
            active_retries=active_retries,
            retry_delay_allowance_sec=self.active_retry_delay_seconds,
        )

        return estimate

    # -------------------------------------------------------------------------
    # Crash / Restart Restoration
    # -------------------------------------------------------------------------

    def restore_from_history(
        self,
        historical_records: Sequence[StageTimingRecord],
        item_states: Optional[Dict[int, ItemStatus]] = None,
    ) -> None:
        """Reconstitute state after a worker crash or process restart."""
        self.estimator.load_records(historical_records)
        self.job_timing_records = [r for r in historical_records if r.job_id == self.job_id]

        # Reconstruct completed stages per item from historical records for this job
        for rec in self.job_timing_records:
            if rec.item_index in self._items and rec.status == "completed":
                self._items[rec.item_index].completed_stages.add(rec.stage)

        # Apply known item statuses if provided
        if item_states:
            for idx, status in item_states.items():
                if idx in self._items:
                    self._items[idx].status = status
