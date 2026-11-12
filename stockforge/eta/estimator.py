"""Rolling duration estimator for StockForge pipeline stages (SF-023)."""
from __future__ import annotations

import math
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Deque, Dict, List, Optional, Sequence

from stockforge.eta.models import (
    ConfidenceLevel,
    EtaEstimate,
    EtaStatus,
    StageTimingRecord,
    StageType,
)


class RollingDurationEstimator:
    """Robust rolling duration estimator using median and conservative percentiles.
    
    Invariants & rules:
    - Stages are tracked independently: research, prompt, image_generation, download, qc, metadata, export.
    - Latency from one stage is never extrapolated to other stages.
    - Percentage of pipeline steps is NEVER treated as proportional to wall-clock time.
    - Returns UNAVAILABLE_INSUFFICIENT_DATA when observations are insufficient (< min_samples).
    - Conservative percentile (p85/p90) used for remaining_seconds_max.
    - All remaining time values are clamped to >= 0.0 (no negative ETA).
    - Side-effect free: cannot initiate network requests, billable calls, or approval changes.
    """

    def __init__(
        self,
        min_samples: int = 3,
        window_size: int = 50,
        default_concurrency: int = 1,
    ) -> None:
        self.min_samples = max(1, min_samples)
        self.window_size = max(5, window_size)
        self.concurrency = max(1, default_concurrency)
        # Store rolling durations in seconds keyed by (stage, provider, model) and by stage
        self._history_by_stage: Dict[StageType, Deque[float]] = defaultdict(
            lambda: deque(maxlen=self.window_size)
        )
        self._records: List[StageTimingRecord] = []

    def record_stage_timing(self, record: StageTimingRecord) -> None:
        """Record a completed stage timing observation in seconds."""
        duration_sec = max(0.0, record.duration_ms / 1000.0)
        self._history_by_stage[record.stage].append(duration_sec)
        self._records.append(record)

    def load_records(self, records: Sequence[StageTimingRecord]) -> None:
        """Load historical records (e.g. after restart or database restore)."""
        self.clear()
        for rec in records:
            self.record_stage_timing(rec)

    def clear(self) -> None:
        """Clear all in-memory rolling history."""
        self._history_by_stage.clear()
        self._records.clear()

    def get_sample_counts(self) -> Dict[str, int]:
        """Return the number of observed samples for each stage."""
        return {stage.value: len(samples) for stage, samples in self._history_by_stage.items()}

    def _calculate_percentile(self, values: Sequence[float], percentile: float) -> float:
        """Calculate percentile from a sorted sequence (0.0 to 1.0)."""
        if not values:
            return 0.0
        n = len(values)
        if n == 1:
            return values[0]
        idx = (n - 1) * percentile
        lower = math.floor(idx)
        upper = math.ceil(idx)
        if lower == upper:
            return values[int(lower)]
        fraction = idx - lower
        return values[int(lower)] * (1.0 - fraction) + values[int(upper)] * fraction

    def estimate_stage_durations(self, stage: StageType) -> Optional[tuple[float, float, float]]:
        """Compute (p25, median, p90) in seconds for a given stage.
        
        Returns None if observations < min_samples.
        """
        samples = list(self._history_by_stage.get(stage, []))
        if len(samples) < self.min_samples:
            return None
        sorted_samples = sorted(samples)
        p25 = self._calculate_percentile(sorted_samples, 0.25)
        median = self._calculate_percentile(sorted_samples, 0.50)
        p90 = self._calculate_percentile(sorted_samples, 0.90)
        return (max(0.0, p25), max(0.0, median), max(0.0, p90))

    def estimate_remaining(
        self,
        remaining_work_by_stage: Dict[StageType, int],
        active_retries: int = 0,
        retry_delay_allowance_sec: float = 0.0,
    ) -> EtaEstimate:
        """Estimate total remaining time for outstanding stages across items.
        
        remaining_work_by_stage: mapping of stage to number of pending/in-flight occurrences.
        active_retries: number of currently active retry attempts.
        retry_delay_allowance_sec: additional backoff delay incurred by active retries/fallbacks.
        """
        now_utc = datetime.now(timezone.utc)
        sample_counts = self.get_sample_counts()

        # Filter to stages that have work remaining
        active_stages = {
            stage: count
            for stage, count in remaining_work_by_stage.items()
            if count > 0
        }

        # If no work remains, ETA is 0.0 completed
        if not active_stages:
            return EtaEstimate(
                status=EtaStatus.COMPLETED,
                confidence=ConfidenceLevel.HIGH,
                remaining_seconds_min=0.0,
                remaining_seconds_median=0.0,
                remaining_seconds_max=0.0,
                reason="Semua tahap telah selesai",
                updated_at=now_utc,
                sample_count_by_stage=sample_counts,
            )

        # Check for insufficient observations
        missing_data_stages: List[str] = []
        for stage in active_stages:
            if len(self._history_by_stage.get(stage, [])) < self.min_samples:
                missing_data_stages.append(stage.value)

        if missing_data_stages:
            return EtaEstimate(
                status=EtaStatus.UNAVAILABLE_INSUFFICIENT_DATA,
                confidence=None,
                remaining_seconds_min=0.0,
                remaining_seconds_median=0.0,
                remaining_seconds_max=0.0,
                reason=(
                    f"ETA belum tersedia: data observasi belum cukup untuk tahap: "
                    f"{', '.join(missing_data_stages)} (minimal {self.min_samples} sampel)"
                ),
                updated_at=now_utc,
                sample_count_by_stage=sample_counts,
            )

        # Calculate sums per stage
        total_p25 = 0.0
        total_median = 0.0
        total_p90 = 0.0
        variances: List[float] = []

        for stage, count in active_stages.items():
            durations = self.estimate_stage_durations(stage)
            assert durations is not None
            p25, median, p90 = durations
            total_p25 += p25 * count
            total_median += median * count
            total_p90 += p90 * count

            # Track dispersion for confidence calculation
            if median > 0:
                dispersion = (p90 - p25) / median
                variances.append(dispersion)
            else:
                variances.append(0.0)

        # Adjust for concurrency (sequential by default, concurrency=1)
        # Sequential processing: sum of stage durations / concurrency
        divisor = float(self.concurrency)
        adj_min = total_p25 / divisor
        adj_median = total_median / divisor
        adj_max = total_p90 / divisor

        # Add allowances for active retries and fallbacks
        if active_retries > 0 or retry_delay_allowance_sec > 0:
            adj_max += max(0.0, retry_delay_allowance_sec)
            adj_median += max(0.0, retry_delay_allowance_sec * 0.5)

        # Guard against negative values and inversion
        final_min = max(0.0, adj_min)
        final_median = max(final_min, adj_median)
        final_max = max(final_median, adj_max)

        # Determine confidence
        avg_dispersion = sum(variances) / len(variances) if variances else 0.0
        min_observed_samples = min(
            len(self._history_by_stage.get(stage, [])) for stage in active_stages
        )

        if min_observed_samples >= 5 and avg_dispersion <= 0.40:
            confidence = ConfidenceLevel.HIGH
        elif min_observed_samples >= 3 and avg_dispersion <= 0.85:
            confidence = ConfidenceLevel.MEDIUM
        else:
            confidence = ConfidenceLevel.LOW

        return EtaEstimate(
            status=EtaStatus.AVAILABLE,
            confidence=confidence,
            remaining_seconds_min=round(final_min, 1),
            remaining_seconds_median=round(final_median, 1),
            remaining_seconds_max=round(final_max, 1),
            reason="Dihitung dari riwayat durasi bertahap",
            updated_at=now_utc,
            sample_count_by_stage=sample_counts,
        )
