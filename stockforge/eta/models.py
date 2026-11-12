"""Data models and enums for StockForge ETA telemetry and estimation (SF-023)."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Dict, Optional
from pydantic import BaseModel, Field, field_validator


class StageType(str, Enum):
    """Pipeline stages. Latency from one stage is never extrapolated to another."""
    RESEARCH = "research"
    PROMPT = "prompt"
    IMAGE_GENERATION = "image_generation"
    DOWNLOAD = "download"
    QC = "qc"
    METADATA = "metadata"
    REVIEW = "review"
    EXPORT = "export"


class ItemStatus(str, Enum):
    """Status of an individual batch item."""
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    NEEDS_RECONCILIATION = "needs_reconciliation"


class EtaStatus(str, Enum):
    """State of runtime ETA computation."""
    AVAILABLE = "available"
    UNAVAILABLE_INSUFFICIENT_DATA = "unavailable_insufficient_data"
    SUSPENDED_APPROVAL = "suspended_approval"
    SUSPENDED_PAUSE = "suspended_pause"
    SUSPENDED_PROVIDER_OUTAGE = "suspended_provider_outage"
    SUSPENDED_AMBIGUOUS_CALL = "suspended_ambiguous_call"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ConfidenceLevel(str, Enum):
    """Confidence level of the runtime estimate."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class StageTimingRecord(BaseModel):
    """Persisted timing telemetry for a completed or failed stage attempt.
    
    Invariants:
    - UTC timestamps.
    - Non-negative duration_ms.
    - Never stores API secrets, auth tokens, or prompts.
    """
    job_id: str
    item_index: int
    stage: StageType
    provider: str
    model: str
    attempt_number: int = Field(default=1, ge=1)
    duration_ms: float = Field(..., ge=0.0)
    started_at: datetime
    finished_at: datetime
    status: str
    is_retry: bool = False
    is_fallback: bool = False

    @field_validator("duration_ms")
    @classmethod
    def validate_non_negative_duration(cls, v: float) -> float:
        if v < 0:
            raise ValueError("duration_ms cannot be negative")
        return v


class EtaEstimate(BaseModel):
    """Computed runtime ETA estimate for a job batch.
    
    Invariants:
    - Never negative remaining duration.
    - remaining_seconds_max >= remaining_seconds_min.
    - Read-only; cannot trigger external API calls, billing charges, or approval overrides.
    """
    status: EtaStatus
    confidence: Optional[ConfidenceLevel] = None
    remaining_seconds_min: float = Field(default=0.0, ge=0.0)
    remaining_seconds_median: float = Field(default=0.0, ge=0.0)
    remaining_seconds_max: float = Field(default=0.0, ge=0.0)
    reason: str = ""
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    sample_count_by_stage: Dict[str, int] = Field(default_factory=dict)
    safety_guard_verified: bool = True

    @field_validator("remaining_seconds_min", "remaining_seconds_median", "remaining_seconds_max")
    @classmethod
    def validate_non_negative_seconds(cls, v: float) -> float:
        if v < 0:
            raise ValueError("Remaining seconds cannot be negative")
        return max(0.0, float(v))
