"""SQLite online backup and restore operations (SF-013)."""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger("stockforge.ops.backup")


class BackupIntegrityError(Exception):
    """Raised when a backup or database fails integrity verification."""
    pass


def check_sqlite_integrity(db_path: Path) -> Tuple[bool, str]:
    """Verify database file integrity using PRAGMA integrity_check."""
    if not db_path.exists():
        return False, f"File database tidak ditemukan: {db_path}"

    try:
        conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
        try:
            cursor = conn.cursor()
            cursor.execute("PRAGMA integrity_check;")
            rows = cursor.fetchall()
            if rows and rows[0][0] == "ok":
                return True, "ok"
            err_msg = "; ".join(r[0] for r in rows)
            return False, f"Integritas gagal: {err_msg}"
        finally:
            conn.close()
    except Exception as e:
        return False, f"Galat pemeriksaan integritas: {e}"


def perform_sqlite_online_backup(
    db_path: Path,
    backup_dir: Path,
    retention_days: int = 30,
    timestamp: Optional[datetime] = None,
) -> Path:
    """Execute live online hot backup of SQLite database using SQLite Backup API.
    
    Guarantees:
    - Non-blocking online snapshot even during WAL write activity.
    - Generates SHA-256 checksum file alongside the backup (.sha256).
    - Verifies PRAGMA integrity_check before finalizing the backup.
    - Cleans up backups older than retention_days.
    """
    if not db_path.exists():
        raise FileNotFoundError(f"Database sumber tidak ditemukan: {db_path}")

    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = timestamp or datetime.now(timezone.utc)
    ts_str = ts.strftime("%Y%m%d_%H%M%S")
    backup_file = backup_dir / f"stockforge_backup_{ts_str}.db"
    temp_backup_file = backup_dir / f".stockforge_backup_{ts_str}.tmp"
    checksum_file = backup_dir / f"stockforge_backup_{ts_str}.db.sha256"

    logger.info("Memulai online backup dari %s ke %s...", db_path, backup_file)

    try:
        # 1. Connect and perform online backup
        src_conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
        dst_conn = sqlite3.connect(str(temp_backup_file))
        try:
            with dst_conn:
                src_conn.backup(dst_conn, pages=100)
        finally:
            dst_conn.close()
            src_conn.close()

        # 2. Check integrity of the backup file
        is_ok, msg = check_sqlite_integrity(temp_backup_file)
        if not is_ok:
            raise BackupIntegrityError(f"Backup snapshot gagal uji integritas: {msg}")

        # 3. Compute SHA-256 and atomically rename
        data = temp_backup_file.read_bytes()
        sha256_hash = hashlib.sha256(data).hexdigest()
        checksum_file.write_text(f"{sha256_hash}  {backup_file.name}\n", encoding="utf-8")

        os.replace(temp_backup_file, backup_file)
        logger.info("Online backup berhasil: %s (SHA-256: %s)", backup_file.name, sha256_hash)
    except Exception as e:
        if temp_backup_file.exists():
            try:
                temp_backup_file.unlink()
            except OSError:
                pass
        logger.error("Gagal melakukan online backup: %s", e)
        raise

    # 4. Enforce retention policy
    if retention_days > 0:
        cutoff = ts - timedelta(days=retention_days)
        for old_file in backup_dir.glob("stockforge_backup_*.db"):
            try:
                mtime = datetime.fromtimestamp(old_file.stat().st_mtime, tz=timezone.utc)
                if mtime < cutoff:
                    old_file.unlink(missing_ok=True)
                    old_sha = old_file.with_suffix(".db.sha256")
                    old_sha.unlink(missing_ok=True)
                    logger.info("Menghapus backup lama di luar batas retensi: %s", old_file.name)
            except Exception as e:
                logger.warning("Gagal menghapus backup lama %s: %s", old_file, e)

    return backup_file


def restore_sqlite_backup(
    backup_file: Path,
    target_db_path: Path,
) -> bool:
    """Safely restore database from an online backup snapshot with checksum and integrity checks.
    
    Guarantees:
    - Verifies checksum if .sha256 file exists.
    - Verifies PRAGMA integrity_check on the backup file before restoring.
    - Atomically replaces target database.
    - Cleans up stale WAL (-wal) and shared memory (-shm) files.
    - Verifies PRAGMA integrity_check on the restored target database.
    """
    if not backup_file.is_file():
        raise FileNotFoundError(f"File backup tidak ditemukan: {backup_file}")

    # 1. Verify SHA-256 checksum if available
    checksum_file = backup_file.with_suffix(".db.sha256")
    if not checksum_file.exists():
        checksum_file = backup_file.parent / f"{backup_file.name}.sha256"

    if checksum_file.exists():
        expected_line = checksum_file.read_text(encoding="utf-8").strip()
        expected_sha = expected_line.split()[0]
        actual_sha = hashlib.sha256(backup_file.read_bytes()).hexdigest()
        if expected_sha.lower() != actual_sha.lower():
            raise BackupIntegrityError(
                f"Checksum backup tidak cocok! Diharapkan: {expected_sha}, Aktual: {actual_sha}"
            )

    # 2. Check backup file integrity
    is_ok, msg = check_sqlite_integrity(backup_file)
    if not is_ok:
        raise BackupIntegrityError(f"File backup korup atau tidak valid: {msg}")

    # 3. Clean up stale WAL / SHM files around target DB
    wal_file = target_db_path.parent / f"{target_db_path.name}-wal"
    shm_file = target_db_path.parent / f"{target_db_path.name}-shm"
    wal_file.unlink(missing_ok=True)
    shm_file.unlink(missing_ok=True)

    # 4. Atomic restore
    temp_target = target_db_path.parent / f".{target_db_path.name}.restore.tmp"
    try:
        shutil.copy2(backup_file, temp_target)
        os.replace(temp_target, target_db_path)
    finally:
        if temp_target.exists():
            temp_target.unlink(missing_ok=True)

    # 5. Final integrity check on restored database
    is_restored_ok, restored_msg = check_sqlite_integrity(target_db_path)
    if not is_restored_ok:
        raise BackupIntegrityError(f"Database hasil pemulihan gagal uji integritas: {restored_msg}")

    logger.info("Pemulihan basis data dari %s berhasil dan terverifikasi.", backup_file.name)
    return True

