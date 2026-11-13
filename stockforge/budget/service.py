"""Budget reservation, settlement, release, and cost tracking ledger (SF-008)."""
from __future__ import annotations

import logging
from datetime import datetime, time, timezone
from typing import Callable, Optional
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from stockforge.db.models import BudgetLedger, Job, utc_now

logger = logging.getLogger("stockforge.budget.service")


class BudgetExceededError(Exception):
    """Raised when an operation would exceed daily or monthly spending limits."""

    def __init__(
        self,
        period: str,
        limit_cents: int,
        current_committed_cents: int,
        requested_cents: int,
    ) -> None:
        self.period = period
        self.limit_cents = limit_cents
        self.current_committed_cents = current_committed_cents
        self.requested_cents = requested_cents
        super().__init__(
            f"Batas anggaran {period} terlampaui! Batas: {limit_cents} sen, "
            f"Terkadens: {current_committed_cents} sen, Diminta: {requested_cents} sen."
        )


class UnknownCostError(Exception):
    """Raised when an operation cost is unknown or undetermined (fail-closed policy)."""
    pass


class InvalidLedgerOperationError(Exception):
    """Raised when an invalid ledger state transition is requested."""
    pass


class BudgetService:
    """Manages transactional budget reservation, settlement, and release using integer cents.
    
    Hard Rules:
    1. Integer Cents Only: Never use floating point for authoritative balances.
    2. Unknown Cost Fail-Closed: Operations with unknown cost must fail closed.
    3. 3-Phase Budget Tracking:
       - Reserve: locks estimated funds prior to external billable calls.
       - Settle: commits actual incurred costs and clears the active reservation.
       - Release: releases unused reservation if job fails or cancels.
    4. Ambiguous Timeout Safety:
       - Uncertain billable timeouts move job to 'needs_reconciliation'.
       - Reserved funds are HELD until human/automated reconciliation confirms billing.
    """

    def __init__(
        self,
        session_factory: Callable[[], Session],
        daily_limit_cents: int = 300,    # $3.00 USD
        monthly_limit_cents: int = 5000, # $50.00 USD
    ) -> None:
        self.session_factory = session_factory
        self.daily_limit_cents = daily_limit_cents
        self.monthly_limit_cents = monthly_limit_cents

    def get_active_reservation_for_job(self, session: Session, job_id: int) -> int:
        """Calculate currently active reserved amount for a specific job in integer cents."""
        reserves = session.scalar(
            select(func.coalesce(func.sum(BudgetLedger.amount_cents), 0)).where(
                BudgetLedger.job_id == job_id,
                BudgetLedger.kind == "reserve",
            )
        ) or 0

        releases = session.scalar(
            select(func.coalesce(func.sum(BudgetLedger.amount_cents), 0)).where(
                BudgetLedger.job_id == job_id,
                BudgetLedger.kind == "release",
            )
        ) or 0

        active = reserves - releases
        return max(0, active)

    def get_total_active_reservations(self, session: Session) -> int:
        """Calculate active reservations across all pending/running jobs."""
        total_reserves = session.scalar(
            select(func.coalesce(func.sum(BudgetLedger.amount_cents), 0)).where(
                BudgetLedger.kind == "reserve",
            )
        ) or 0

        total_releases = session.scalar(
            select(func.coalesce(func.sum(BudgetLedger.amount_cents), 0)).where(
                BudgetLedger.kind == "release",
            )
        ) or 0

        return max(0, total_reserves - total_releases)

    def get_daily_committed_cents(self, session: Session, as_of: Optional[datetime] = None) -> int:
        """Calculate total committed budget for the current UTC day (settled today + active reservations)."""
        ref_time = as_of or utc_now()
        start_of_day = datetime.combine(ref_time.date(), time.min, tzinfo=timezone.utc)

        # Sum of settled spend today
        settled_today = session.scalar(
            select(func.coalesce(func.sum(BudgetLedger.amount_cents), 0)).where(
                BudgetLedger.kind == "settle",
                BudgetLedger.created_at >= start_of_day,
            )
        ) or 0

        # Plus active reservations
        active_reservations = self.get_total_active_reservations(session)
        return settled_today + active_reservations

    def get_monthly_committed_cents(self, session: Session, as_of: Optional[datetime] = None) -> int:
        """Calculate total committed budget for current UTC month (settled this month + active reservations)."""
        ref_time = as_of or utc_now()
        start_of_month = datetime(ref_time.year, ref_time.month, 1, 0, 0, 0, tzinfo=timezone.utc)

        settled_month = session.scalar(
            select(func.coalesce(func.sum(BudgetLedger.amount_cents), 0)).where(
                BudgetLedger.kind == "settle",
                BudgetLedger.created_at >= start_of_month,
            )
        ) or 0

        active_reservations = self.get_total_active_reservations(session)
        return settled_month + active_reservations

    def reserve_budget(
        self,
        job_id: int,
        estimated_cost_cents: Optional[int],
    ) -> BudgetLedger:
        """Reserve budget for a job. Fail-closed if cost is unknown, or if daily/monthly caps are exceeded."""
        if estimated_cost_cents is None or estimated_cost_cents <= 0:
            raise UnknownCostError(
                f"Fail-closed: Biaya estimasi untuk job #{job_id} tidak diketahui atau bernilai <= 0 "
                f"({estimated_cost_cents}). Panggilan berbayar diblokir."
            )

        with self.session_factory() as session:
            # Begin explicit immediate transaction for concurrency safety
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")

            # Check existing active reservation for this job
            existing_res = self.get_active_reservation_for_job(session, job_id)
            if existing_res > 0:
                raise InvalidLedgerOperationError(
                    f"Job #{job_id} sudah memiliki reservasi aktif sebesar {existing_res} sen."
                )

            # Check daily cap
            daily_committed = self.get_daily_committed_cents(session)
            if daily_committed + estimated_cost_cents > self.daily_limit_cents:
                raise BudgetExceededError(
                    period="harian (daily)",
                    limit_cents=self.daily_limit_cents,
                    current_committed_cents=daily_committed,
                    requested_cents=estimated_cost_cents,
                )

            # Check monthly cap
            monthly_committed = self.get_monthly_committed_cents(session)
            if monthly_committed + estimated_cost_cents > self.monthly_limit_cents:
                raise BudgetExceededError(
                    period="bulanan (monthly)",
                    limit_cents=self.monthly_limit_cents,
                    current_committed_cents=monthly_committed,
                    requested_cents=estimated_cost_cents,
                )

            entry = BudgetLedger(
                job_id=job_id,
                kind="reserve",
                amount_cents=estimated_cost_cents,
                created_at=utc_now(),
            )
            session.add(entry)

            # Sync Job reserved_usd_cents if job entity exists
            job = session.get(Job, job_id)
            if job:
                job.reserved_usd_cents = estimated_cost_cents

            session.commit()
            session.refresh(entry)
            logger.info("Dana berhasil direservasi untuk Job #%d: %d sen.", job_id, estimated_cost_cents)
            return entry

    def settle_budget(
        self,
        job_id: int,
        actual_cost_cents: int,
    ) -> BudgetLedger:
        """Settle actual cost, clearing the active reservation and committing the final expense."""
        if actual_cost_cents < 0:
            raise ValueError(f"Biaya aktual tidak boleh negatif: {actual_cost_cents}")

        with self.session_factory() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")

            reserved = self.get_active_reservation_for_job(session, job_id)
            if reserved <= 0:
                raise InvalidLedgerOperationError(
                    f"Job #{job_id} tidak memiliki reservasi aktif untuk diselesaikan (settle)."
                )

            now = utc_now()
            # 1. Release the full reservation hold
            release_entry = BudgetLedger(
                job_id=job_id,
                kind="release",
                amount_cents=reserved,
                created_at=now,
            )
            session.add(release_entry)

            # 2. Add actual settled cost
            settle_entry = BudgetLedger(
                job_id=job_id,
                kind="settle",
                amount_cents=actual_cost_cents,
                created_at=now,
            )
            session.add(settle_entry)

            # Update job record
            job = session.get(Job, job_id)
            if job:
                job.reserved_usd_cents = 0

            session.commit()
            session.refresh(settle_entry)
            logger.info("Biaya Job #%d berhasil disettle: %d sen (reservasi awal: %d sen).", job_id, actual_cost_cents, reserved)
            return settle_entry

    def release_budget(
        self,
        job_id: int,
    ) -> Optional[BudgetLedger]:
        """Release any active reservation without incurring settled costs (e.g. on job failure or cancellation)."""
        with self.session_factory() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")

            reserved = self.get_active_reservation_for_job(session, job_id)
            if reserved <= 0:
                logger.debug("Tidak ada reservasi aktif untuk dilepas pada Job #%d.", job_id)
                return None

            release_entry = BudgetLedger(
                job_id=job_id,
                kind="release",
                amount_cents=reserved,
                created_at=utc_now(),
            )
            session.add(release_entry)

            job = session.get(Job, job_id)
            if job:
                job.reserved_usd_cents = 0

            session.commit()
            session.refresh(release_entry)
            logger.info("Reservasi dana Job #%d berhasil dilepas (%d sen).", job_id, reserved)
            return release_entry

    def handle_ambiguous_timeout(
        self,
        job_id: int,
        error_context: str = "Ambiguous provider timeout",
    ) -> None:
        """Handle ambiguous billable call timeout.
        
        INVARIANT:
        Do NOT release reserved funds blindly! Move job to 'needs_reconciliation'.
        The reservation remains locked until manual or automated reconciliation occurs.
        """
        with self.session_factory() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")

            job = session.get(Job, job_id)
            if job:
                job.status = "needs_reconciliation"
                job.lease_owner = None
                job.lease_until = None
                logger.warning(
                    "Job #%d mengalami timeout ambigu ('%s'). Status dipindahkan ke 'needs_reconciliation'. "
                    "Dana reservasi TETAP DITAHAN untuk mencegah double spending.",
                    job_id,
                    error_context,
                )
            session.commit()

    def reconcile_job(
        self,
        job_id: int,
        was_billed: bool,
        actual_cost_cents: int = 0,
    ) -> None:
        """Resolve a job in 'needs_reconciliation' status after billing verification."""
        with self.session_factory() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")

            job = session.get(Job, job_id)
            if not job or job.status != "needs_reconciliation":
                raise InvalidLedgerOperationError(
                    f"Job #{job_id} bukan berstatus 'needs_reconciliation' (status saat ini: {getattr(job, 'status', None)})."
                )

            reserved = self.get_active_reservation_for_job(session, job_id)
            now = utc_now()

            if was_billed:
                # Settle actual cost
                if reserved > 0:
                    session.add(BudgetLedger(job_id=job_id, kind="release", amount_cents=reserved, created_at=now))
                session.add(BudgetLedger(job_id=job_id, kind="settle", amount_cents=actual_cost_cents, created_at=now))
                job.status = "completed"
                logger.info("Rekonsiliasi Job #%d: terbukti ditagih %d sen. Diselesaikan.", job_id, actual_cost_cents)
            else:
                # Release reserved funds
                if reserved > 0:
                    session.add(BudgetLedger(job_id=job_id, kind="release", amount_cents=reserved, created_at=now))
                job.status = "failed"
                logger.info("Rekonsiliasi Job #%d: terbukti tidak ditagih. Reservasi dana %d sen dilepaskan.", job_id, reserved)

            job.reserved_usd_cents = 0
            session.commit()
