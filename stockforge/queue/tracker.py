"""Durable per-image progress, stage tracking, and crash-resilient batch reporting (SF-016)."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from stockforge.db.models import Job, JobAttempt, JobItem, utc_now

logger = logging.getLogger("stockforge.queue.tracker")

VALID_STAGES: List[str] = [
    "research",
    "prompt",
    "generating",
    "download",
    "qc",
    "metadata",
    "review",
    "export",
]

STAGE_ORDER: Dict[str, int] = {stage: idx for idx, stage in enumerate(VALID_STAGES)}

VALID_ITEM_STATUSES: Set[str] = {
    "pending",
    "processing",
    "completed",
    "failed",
    "cancelled",
    "needs_reconciliation",
}

TERMINAL_ITEM_STATUSES: Set[str] = {
    "completed",
    "failed",
    "cancelled",
}


def sanitize_error_text(error: Optional[str]) -> Optional[str]:
    """Redact sensitive API keys, authorization headers, and secrets from error strings."""
    if not error:
        return None
    text = str(error)
    # Mask Bearer tokens or API keys
    text = re.sub(r"(Bearer\s+)([A-Za-z0-9_\-\.]+)", r"\1***REDACTED***", text, flags=re.IGNORECASE)
    text = re.sub(r"(sk-[a-zA-Z0-9_\-]{8,})", r"***REDACTED***", text)
    text = re.sub(r"([kK]ey|[sS]ecret|[tT]oken)=([^\s&]+)", r"\1=***REDACTED***", text)
    return text


@dataclass
class BatchProgressSummary:
    """Detailed and strictly accurate batch progress counters."""
    job_id: int
    total_items: int
    completed_count: int
    processing_count: int
    pending_count: int
    failed_count: int
    cancelled_count: int
    reconciliation_count: int
    current_item_index: Optional[int]
    current_stage: Optional[str]
    success_rate_percent: float
    is_terminal: bool
    summary_text: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "total_items": self.total_items,
            "completed": self.completed_count,
            "processing": self.processing_count,
            "pending": self.pending_count,
            "failed": self.failed_count,
            "cancelled": self.cancelled_count,
            "needs_reconciliation": self.reconciliation_count,
            "current_item_index": self.current_item_index,
            "current_stage": self.current_stage,
            "success_rate_percent": round(self.success_rate_percent, 1),
            "is_terminal": self.is_terminal,
            "summary_text": self.summary_text,
        }


class ItemProgressTracker:
    """Coordinates durable per-image stage updates, attempts, and crash recovery."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self.session_factory = session_factory

    def initialize_job_items(
        self,
        job_id: int,
        total_items: int,
        initial_stage: str = "prompt",
    ) -> List[JobItem]:
        """Create initial job_items entries (1 to total_items) for a batch job."""
        if total_items < 1:
            raise ValueError(f"total_items harus >= 1, ditemukan: {total_items}")

        with self.session_factory() as session:
            # Check existing items to prevent duplicates
            existing = session.scalars(
                select(JobItem).where(JobItem.job_id == job_id).order_by(JobItem.item_index.asc())
            ).all()
            if existing:
                return list(existing)

            items = []
            now = utc_now()
            for idx in range(1, total_items + 1):
                item = JobItem(
                    job_id=job_id,
                    item_index=idx,
                    stage=initial_stage,
                    status="pending",
                    created_at=now,
                    updated_at=now,
                )
                session.add(item)
                items.append(item)

            session.commit()
            for item in items:
                session.refresh(item)
            return items

    def update_item_stage(
        self,
        job_id: int,
        item_index: int,
        stage: str,
        status: str,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        asset_id: Optional[int] = None,
        last_error: Optional[str] = None,
    ) -> JobItem:
        """Persist state transition for a specific job item with strict transition invariants.
        
        Guarantees:
        - Rejects invalid stage or status names.
        - Terminal completed status cannot be overwritten by earlier stages.
        - Sanitizes last_error before saving.
        """
        if stage not in VALID_STAGES:
            raise ValueError(f"Stage '{stage}' tidak valid. Harus salah satu dari: {VALID_STAGES}")

        if status not in VALID_ITEM_STATUSES:
            raise ValueError(f"Status '{status}' tidak valid. Harus salah satu dari: {VALID_ITEM_STATUSES}")

        with self.session_factory() as session:
            stmt = select(JobItem).where(
                JobItem.job_id == job_id,
                JobItem.item_index == item_index,
            )
            item = session.scalars(stmt).first()
            if not item:
                # Create on demand if missing
                item = JobItem(
                    job_id=job_id,
                    item_index=item_index,
                    stage=stage,
                    status=status,
                    created_at=utc_now(),
                    updated_at=utc_now(),
                )
                session.add(item)

            # Invariant: Never downgrade a completed item
            if item.status == "completed" and status in ("pending", "processing"):
                logger.warning(
                    "Mencegah regresi item yang sudah sukses: job=%d, item=%d (status tetap 'completed')",
                    job_id,
                    item_index,
                )
                return item

            item.stage = stage
            item.status = status
            if provider:
                item.provider = provider
            if model:
                item.model = model
            if asset_id is not None:
                item.asset_id = asset_id
            if last_error is not None:
                item.last_error = sanitize_error_text(last_error)
            item.updated_at = utc_now()

            session.commit()
            session.refresh(item)
            return item

    def record_item_attempt(
        self,
        job_id: int,
        item_index: int,
        role: str,
        provider: str,
        model: str,
        attempt_number: int,
        outcome: str,
        request_id: Optional[str] = None,
        operation_id: Optional[str] = None,
        http_status: Optional[int] = None,
        normalized_error_code: Optional[str] = None,
        provider_error_code: Optional[str] = None,
        retry_after: Optional[int] = None,
        estimated_cost_cents: int = 0,
        actual_cost_cents: int = 0,
        started_at: Optional[datetime] = None,
        finished_at: Optional[datetime] = None,
    ) -> JobAttempt:
        """Record attempt details into job_attempts table linked to job_item."""
        with self.session_factory() as session:
            stmt = select(JobItem.id).where(
                JobItem.job_id == job_id,
                JobItem.item_index == item_index,
            )
            item_id = session.scalar(stmt)

            now = utc_now()
            attempt = JobAttempt(
                job_id=job_id,
                job_item_id=item_id,
                attempt_number=attempt_number,
                role=role,
                provider=provider,
                model=model,
                request_id=request_id,
                operation_id=operation_id,
                http_status=http_status,
                normalized_error_code=normalized_error_code,
                provider_error_code=provider_error_code,
                retry_after=retry_after,
                outcome=outcome,
                estimated_cost_cents=estimated_cost_cents,
                actual_cost_cents=actual_cost_cents,
                started_at=started_at or now,
                finished_at=finished_at or now,
            )
            session.add(attempt)
            session.commit()
            session.refresh(attempt)
            return attempt

    def get_batch_progress(self, job_id: int) -> BatchProgressSummary:
        """Calculate strictly accurate batch counters and stage progress.
        
        CRITICAL ACCURACY INVARIANT:
        An item in 'processing' or 'pending' is NOT complete.
        For example: 6 complete + 1 processing + 3 pending is 60% successful,
        NEVER reported as 70% successful.
        """
        with self.session_factory() as session:
            stmt = select(JobItem).where(JobItem.job_id == job_id).order_by(JobItem.item_index.asc())
            items = list(session.scalars(stmt).all())

            total = len(items)
            completed = sum(1 for i in items if i.status == "completed")
            processing = sum(1 for i in items if i.status == "processing")
            pending = sum(1 for i in items if i.status == "pending")
            failed = sum(1 for i in items if i.status == "failed")
            cancelled = sum(1 for i in items if i.status == "cancelled")
            reconciliation = sum(1 for i in items if i.status == "needs_reconciliation")

            # Determine currently active item
            active_item = next((i for i in items if i.status == "processing"), None)
            if not active_item:
                active_item = next((i for i in items if i.status == "pending"), None)

            current_idx = active_item.item_index if active_item else (total if total > 0 else None)
            current_stage = active_item.stage if active_item else (items[-1].stage if items else None)

            # Strict completion rate: ONLY truly completed items count towards success rate
            success_pct = (completed / total * 100.0) if total > 0 else 0.0

            # Terminal check: all items have reached a terminal status
            is_terminal = total > 0 and (completed + failed + cancelled == total)

            if is_terminal:
                summary = f"Batch selesai: {completed}/{total} sukses, {failed} gagal"
            elif active_item:
                summary = f"Gambar {current_idx}/{total} (Tahap: {current_stage}) — {completed} selesai, {processing} proses, {pending} antre"
            else:
                summary = f"Batch {job_id}: {completed}/{total} selesai ({success_pct:.0f}%)"

            return BatchProgressSummary(
                job_id=job_id,
                total_items=total,
                completed_count=completed,
                processing_count=processing,
                pending_count=pending,
                failed_count=failed,
                cancelled_count=cancelled,
                reconciliation_count=reconciliation,
                current_item_index=current_idx,
                current_stage=current_stage,
                success_rate_percent=success_pct,
                is_terminal=is_terminal,
                summary_text=summary,
            )

    def prepare_resumption_plan(self, job_id: int) -> List[int]:
        """Examine job items after a worker restart and return list of uncompleted item indices.
        
        Guarantees:
        - Already completed items are excluded (never rerun successful images).
        - Items left in 'processing' are reset to 'pending' to be safely resumed.
        """
        with self.session_factory() as session:
            stmt = select(JobItem).where(JobItem.job_id == job_id).order_by(JobItem.item_index.asc())
            items = list(session.scalars(stmt).all())

            to_resume: List[int] = []
            now = utc_now()

            for item in items:
                if item.status == "completed":
                    # Invariant: Never rerun completed images!
                    continue
                elif item.status == "processing":
                    # Worker crashed mid-processing -> reset to pending for resumption
                    item.status = "pending"
                    item.updated_at = now
                    to_resume.append(item.item_index)
                elif item.status in ("pending", "failed"):
                    to_resume.append(item.item_index)

            session.commit()
            logger.info("Resumption plan untuk Job #%d: melanjutkan %d item: %s", job_id, len(to_resume), to_resume)
            return to_resume
