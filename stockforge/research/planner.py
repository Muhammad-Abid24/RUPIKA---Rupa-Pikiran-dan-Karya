"""Production planning, immutable plan hashing, and approval state machine (SF-020)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from typing import Any, Callable, Dict, List, Optional
from sqlalchemy import select
from sqlalchemy.orm import Session

from stockforge.db.models import ProductionPlan, utc_now

logger = logging.getLogger("stockforge.research.planner")


class PlanStateError(Exception):
    """Raised for invalid production plan state transitions."""
    pass


class PlanIntegrityError(Exception):
    """Raised when plan hash mismatch or tampering is detected."""
    pass


def compute_plan_hash(
    research_run_id: int,
    selected_niches: List[str],
    concepts: List[Dict[str, Any]],
    image_count: int,
    provider_routes: Dict[str, Any],
    estimated_max_cost_cents: int,
) -> str:
    """Compute deterministic SHA-256 hash over immutable production plan properties."""
    payload = {
        "research_run_id": research_run_id,
        "selected_niches": sorted(selected_niches),
        "concepts": sorted(
            [
                {
                    "title": c.get("title", ""),
                    "prompt": c.get("prompt", ""),
                    "fingerprint": c.get("fingerprint", ""),
                }
                for c in concepts
            ],
            key=lambda x: x["fingerprint"] or x["title"],
        ),
        "image_count": image_count,
        "provider_routes": {k: provider_routes[k] for k in sorted(provider_routes.keys())},
        "estimated_max_cost_cents": estimated_max_cost_cents,
    }
    canonical_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


class ProductionPlanner:
    """Manages production plan creation, hashing, and state transitions."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self.session_factory = session_factory

    def create_plan(
        self,
        research_run_id: int,
        selected_niches: List[str],
        concepts: List[Dict[str, Any]],
        image_count: int,
        provider_routes: Dict[str, Any],
        estimated_max_cost_cents: int,
        expires_in_hours: int = 24,
    ) -> ProductionPlan:
        """Create a new production plan in 'awaiting_approval' status."""
        if image_count <= 0:
            raise PlanStateError("image_count harus bernilai >= 1")
        if not selected_niches:
            raise PlanStateError("selected_niches tidak boleh kosong")

        plan_hash = compute_plan_hash(
            research_run_id=research_run_id,
            selected_niches=selected_niches,
            concepts=concepts,
            image_count=image_count,
            provider_routes=provider_routes,
            estimated_max_cost_cents=estimated_max_cost_cents,
        )

        now = utc_now()
        expires_at = now + timedelta(hours=expires_in_hours)

        with self.session_factory() as session:
            # Check if identical active plan already exists
            existing = session.scalar(
                select(ProductionPlan).where(
                    ProductionPlan.plan_hash == plan_hash,
                    ProductionPlan.status.in_(["draft", "awaiting_approval", "approved"]),
                )
            )
            if existing:
                return existing

            plan = ProductionPlan(
                research_run_id=research_run_id,
                selected_niches_json=json.dumps(selected_niches),
                concepts_json=json.dumps(concepts),
                image_count=image_count,
                provider_routes_json=json.dumps(provider_routes),
                estimated_max_cost_cents=estimated_max_cost_cents,
                plan_hash=plan_hash,
                status="awaiting_approval",
                expires_at=expires_at,
                approved_by=None,
                approved_at=None,
                consumed_job_id=None,
                created_at=now,
            )
            session.add(plan)
            session.commit()
            session.refresh(plan)
            logger.info("Production plan #%s dibuat dengan hash %s", plan.id, plan_hash[:12])
            return plan

    def approve_plan(
        self,
        plan_id: int,
        approved_by_user_id: int,
        expected_plan_hash: str,
    ) -> ProductionPlan:
        """Approve an awaiting plan after verifying integrity and TTL."""
        now = utc_now()
        with self.session_factory() as session:
            plan = session.get(ProductionPlan, plan_id)
            if not plan:
                raise PlanStateError(f"Plan #{plan_id} tidak ditemukan")

            # 1. State transition check
            if plan.status != "awaiting_approval":
                raise PlanStateError(
                    f"Plan #{plan_id} tidak dapat disetujui karena status saat ini: '{plan.status}'"
                )

            # 2. Expiration check
            plan_exp = plan.expires_at
            if plan_exp.tzinfo is None:
                plan_exp = plan_exp.replace(tzinfo=timezone.utc)
            if now >= plan_exp:
                plan.status = "expired"
                session.commit()
                raise PlanStateError(f"Plan #{plan_id} telah kedaluwarsa pada {plan.expires_at}")

            # 3. Hash verification (anti-tampering check)
            if plan.plan_hash != expected_plan_hash:
                raise PlanIntegrityError(
                    f"Plan hash mismatch! Diharapkan: {expected_plan_hash}, Tercatat: {plan.plan_hash}"
                )

            plan.status = "approved"
            plan.approved_by = approved_by_user_id
            plan.approved_at = now
            session.commit()
            session.refresh(plan)
            logger.info("Plan #%s DISETUJUI oleh user #%s", plan.id, approved_by_user_id)
            return plan

    def reject_plan(self, plan_id: int, user_id: int) -> ProductionPlan:
        """Reject a production plan."""
        with self.session_factory() as session:
            plan = session.get(ProductionPlan, plan_id)
            if not plan:
                raise PlanStateError(f"Plan #{plan_id} tidak ditemukan")
            if plan.status in ("consumed", "revoked"):
                raise PlanStateError(f"Plan #{plan_id} tidak dapat ditolak karena berstatus '{plan.status}'")

            plan.status = "rejected"
            session.commit()
            session.refresh(plan)
            logger.info("Plan #%s DITOLAK oleh user #%s", plan.id, user_id)
            return plan

    def revoke_plan(self, plan_id: int, reason: str) -> ProductionPlan:
        """Revoke an approved plan before it is consumed."""
        with self.session_factory() as session:
            plan = session.get(ProductionPlan, plan_id)
            if not plan:
                raise PlanStateError(f"Plan #{plan_id} tidak ditemukan")
            if plan.status == "consumed":
                raise PlanStateError(f"Plan #{plan_id} sudah dikonsumsi menjadi job, tidak dapat direvokasi")

            plan.status = "revoked"
            session.commit()
            session.refresh(plan)
            logger.warning("Plan #%s DIREVOKASI: %s", plan.id, reason)
            return plan

