"""Acceptance tests for SF-013 Scheduler, backup, health and recovery."""
from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from stockforge.db.models import Asset, Base, BudgetLedger, Job, TelegramOutbox, utc_now
from stockforge.images.service import ImageGenerationService
from stockforge.ops import (
    BackupIntegrityError,
    DiskFullError,
    DiskHealthStatus,
    MaintenanceScheduler,
    OutboxService,
    assert_disk_healthy_for_generation,
    check_budget_health,
    check_disk_space,
    check_queue_health,
    check_sqlite_integrity,
    cleanup_stale_files,
    perform_sqlite_online_backup,
    restore_sqlite_backup,
)
from stockforge.providers.base import ProviderKind, RoleType
from stockforge.providers.mock import MockImageProvider
from stockforge.providers.registry import ProviderRegistry
from stockforge.providers.router import ProviderRouter
from stockforge.storage.service import StorageService


@pytest.fixture
def ops_env(tmp_path: Path):
    """Setup isolated SQLite DB, storage, and backup directory."""
    db_path = tmp_path / "test_ops.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    data_dir = tmp_path / "data"
    raw_dir = data_dir / "raw"
    backups_dir = data_dir / "backups"
    raw_dir.mkdir(parents=True, exist_ok=True)
    backups_dir.mkdir(parents=True, exist_ok=True)

    storage_service = StorageService(base_data_dir=data_dir)
    return session_factory, storage_service, db_path, backups_dir, data_dir


# ---------------------------------------------------------------------------
# Test 1: SQLite online hot backup and restore drill
# ---------------------------------------------------------------------------
def test_sqlite_online_backup_and_restore(ops_env):
    session_factory, storage_service, db_path, backups_dir, _ = ops_env

    # 1. Populate database with some records
    with session_factory() as session:
        job = Job(id=1, type="image_batch", status="completed", operation_id="op_test_1")
        session.add(job)
        session.commit()


    # 2. Perform live online backup
    backup_file = perform_sqlite_online_backup(db_path=db_path, backup_dir=backups_dir)
    assert backup_file.is_file()
    assert backup_file.suffix == ".db"

    # Verify sha256 checksum file was generated
    checksum_file = backup_file.with_suffix(".db.sha256")
    assert checksum_file.is_file()

    # 3. Corrupt/wipe original database to simulate catastrophe
    with open(db_path, "wb") as f:
        f.write(b"CORRUPTED_GARBAGE_DATA_1234567890")

    is_ok, _ = check_sqlite_integrity(db_path)
    assert is_ok is False

    # 4. Restore database from backup
    success = restore_sqlite_backup(backup_file=backup_file, target_db_path=db_path)
    assert success is True

    # Dispose connection pool so fresh connection is established to restored file
    session_factory.kw["bind"].dispose()

    # 5. Verify restored database integrity and record presence

    is_restored_ok, msg = check_sqlite_integrity(db_path)
    assert is_restored_ok is True
    assert msg == "ok"

    with session_factory() as session:
        restored_job = session.get(Job, 1)
        assert restored_job is not None
        assert restored_job.status == "completed"


# ---------------------------------------------------------------------------
# Test 2: Restore rejects corrupted backup snapshot
# ---------------------------------------------------------------------------
def test_restore_rejects_corrupted_backup(ops_env):
    _, _, db_path, backups_dir, _ = ops_env

    corrupted_backup = backups_dir / "corrupted_backup.db"
    corrupted_backup.write_bytes(b"NON_SQLITE_TRASH_DATA")

    with pytest.raises(BackupIntegrityError):
        restore_sqlite_backup(backup_file=corrupted_backup, target_db_path=db_path)


# ---------------------------------------------------------------------------
# Test 3: Disk space monitoring & warning/critical threshold classification
# ---------------------------------------------------------------------------
def test_disk_space_classification(ops_env):
    _, _, _, _, data_dir = ops_env

    # Simulate 50% free -> OK
    with patch("shutil.disk_usage", return_value=shutil._ntuple_diskusage(1000, 500, 500)):
        rep = check_disk_space(data_dir, warning_percent=20.0, critical_percent=10.0, critical_bytes=100)
        assert rep.status == DiskHealthStatus.OK
        assert rep.free_percent == 50.0

    # Simulate 15% free -> WARNING
    with patch("shutil.disk_usage", return_value=shutil._ntuple_diskusage(1000, 850, 150)):
        rep_warn = check_disk_space(data_dir, warning_percent=20.0, critical_percent=10.0, critical_bytes=100)
        assert rep_warn.status == DiskHealthStatus.WARNING
        assert rep_warn.free_percent == 15.0

    # Simulate 5% free -> CRITICAL
    with patch("shutil.disk_usage", return_value=shutil._ntuple_diskusage(1000, 950, 50)):
        rep_crit = check_disk_space(data_dir, warning_percent=20.0, critical_percent=10.0, critical_bytes=100)
        assert rep_crit.status == DiskHealthStatus.CRITICAL
        assert rep_crit.free_percent == 5.0


# ---------------------------------------------------------------------------
# Test 4: Disk-full simulation halts new generation (CRITICAL ACCEPTANCE CRITERION)
# ---------------------------------------------------------------------------
def test_disk_full_halts_new_generation(ops_env):
    session_factory, storage_service, _, _, data_dir = ops_env

    mock_provider = MockImageProvider(provider_name="test-image")
    image_service = ImageGenerationService(
        session_factory=session_factory,
        storage_service=storage_service,
        image_provider=mock_provider,
        license_evidence="COMMERCIAL_LICENSE_VERIFIED",
    )


    # Simulate CRITICAL disk space (< 10% free)
    with patch("shutil.disk_usage", return_value=shutil._ntuple_diskusage(1000, 950, 50)):
        with pytest.raises(DiskFullError) as exc_info:
            asyncio.run(image_service.generate_and_store_image("A futuristic city at night"))
        assert "Kapasitas penyimpanan kritis" in str(exc_info.value)

    # Verify zero calls to image provider and zero assets inserted
    assert len(mock_provider.calls) == 0
    with session_factory() as session:
        assets = session.query(Asset).all()
        assert len(assets) == 0


# ---------------------------------------------------------------------------
# Test 5: Cleanup of stale temporary files (*.tmp)
# ---------------------------------------------------------------------------
def test_cleanup_stale_files(ops_env):
    _, _, _, _, data_dir = ops_env

    raw_dir = data_dir / "raw"
    fresh_tmp = raw_dir / ".active_image.tmp"
    fresh_tmp.write_text("fresh temp data")

    stale_tmp = raw_dir / ".orphaned_image.tmp"
    stale_tmp.write_text("stale temp data")

    # Set modification time of stale_tmp to 30 hours ago
    past_time = time.time() - (30 * 3600)
    os.utime(stale_tmp, (past_time, past_time))

    cleaned = cleanup_stale_files(base_data_dir=data_dir, max_age_hours=24)
    assert cleaned == 1
    assert not stale_tmp.exists()
    assert fresh_tmp.exists()


# ---------------------------------------------------------------------------
# Test 6: Outbox idempotency and anti-storm throttling
# ---------------------------------------------------------------------------
def test_outbox_anti_storm_throttling(ops_env):
    session_factory, _, _, _, _ = ops_env
    outbox_service = OutboxService(
        session_factory=session_factory,
        suppression_window_seconds=10,  # 10s window for test
        max_attempts=3,
    )

    # 1. Enqueue event 1
    rec1, created1 = outbox_service.enqueue_event(
        chat_id=1001,
        event_key="alert:disk_warn:run1",
        payload={"category": "disk_alert", "text": "Disk capacity warning!"},
    )
    assert created1 is True

    # 2. Duplicate enqueue with same key returns existing record (idempotent)
    rec2, created2 = outbox_service.enqueue_event(
        chat_id=1001,
        event_key="alert:disk_warn:run1",
        payload={"category": "disk_alert", "text": "Disk capacity warning!"},
    )
    assert created2 is False
    assert rec1.id == rec2.id

    # 3. Enqueue another event in the SAME category (rapid burst)
    rec3, created3 = outbox_service.enqueue_event(
        chat_id=1001,
        event_key="alert:disk_warn:run2",
        payload={"category": "disk_alert", "text": "Disk capacity warning burst!"},
    )
    assert created3 is True

    mock_send = AsyncMock()

    # 4. First process run: rec1 sent, rec3 suppressed by anti-storm throttling!
    report = asyncio.run(outbox_service.process_outbox(send_message_fn=mock_send, max_batch=10))
    assert report.sent_count == 1
    assert report.suppressed_count == 1
    assert mock_send.call_count == 1


# ---------------------------------------------------------------------------
# Test 7: Consolidated maintenance cycle execution
# ---------------------------------------------------------------------------
def test_maintenance_cycle_execution(ops_env):
    session_factory, storage_service, db_path, backups_dir, _ = ops_env

    scheduler = MaintenanceScheduler(
        session_factory=session_factory,
        storage_service=storage_service,
        db_path=db_path,
        backup_dir=backups_dir,
    )

    report = scheduler.run_maintenance_cycle(trigger_backup=True)
    assert report.backup_file is not None
    assert report.backup_integrity_ok is True
    assert report.disk.status in (DiskHealthStatus.OK, DiskHealthStatus.WARNING)
    assert report.queue.pending_count == 0
    assert report.budget.committed_cents == 0
