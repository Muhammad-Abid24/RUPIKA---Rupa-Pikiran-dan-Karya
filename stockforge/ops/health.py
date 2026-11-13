"""System health diagnostics, disk space monitoring, and cleanup policies (SF-013)."""
from __future__ import annotations

import logging
import os
import shutil
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, List, Optional
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from stockforge.db.models import BudgetLedger, Job, utc_now

logger = logging.getLogger("stockforge.ops.health")


class DiskHealthStatus(str, Enum):
    OK = "ok"
    WARNING = "warning"
    CRITICAL = "critical"


class DiskFullError(Exception):
    """Raised when available disk capacity is in critical threshold to prevent generation failure."""
    pass


@dataclass
class DiskSpaceReport:
    """Detailed disk capacity report."""
    total_bytes: int
    used_bytes: int
    free_bytes: int
    free_percent: float
    status: DiskHealthStatus

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_bytes": self.total_bytes,
            "used_bytes": self.used_bytes,
            "free_bytes": self.free_bytes,
            "free_percent": round(self.free_percent, 2),
            "status": self.status.value,
        }


def check_disk_space(
    path: Path,
    warning_percent: float = 20.0,
    critical_percent: float = 10.0,
    critical_bytes: int = 500 * 1024 * 1024,  # 500 MB absolute floor
) -> DiskSpaceReport:
    """Evaluate free disk space and classify health status into OK, WARNING, or CRITICAL.
    
    Guarantees:
    - Free percent < 10% or free bytes < 500 MB -> CRITICAL.
    - Free percent < 20% -> WARNING.
    - Otherwise -> OK.
    """
    target = path.resolve()
    # If path does not exist yet, check its parent
    while not target.exists() and target != target.parent:
        target = target.parent

    usage = shutil.disk_usage(target)
    free_pct = (usage.free / usage.total) * 100.0 if usage.total > 0 else 0.0

    if free_pct < critical_percent or usage.free < critical_bytes:
        status = DiskHealthStatus.CRITICAL
    elif free_pct < warning_percent:
        status = DiskHealthStatus.WARNING
    else:
        status = DiskHealthStatus.OK

    return DiskSpaceReport(
        total_bytes=usage.total,
        used_bytes=usage.used,
        free_bytes=usage.free,
        free_percent=free_pct,
        status=status,
    )


def assert_disk_healthy_for_generation(
    path: Path,
    critical_percent: float = 10.0,
    critical_bytes: int = 500 * 1024 * 1024,
) -> None:
    """Pre-flight safety assertion: halt image generation immediately if disk space is critical.
    
    Raises:
        DiskFullError if status is CRITICAL.
    """
    report = check_disk_space(
        path,
        critical_percent=critical_percent,
        critical_bytes=critical_bytes,
    )
    if report.status == DiskHealthStatus.CRITICAL:
        msg = (
            f"Kapasitas penyimpanan kritis (< {critical_percent}% atau sisa {report.free_bytes // (1024 * 1024)} MB). "
            "Generasi gambar dihentikan secara otomatis untuk mencegah korupsi sistem!"
        )
        logger.critical(msg)
        raise DiskFullError(msg)
    elif report.status == DiskHealthStatus.WARNING:
        logger.warning(
            "Peringatan kapasitas disk rendah: sisa %.1f%% (%d MB).",
            report.free_percent,
            report.free_bytes // (1024 * 1024),
        )


@dataclass
class QueueHealthReport:
    """Summary of queue processing status and latency."""
    pending_count: int
    running_count: int
    failed_count: int
    reconciliation_count: int
    has_stalled_jobs: bool
    stalled_job_ids: List[int]


def check_queue_health(session_factory: Callable[[], Session]) -> QueueHealthReport:
    """Inspect job queue health, lease timeouts, and stalled execution."""
    with session_factory() as session:
        # Group counts by status
        stmt = select(Job.status, func.count(Job.id)).group_by(Job.status)
        counts = dict(session.execute(stmt).all())

        pending = counts.get("pending", 0)
        running = counts.get("running", 0)
        failed = counts.get("failed", 0)
        reconciliation = counts.get("needs_reconciliation", 0)

        # Check for stalled running jobs (lease expired)
        now = utc_now()
        stalled_stmt = select(Job.id).where(
            Job.status == "running",
            Job.lease_until != None,
            Job.lease_until < now,
        )
        stalled_ids = list(session.scalars(stalled_stmt).all())


        return QueueHealthReport(
            pending_count=pending,
            running_count=running,
            failed_count=failed,
            reconciliation_count=reconciliation,
            has_stalled_jobs=len(stalled_ids) > 0,
            stalled_job_ids=stalled_ids,
        )


@dataclass
class CostHealthReport:
    """Daily budget consumption analysis."""
    committed_cents: int
    daily_budget_cents: int
    usage_percent: float
    alert_triggered: bool


def check_cost_health(
    session_factory: Callable[[], Session],
    daily_budget_cents: int = 300,  # $3.00 USD
    alert_threshold_percent: float = 80.0,
) -> CostHealthReport:
    """Evaluate current day budget consumption and trigger warning if >= threshold."""
    with session_factory() as session:
        now = utc_now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        stmt = select(func.coalesce(func.sum(BudgetLedger.amount_cents), 0)).where(
            BudgetLedger.kind.in_(["settlement", "reservation"]),
            BudgetLedger.created_at >= today_start,
        )
        committed = int(session.scalar(stmt) or 0)

        pct = (committed / daily_budget_cents) * 100.0 if daily_budget_cents > 0 else 0.0
        alert = pct >= alert_threshold_percent

        return CostHealthReport(
            committed_cents=committed,
            daily_budget_cents=daily_budget_cents,
            usage_percent=pct,
            alert_triggered=alert,
        )


check_budget_health = check_cost_health



def cleanup_stale_files(
    base_data_dir: Path,
    max_age_hours: int = 24,
) -> int:
    """Remove orphaned temporary files (*.tmp) older than max_age_hours.
    
    Returns:
        Number of cleaned files.
    """
    if not base_data_dir.exists():
        return 0

    cutoff_seconds = time.time() - (max_age_hours * 3600)
    cleaned_count = 0

    for root, _, files in os.walk(base_data_dir):
        for f in files:
            if f.endswith(".tmp") or f.startswith(".tmp"):
                file_path = Path(root) / f
                try:
                    if file_path.stat().st_mtime < cutoff_seconds:
                        file_path.unlink(missing_ok=True)
                        cleaned_count += 1
                        logger.info("Membersihkan file temporary usang: %s", file_path)
                except Exception as e:
                    logger.warning("Gagal membersihkan file %s: %s", file_path, e)

    return cleaned_count
