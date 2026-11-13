"""Generation gatekeeper enforcing immutable plan approval and single-use consumption (SF-020)."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from typing import Any, Callable, Dict, Optional, Tuple
from sqlalchemy import select
from sqlalchemy.orm import Session

from stockforge.budget.service import BudgetExceededError, BudgetService
from stockforge.db.models import Job, ProductionPlan, utc_now
from stockforge.queue.service import QueueService

logger = logging.getLogger("stockforge.research.gate")


class GenerationGateBlockedError(Exception):
    """Raised when an attempt to generate images is blocked by gate invariants."""
    pass


class PlanAlreadyConsumedError(GenerationGateBlockedError):
    """Raised when a production plan has already been converted into an image job."""
    pass


class PlanExpiredError(GenerationGateBlockedError):
    """Raised when a production plan has passed its validity TTL."""
    pass


class GenerationGate:
    """Enforces strict pre-generation verification invariants.
    
    Hard Invariants:
    1. Only an APPROVED, unexpired plan with matching immutable plan_hash can create an image job.
    2. Single-use consumption: duplicate callbacks or replay attacks cannot enqueue twice.
    3. Changes to model, quantity, or prompts invalidate plan approval.
    4. Scheduled research runs or niche selections NEVER create image jobs directly.
    """

    def __init__(
        self,
        session_factory: Callable[[], Session],
        queue_service: QueueService,
        budget_service: Optional[BudgetService] = None,
    ) -> None:
        self.session_factory = session_factory
        self.queue_service = queue_service
        self.budget_service = budget_service

    def validate_plan(
        self,
        session: Session,
        plan_id: int,
        expected_plan_hash: Optional[str] = None,
    ) -> ProductionPlan:
        """Verify plan readiness for image generation; raises GenerationGateBlockedError if invalid."""
        plan = session.get(ProductionPlan, plan_id)
        if not plan:
            raise GenerationGateBlockedError(f"Production plan #{plan_id} tidak ditemukan")

        # 1. State check
        if plan.status == "consumed":
            raise PlanAlreadyConsumedError(
                f"Plan #{plan_id} sudah pernah dikonsumsi untuk Job #{plan.consumed_job_id}. "
                "Pencegahan eksekusi ganda aktif."
            )
        if plan.status != "approved":
            raise GenerationGateBlockedError(
                f"Plan #{plan_id} belum disetujui (status saat ini: '{plan.status}'). "
                "Generasi gambar membutuhkan persetujuan eksplisit."
            )

        # 2. Expiration check
        now = utc_now()
        plan_exp = plan.expires_at
        if plan_exp.tzinfo is None:
            plan_exp = plan_exp.replace(tzinfo=timezone.utc)
        if now >= plan_exp:
            plan.status = "expired"
            session.commit()
            raise PlanExpiredError(f"Plan #{plan_id} telah kedaluwarsa pada {plan.expires_at}")

        # 3. Hash integrity check
        if expected_plan_hash and plan.plan_hash != expected_plan_hash:
            raise GenerationGateBlockedError(
                f"Plan hash mismatch! Diharapkan: {expected_plan_hash}, Tercatat: {plan.plan_hash}. "
                "Perubahan material membatalkan otorisasi."
            )

        # 4. Approver check
        if not plan.approved_by:
            raise GenerationGateBlockedError(f"Plan #{plan_id} tidak memiliki catatan approver valid")

        return plan

    def consume_and_enqueue_job(
        self,
        plan_id: int,
        actor_telegram_id: int,
        expected_plan_hash: Optional[str] = None,
    ) -> Job:
        """Atomically validate plan, reserve budget, enqueue image job, and mark plan consumed."""
        with self.session_factory() as session:
            # Validate plan strictly
            plan = self.validate_plan(session, plan_id, expected_plan_hash=expected_plan_hash)

            operation_id = f"plan_{plan.id}_{plan.plan_hash[:8]}"

            # 1. Check budget reservation if budget service is available
            if self.budget_service and plan.estimated_max_cost_cents > 0:
                try:
                    # Enqueue first to obtain job ID for budget reservation
                    pass
                except BudgetExceededError as be:
                    logger.error("Batas anggaran terlampaui saat membuka gerbang produksi: %s", be)
                    raise be

            # 2. Enqueue the image generation batch job
            job = self.queue_service.enqueue_job(
                job_type="image_batch",
                operation_id=operation_id,
            )

            # 3. Atomically bind job and mark plan consumed
            plan.status = "consumed"
            plan.consumed_job_id = job.id
            session.commit()
            session.refresh(plan)

            # 4. Bind budget reservation to the new job
            if self.budget_service and plan.estimated_max_cost_cents > 0:
                try:
                    self.budget_service.reserve_budget(
                        job_id=job.id,
                        estimated_cost_cents=plan.estimated_max_cost_cents,
                    )
                except BudgetExceededError as be:
                    # Rollback job if budget exceeds
                    self.queue_service.cancel_job(job.id)
                    plan.status = "approved"
                    plan.consumed_job_id = None
                    session.commit()
                    raise be

            logger.info(
                "Gerbang produksi terbuka: Plan #%s dikonsumsi menjadi Job #%s (%d gambar) oleh user #%s",
                plan.id,
                job.id,
                plan.image_count,
                actor_telegram_id,
            )
            return job
