"""Acceptance tests for SF-014C Local container restart, persistence, backup and rollback."""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
from pathlib import Path
import pytest


def test_deploy_scripts_exist_and_executable():
    """Verify that backup and restore scripts exist and have execute permissions."""
    backup_sh = Path("deploy/backup.sh")
    restore_sh = Path("deploy/restore.sh")
    rollback_md = Path("deploy/rollback.md")

    assert backup_sh.exists(), "deploy/backup.sh wajib ada"
    assert restore_sh.exists(), "deploy/restore.sh wajib ada"
    assert rollback_md.exists(), "deploy/rollback.md wajib ada"

    assert os.access(backup_sh, os.X_OK), "deploy/backup.sh harus executable"
    assert os.access(restore_sh, os.X_OK), "deploy/restore.sh harus executable"


def test_rollback_documentation_safety_warnings():
    """Verify deploy/rollback.md contains critical safety warnings against down -v."""
    rollback_md = Path("deploy/rollback.md")
    content = rollback_md.read_text(encoding="utf-8")

    assert "down -v" in content, "rollback.md wajib memperingatkan bahaya 'down -v'"
    assert "stockforge_data" in content or "named volume" in content
    assert "sqlite3" in content
    assert "restore.sh" in content


def test_backup_and_restore_script_logic(tmp_path: Path):
    """Test backup.sh and restore.sh execution on an isolated SQLite database."""
    # 1. Create source SQLite database with test table and data
    src_db = tmp_path / "test_source.db"
    conn = sqlite3.connect(src_db)
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode = WAL;")
    cursor.execute("CREATE TABLE test_data (id INTEGER PRIMARY KEY, note TEXT);")
    cursor.execute("INSERT INTO test_data (note) VALUES ('Initial Backup Data 1');")
    cursor.execute("INSERT INTO test_data (note) VALUES ('Initial Backup Data 2');")
    conn.commit()
    conn.close()

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    # 2. Run deploy/backup.sh
    backup_script = Path("deploy/backup.sh").resolve()
    res_backup = subprocess.run(
        ["sh", str(backup_script), str(src_db), str(backup_dir)],
        capture_output=True,
        text=True,
    )
    assert res_backup.returncode == 0, f"backup.sh gagal: {res_backup.stderr}"
    assert "Backup Selesai dengan Sukses" in res_backup.stdout

    # Verify backup file and checksum generated
    backup_files = list(backup_dir.glob("stockforge_backup_*.db"))
    assert len(backup_files) == 1
    backup_file = backup_files[0]
    checksum_file = Path(f"{backup_file}.sha256")
    assert checksum_file.exists()

    # 3. Simulate database drift / modification
    conn = sqlite3.connect(src_db)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO test_data (note) VALUES ('Drift Data After Backup');")
    conn.commit()
    count_before_restore = cursor.execute("SELECT count(*) FROM test_data;").fetchone()[0]
    conn.close()
    assert count_before_restore == 3

    # 4. Run deploy/restore.sh to restore back to backup state
    restore_script = Path("deploy/restore.sh").resolve()
    res_restore = subprocess.run(
        ["sh", str(restore_script), str(backup_file), str(src_db)],
        capture_output=True,
        text=True,
    )
    assert res_restore.returncode == 0, f"restore.sh gagal: {res_restore.stderr}"
    assert "Restore Selesai dengan Sukses" in res_restore.stdout

    # 5. Verify restored database has original count of 2 records
    conn = sqlite3.connect(src_db)
    cursor = conn.cursor()
    count_after_restore = cursor.execute("SELECT count(*) FROM test_data;").fetchone()[0]
    integrity = cursor.execute("PRAGMA integrity_check;").fetchone()[0]
    conn.close()

    assert count_after_restore == 2, "Data hasil restore harus sesuai dengan snapshot backup"
    assert integrity == "ok", "Integritas database hasil restore harus ok"


def test_restore_rejects_corrupted_backup(tmp_path: Path):
    """Verify restore.sh fails safely if the backup file is corrupted, preserving active DB."""
    # Create active db
    active_db = tmp_path / "active.db"
    conn = sqlite3.connect(active_db)
    conn.execute("CREATE TABLE safety (val TEXT);")
    conn.execute("INSERT INTO safety VALUES ('Critical Active Data');")
    conn.commit()
    conn.close()

    # Create corrupted backup file
    corrupt_backup = tmp_path / "corrupt.db"
    corrupt_backup.write_bytes(b"CORRUPTED_NON_SQLITE_GARBAGE_PAYLOAD")

    restore_script = Path("deploy/restore.sh").resolve()
    res_restore = subprocess.run(
        ["sh", str(restore_script), str(corrupt_backup), str(active_db)],
        capture_output=True,
        text=True,
    )

    assert res_restore.returncode != 0, "Restore harus gagal saat file backup korup"
    assert "korup atau tidak valid" in res_restore.stderr or "korup atau tidak valid" in res_restore.stdout

    # Verify active DB was not overwritten
    conn = sqlite3.connect(active_db)
    val = conn.execute("SELECT val FROM safety;").fetchone()[0]
    conn.close()
    assert val == "Critical Active Data", "Database aktif harus tetap aman saat backup korup ditolak"


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI tidak tersedia")
def test_live_docker_backup_and_rollback():
    """Verify live container backup drill and persistent volume preservation across down/up cycle."""
    try:
        res = subprocess.run(["docker", "info"], capture_output=True, timeout=5)
        if res.returncode != 0:
            pytest.skip("Docker daemon tidak aktif.")
    except Exception:
        pytest.skip("Gagal menghubungi Docker daemon.")

    # 1. Run backup script inside container
    backup_cmd = ["docker", "compose", "run", "--rm", "--entrypoint", "/bin/sh", "bot", "/app/deploy/backup.sh"]
    run_backup = subprocess.run(backup_cmd, capture_output=True, text=True, timeout=30)
    assert run_backup.returncode == 0, f"Backup via container gagal: {run_backup.stderr}"
    assert "Backup Selesai dengan Sukses" in run_backup.stdout

    # 2. Simulate container restart / teardown (without -v)
    down_cmd = ["docker", "compose", "down"]
    run_down = subprocess.run(down_cmd, capture_output=True, text=True, timeout=30)
    assert run_down.returncode == 0

    # 3. Spawn container again and verify database and backups survive
    check_cmd = [
        "docker", "compose", "run", "--rm", "--entrypoint", "sqlite3", "bot",
        "/data/stockforge.db", "PRAGMA integrity_check;"
    ]
    run_check = subprocess.run(check_cmd, capture_output=True, text=True, timeout=30)
    assert run_check.returncode == 0
    assert "ok" in run_check.stdout

