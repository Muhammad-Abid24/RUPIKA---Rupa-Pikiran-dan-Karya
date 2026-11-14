"""Periodic maintenance scheduler for backups, health checks, and cleanup (SF-013)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple
from sqlalchemy.orm import Session

from stockforge.config import get_settings
from stockforge.db.session import create_session_factory
from stockforge.ops.backup import check_sqlite_integrity, perform_sqlite_online_backup
from stockforge.ops.health import (
    CostHealthReport,
    DiskSpaceReport,
    QueueHealthReport,
    check_budget_health,
    check_disk_space,
    check_queue_health,
    cleanup_stale_files,
)
from stockforge.ops.outbox import OutboxDispatchReport, OutboxService
from stockforge.storage.service import StorageService

logger = logging.getLogger("stockforge.ops.scheduler")


@dataclass
class MaintenanceReport:
    """Consolidated outcome of an automated maintenance cycle."""
    timestamp: str
    backup_file: Optional[str]
    backup_integrity_ok: bool
    disk: DiskSpaceReport
    queue: QueueHealthReport
    budget: CostHealthReport
    stale_files_cleaned: int
    outbox_report: Optional[OutboxDispatchReport] = None


class MaintenanceScheduler:
    """Coordinates daily SQLite online backup, diagnostics, cleanup, and outbox delivery."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        storage_service: StorageService,
        db_path: Path,
        backup_dir: Path,
        retention_days: int = 30,
    ) -> None:
        self.session_factory = session_factory
        self.storage_service = storage_service
        self.db_path = db_path
        self.backup_dir = backup_dir
        self.retention_days = retention_days
        self.outbox_service = OutboxService(session_factory=session_factory)

    def run_daily_backup(self) -> Tuple[Optional[Path], bool]:
        """Execute scheduled online database backup and integrity verification."""
        try:
            backup_file = perform_sqlite_online_backup(
                db_path=self.db_path,
                backup_dir=self.backup_dir,
                retention_days=self.retention_days,
            )
            is_ok, _ = check_sqlite_integrity(backup_file)
            return backup_file, is_ok
        except Exception as e:
            logger.error("Gagal menjalankan scheduled backup: %s", e)
            return None, False

    def run_health_checks(self) -> Tuple[DiskSpaceReport, QueueHealthReport, CostHealthReport]:
        """Perform comprehensive disk, queue, and budget diagnostic checks."""
        settings = get_settings()
        daily_cents = int(settings.daily_budget_usd * 100)

        disk_rep = check_disk_space(self.storage_service.base_data_dir)
        queue_rep = check_queue_health(self.session_factory)
        cost_rep = check_budget_health(self.session_factory, daily_budget_cents=daily_cents)
        return disk_rep, queue_rep, cost_rep

    def run_cleanup(self, max_age_hours: int = 24) -> int:
        """Purge orphaned temporary files (*.tmp) across all storage directories."""
        return cleanup_stale_files(self.storage_service.base_data_dir, max_age_hours=max_age_hours)

    def run_maintenance_cycle(self, trigger_backup: bool = True) -> MaintenanceReport:
        """Execute a full maintenance cycle: health check -> backup -> cleanup."""
        now = datetime.now(timezone.utc)
        logger.info("Memulai maintenance cycle pada %s UTC...", now.isoformat())

        # 1. Health checks
        disk, queue, budget = self.run_health_checks()

        # 2. Daily online backup
        backup_file_path = None
        backup_ok = True
        if trigger_backup and self.db_path.exists():
            backup_file_path, backup_ok = self.run_daily_backup()

        # 3. Cleanup stale temporary files
        cleaned_count = self.run_cleanup()

        report = MaintenanceReport(
            timestamp=now.isoformat(),
            backup_file=str(backup_file_path) if backup_file_path else None,
            backup_integrity_ok=backup_ok,
            disk=disk,
            queue=queue,
            budget=budget,
            stale_files_cleaned=cleaned_count,
        )

        logger.info(
            "Maintenance cycle selesai: disk=%s, queue_pending=%d, budget_usage=%.1f%%, backup=%s",
            disk.status.value,
            queue.pending_count,
            budget.usage_percent,
            backup_file_path.name if backup_file_path else "None",
        )
        return report

