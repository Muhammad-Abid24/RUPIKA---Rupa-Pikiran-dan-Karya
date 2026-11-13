"""Worker process claiming jobs and processing pipeline items safely (SF-004)."""
from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Callable, Optional
from sqlalchemy import select
from sqlalchemy.orm import Session

from stockforge.db.models import Job, JobItem, utc_now
from stockforge.providers.resilience import AmbiguousTimeoutError
from stockforge.queue.service import QueueService
from stockforge.queue.tracker import ItemProgressTracker

logger = logging.getLogger("stockforge.queue.worker")


class Worker:
    """Sequential job worker executing pipeline stages outside long DB transactions.
    
    Invariants:
    - Never holds a database lock during network API calls.
    - Performs periodic lease heartbeats.
    - Honors cooperative cancellation: stops when job status is 'cancelled'.
    - Separate from Telegram bot process.
    """

    def __init__(
        self,
        queue_service: QueueService,
        worker_id: Optional[str] = None,
        poll_interval_seconds: float = 1.0,
        lease_seconds: int = 60,
    ) -> None:
        self.queue_service = queue_service
        self.worker_id = worker_id or f"worker-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.poll_interval = poll_interval_seconds
        self.lease_seconds = lease_seconds
        self.is_running = False
        self.current_job_id: Optional[int] = None

    def check_is_cancelled(self, job_id: int) -> bool:
        """Check if job has received cooperative cancellation."""
        with Session(self.queue_service.engine) as session:
            status = session.scalar(select(Job.status).where(Job.id == job_id))
            return status == "cancelled"

    def execute_job_items(
        self,
        job_id: int,
        item_processor: Optional[Callable[[int, int], None]] = None,
    ) -> str:
        """Process items belonging to a claimed job with durable stage tracking.
        
        Guarantees:
        - Accurately tracks per-item stages before and after external calls.
        - Preserves progress across crashes; never reruns completed images.
        - Returns final status: 'completed', 'completed_with_errors', or 'cancelled'.
        """
        tracker = ItemProgressTracker(lambda: Session(self.queue_service.engine))

        # Check existing items or prepare resumption plan
        with Session(self.queue_service.engine) as session:
            job = session.get(Job, job_id)
            if not job:
                return "failed"
            has_items = len(job.items) > 0

        # If job had no items created yet, initialize item 1
        if not has_items:
            tracker.initialize_job_items(job_id, total_items=1, initial_stage="generating")

        # Plan which items need execution (excluding completed ones!)
        indices_to_run = tracker.prepare_resumption_plan(job_id)

        any_failure = False
        for idx in indices_to_run:
            # Check cancellation before starting next item
            if self.check_is_cancelled(job_id):
                logger.info("Pekerjaan %s dibatalkan secara kooperatif oleh operator", job_id)
                tracker.update_item_stage(job_id, idx, stage="generating", status="cancelled")
                return "cancelled"

            # Heartbeat to extend lease
            self.queue_service.heartbeat(job_id, self.worker_id, extend_seconds=self.lease_seconds)

            # Persist stage update BEFORE external execution
            tracker.update_item_stage(job_id, idx, stage="generating", status="processing")

            try:
                # Network / processing happens outside database transaction!
                if item_processor:
                    item_processor(job_id, idx)
                else:
                    # Default simulation: lightweight placeholder processing
                    time.sleep(0.01)

                # Persist stage update AFTER successful execution
                tracker.update_item_stage(job_id, idx, stage="generating", status="completed")
            except AmbiguousTimeoutError as ate:
                logger.warning("Item %s pada job %s mengalami ambiguous timeout: %s", idx, job_id, ate)
                tracker.update_item_stage(
                    job_id,
                    idx,
                    stage="generating",
                    status="needs_reconciliation",
                    last_error=str(ate),
                )
            except Exception as e:
                logger.error("Gagal memproses item %s pada job %s: %s", idx, job_id, e)
                tracker.update_item_stage(
                    job_id,
                    idx,
                    stage="generating",
                    status="failed",
                    last_error=str(e),
                )

        progress = tracker.get_batch_progress(job_id)
        if progress.reconciliation_count > 0:
            return "needs_reconciliation"
        if progress.failed_count > 0:
            return "completed_with_errors"
        return "completed"


    def run_once(
        self,
        item_processor: Optional[Callable[[int, int], None]] = None,
    ) -> Optional[int]:
        """Attempt to claim and process exactly one job.
        
        Returns job_id if processed, None if queue was empty.
        """
        job = self.queue_service.claim_job(self.worker_id, lease_seconds=self.lease_seconds)
        if not job:
            return None

        job_id = job.id
        self.current_job_id = job_id
        logger.info("Worker %s memulai pemrosesan job %s (%s)", self.worker_id, job_id, job.type)

        final_status = "failed"
        try:
            final_status = self.execute_job_items(job_id, item_processor=item_processor)
        except Exception as e:
            logger.error("Ekspepsi fatal saat eksekusi job %s: %s", job_id, e, exc_info=True)
            final_status = "failed"
        finally:
            if final_status != "cancelled":
                self.queue_service.complete_job(job_id, self.worker_id, status=final_status)
            self.current_job_id = None

        logger.info("Worker %s menyelesaikan job %s dengan status: %s", self.worker_id, job_id, final_status)
        return job_id

    def run(self) -> None:
        """Start worker loop polling the queue until stopped."""
        self.is_running = True
        logger.info("Worker %s aktif dan memantau antrean...", self.worker_id)
        while self.is_running:
            try:
                claimed = self.run_once()
                if not claimed:
                    time.sleep(self.poll_interval)
            except Exception as e:
                logger.error("Galat pada loop worker %s: %s", self.worker_id, e)
                time.sleep(self.poll_interval)

    def stop(self) -> None:
        """Signal worker to stop after current job finishes."""
        self.is_running = False
        logger.info("Worker %s menerima sinyal penghentian", self.worker_id)

