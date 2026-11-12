"""Acceptance tests for SF-014A and SF-014B Docker Compose, named-volume persistence, and non-root security."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
import pytest
import yaml


@pytest.fixture
def compose_config():
    """Load and parse compose.yaml configuration."""
    compose_path = Path("compose.yaml")
    assert compose_path.exists(), "compose.yaml wajib ada di root project"
    with open(compose_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_compose_named_volume_declaration(compose_config):
    """Verify SF-014B: stockforge_data named volume is defined and mounted at /data."""
    volumes = compose_config.get("volumes", {})
    assert "stockforge_data" in volumes, "Named volume 'stockforge_data' wajib terdefinisi di compose.yaml"

    services = compose_config.get("services", {})
    assert "bot" in services, "Service 'bot' wajib ada di compose.yaml"
    assert "worker" in services, "Service 'worker' wajib ada di compose.yaml"

    # Verify both bot and worker mount stockforge_data to /data
    for svc_name in ("bot", "worker"):
        svc = services[svc_name]
        svc_vols = svc.get("volumes", [])
        has_data_mount = any("stockforge_data:/data" in str(v) for v in svc_vols)
        assert has_data_mount, f"Service '{svc_name}' wajib me-mount 'stockforge_data:/data'"


def test_compose_security_hardenings(compose_config):
    """Verify security invariants: no-new-privileges, cap_drop ALL, and no published ports."""
    services = compose_config.get("services", {})
    for svc_name, svc in services.items():
        sec_opts = svc.get("security_opt", [])
        assert "no-new-privileges:true" in sec_opts, f"Service '{svc_name}' wajib mengaktifkan no-new-privileges:true"

        cap_drops = svc.get("cap_drop", [])
        assert "ALL" in cap_drops, f"Service '{svc_name}' wajib menyertakan cap_drop: [ALL]"

        ports = svc.get("ports", [])
        assert len(ports) == 0, f"Service '{svc_name}' tidak boleh mengekspos public ports ke host luar"


def test_scheduler_profile_isolation(compose_config):
    """Verify that scheduler is isolated using profiles so it does not auto-start."""
    services = compose_config.get("services", {})
    assert "scheduler" in services, "Service 'scheduler' wajib terdefinisi di compose.yaml"
    scheduler_profiles = services["scheduler"].get("profiles", [])
    assert len(scheduler_profiles) > 0, "Service 'scheduler' wajib memiliki profiles agar tidak auto-start"
    assert "scheduler" in scheduler_profiles or "ops" in scheduler_profiles


def test_dockerfile_security_and_non_root():
    """Verify Dockerfile enforces non-root UID 10001 and /data ownership."""
    dockerfile_path = Path("Dockerfile")
    assert dockerfile_path.exists(), "Dockerfile wajib ada di root project"
    content = dockerfile_path.read_text(encoding="utf-8")

    assert "10001" in content, "Dockerfile wajib mendefinisikan UID/GID 10001"
    assert "USER 10001:10001" in content, "Dockerfile wajib menginstruksikan USER 10001:10001"
    assert "/data" in content, "Dockerfile wajib menyiapkan direktori /data"


def test_dockerignore_excludes_secrets_and_local_data():
    """Verify .dockerignore excludes .env, database files, and local output assets."""
    dockerignore_path = Path(".dockerignore")
    assert dockerignore_path.exists(), ".dockerignore wajib ada di root project"
    content = dockerignore_path.read_text(encoding="utf-8")

    assert ".env" in content
    assert "*.db" in content
    assert "data/" in content
    assert ".venv/" in content
    assert ".git" in content


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI tidak tersedia di environment")
def test_live_docker_named_volume_persistence():
    """Live verification using Docker daemon: verify named volume persists across container teardown."""
    # 1. Check if docker daemon is running
    try:
        res = subprocess.run(["docker", "info"], capture_output=True, timeout=5)
        if res.returncode != 0:
            pytest.skip("Docker daemon tidak aktif.")
    except Exception:
        pytest.skip("Gagal menghubungi Docker daemon.")

    # 2. Check persistence of SQLite database in named volume
    inspect_cmd = [
        "docker", "compose", "run", "--rm", "--entrypoint", "sqlite3", "bot",
        "/data/stockforge.db", "SELECT count(*) FROM sqlite_master WHERE type='table';"
    ]
    run_res = subprocess.run(inspect_cmd, capture_output=True, text=True, timeout=30)
    assert run_res.returncode == 0, f"Gagal membaca SQLite dari named volume: {run_res.stderr}"
    table_count = int(run_res.stdout.strip())
    assert table_count >= 29, f"Tabel di named volume harus >= 29, ditemukan: {table_count}"
