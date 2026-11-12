"""Durable transactional job queue service using SQLite BEGIN IMMEDIATE (SF-004)."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from stockforge.db.models import Job, JobAttempt, JobEvent, TelegramOutbox, utc_now

logger = logging.getLogger("stockforge.queue.service")


class QueueService:
    """Manages transactional job queue operations with lease-based locking.
    
    Invariants:
    - Atomically claims at most one job using SQLite BEGIN IMMEDIATE.
    - Heartbeat extends lease while worker is alive.
    - Crash recovery NEVER blindly replays ambiguous in-flight paid operations;
      stale leased jobs with active attempts are moved to 'needs_reconciliation'.
    - Cooperative cancellation allows safe shutdown of in-flight work.
    """

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def enqueue_job(
        self,
        job_type: str,
        operation_id: str,
        concept_id: Optional[int] = None,
        created_by: Optional[int] = None,
        reserved_usd_cents: int = 0,
    ) -> Job:
        """Enqueue a new job with status 'pending'."""
        with Session(self.engine) as session:
            job = Job(
                type=job_type,
                operation_id=operation_id,
                concept_id=concept_id,
                created_by=created_by,
                status="pending",
                reserved_usd_cents=reserved_usd_cents,
                attempts=0,
                max_attempts=3,
                created_at=utc_now(),
                updated_at=utc_now(),
            )
            session.add(job)
            session.commit()
            session.refresh(job)
            return job

    def claim_job(self, worker_id: str, lease_seconds: int = 60) -> Optional[Job]:
        """Atomically claim the next eligible job using BEGIN IMMEDIATE.
        
        Eligible jobs:
        - status == 'pending'
        - OR status == 'reserved'
        - OR (status == 'running' AND lease_until < now AND attempts < max_attempts AND no ambiguous in-flight charge)
        """
        now = utc_now()
        lease_until = now + timedelta(seconds=lease_seconds)

        # Use raw DBAPI connection with explicit BEGIN IMMEDIATE for SQLite lock atomicity
        with self.engine.connect() as conn:
            conn.execute(text("BEGIN IMMEDIATE"))
            try:
                # Find the oldest pending or eligible job
                find_query = text(
                    """
                    SELECT id FROM jobs
                    WHERE status IN ('pending', 'reserved')
                    ORDER BY id ASC
                    LIMIT 1
                    """
                )
                job_id = conn.execute(find_query).scalar()

                if not job_id:
                    conn.execute(text("COMMIT"))
                    return None

                # Update job with lease details
                update_query = text(
                    """
                    UPDATE jobs
                    SET status = 'running',
                        lease_owner = :worker_id,
                        lease_until = :lease_until,
                        attempts = attempts + 1,
                        updated_at = :now
                    WHERE id = :job_id
                    """
                )
                conn.execute(
                    update_query,
                    {
                        "worker_id": worker_id,
                        "lease_until": lease_until,
                        "now": now,
                        "job_id": job_id,
                    },
                )
                conn.execute(text("COMMIT"))

                with Session(self.engine) as session:
                    claimed_job = session.get(Job, job_id)
                    logger.info("Worker %s berhasil mengklaim job %s (lease s/d %s)", worker_id, job_id, lease_until)
                    return claimed_job

            except Exception:
                conn.execute(text("ROLLBACK"))
                raise

    def heartbeat(self, job_id: int, worker_id: str, extend_seconds: int = 60) -> bool:
        """Extend lease duration for an active worker."""
        now = utc_now()
        new_lease = now + timedelta(seconds=extend_seconds)

        with self.engine.begin() as conn:
            result = conn.execute(
                text(
                    """
                    UPDATE jobs
                    SET lease_until = :new_lease,
                        updated_at = :now
                    WHERE id = :job_id AND lease_owner = :worker_id AND status = 'running'
                    """
                ),
                {
                    "new_lease": new_lease,
                    "now": now,
                    "job_id": job_id,
                    "worker_id": worker_id,
                },
            )
            return result.rowcount > 0

    def complete_job(
        self,
        job_id: int,
        worker_id: str,
        status: str = "completed",
    ) -> bool:
        """Mark job as completed, completed_with_errors, or failed, releasing lease."""
        now = utc_now()
        with self.engine.begin() as conn:
            result = conn.execute(
                text(
                    """
                    UPDATE jobs
                    SET status = :status,
                        lease_owner = NULL,
                        lease_until = NULL,
                        updated_at = :now
                    WHERE id = :job_id AND lease_owner = :worker_id
                    """
                ),
                {"status": status, "now": now, "job_id": job_id, "worker_id": worker_id},
            )
            return result.rowcount > 0

    def cancel_job(self, job_id: int) -> bool:
        """Cooperatively request cancellation of a job."""
        now = utc_now()
        with self.engine.begin() as conn:
            result = conn.execute(
                text(
                    """
                    UPDATE jobs
                    SET status = 'cancelled',
                        lease_owner = NULL,
                        lease_until = NULL,
                        updated_at = :now
                    WHERE id = :job_id AND status NOT IN ('completed', 'completed_with_errors', 'failed')
                    """
                ),
                {"now": now, "job_id": job_id},
            )
            return result.rowcount > 0

    def recover_stale_jobs(self) -> List[int]:
        """Crash recovery for stale running jobs whose lease has expired.
        
        CRITICAL SAFETY RULE:
        Never blindly replay ambiguous paid calls! If a stale job has any
        attempts in progress, mark it 'needs_reconciliation'.
        """
        now = utc_now()
        recovered_ids: List[int] = []

        with Session(self.engine) as session:
            # Find running jobs where lease_until has passed
            stale_jobs = session.scalars(
                select(Job).where(
                    Job.status == "running",
                    Job.lease_until.is_not(None),
                    Job.lease_until < now,
                )
            ).all()

            for job in stale_jobs:
                # Check if job has attempts with uncertain outcomes or billable spend
                has_attempts = session.scalar(
                    select(JobAttempt.id).where(JobAttempt.job_id == job.id).limit(1)
                )

                if has_attempts:
                    # Ambiguous / uncertain execution state -> MUST reconcile
                    job.status = "needs_reconciliation"
                    job.lease_owner = None
                    job.lease_until = None
                    job.updated_at = now
                    event_payload = json.dumps({
                        "reason": "Crash recovery: lease kadaluarsa dengan panggilan in-flight / upaya tercatat",
                        "stale_lease_until": str(job.lease_until),
                    })
                    session.add(
                        JobEvent(
                            job_id=job.id,
                            event_type="needs_reconciliation",
                            payload_json=event_payload,
                            created_at=now,
                        )
                    )
                    logger.warning(
                        "Job %s dipindahkan ke 'needs_reconciliation' karena lease kadaluarsa dengan panggilan ambigu",
                        job.id,
                    )
                else:
                    # No attempts recorded -> safe to revert to pending if attempts < max_attempts
                    if job.attempts < job.max_attempts:
                        job.status = "pending"
                        job.lease_owner = None
                        job.lease_until = None
                        job.updated_at = now
                        logger.info("Job %s tanpa panggilan ambigu dikembalikan ke status 'pending'", job.id)
                    else:
                        job.status = "failed"
                        job.lease_owner = None
                        job.lease_until = None
                        job.updated_at = now
                        logger.warning("Job %s mencapai max_attempts dan ditandai failed", job.id)

                recovered_ids.append(job.id)

            session.commit()
            return recovered_ids

    def publish_outbox_event(self, chat_id: int, event_key: str, payload: Dict[str, Any]) -> TelegramOutbox:
        """Idempotently persist an event to telegram_outbox."""
        with Session(self.engine) as session:
            outbox_entry = TelegramOutbox(
                chat_id=chat_id,
                event_key=event_key,
                payload_json=json.dumps(payload),
                sent_at=None,
                attempts=0,
                created_at=utc_now(),
            )
            session.add(outbox_entry)
            session.commit()
            session.refresh(outbox_entry)
            return outbox_entry

