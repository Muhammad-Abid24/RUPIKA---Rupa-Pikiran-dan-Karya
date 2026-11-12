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
from stockforge.queue.service import QueueService

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
        """Process items belonging to a claimed job.
        
        item_processor: optional callback (job_id, item_index) for pluggable stage execution.
        Returns final status: 'completed', 'completed_with_errors', or 'cancelled'.
        """
        # Load items
        with Session(self.queue_service.engine) as session:
            job = session.get(Job, job_id)
            if not job:
                return "failed"
            item_indices = [item.item_index for item in job.items]

        # If no items explicitly created, fallback to item 1
        if not item_indices:
            item_indices = [1]

        any_failure = False
        for idx in item_indices:
            # Check cancellation before starting next item
            if self.check_is_cancelled(job_id):
                logger.info("Pekerjaan %s dibatalkan secara kooperatif oleh operator", job_id)
                return "cancelled"

            # Heartbeat to extend lease
            self.queue_service.heartbeat(job_id, self.worker_id, extend_seconds=self.lease_seconds)

            try:
                # Network / processing happens outside database transaction!
                if item_processor:
                    item_processor(job_id, idx)
                else:
                    # Default simulation: lightweight placeholder processing
                    time.sleep(0.01)
            except Exception as e:
                logger.error("Gagal memproses item %s pada job %s: %s", idx, job_id, e)
                any_failure = True

        return "completed_with_errors" if any_failure else "completed"

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
