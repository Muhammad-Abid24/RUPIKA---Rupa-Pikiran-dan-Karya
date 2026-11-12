"""Acceptance tests for SF-001 Bootstrap and CLI."""
import logging
from pathlib import Path
from typer.testing import CliRunner
import pytest

from stockforge.cli import app
from stockforge.config import Settings
from stockforge.logging import SecretMaskingFilter, mask_secret, setup_logging

runner = CliRunner()


def test_cli_help():
    """Verify stockforge / rupika --help displays usage and commands."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "RUPIKA" in result.output
    assert "init" in result.output
    assert "doctor" in result.output


def test_cli_init_creates_directories(tmp_path: Path):
    """Verify stockforge init creates data subdirectories with write access."""
    target_data_dir = tmp_path / "sf_data"
    result = runner.invoke(app, ["init", "--data-dir", str(target_data_dir)])
    assert result.exit_code == 0
    assert "Inisialisasi berhasil" in result.output

    expected_subdirs = ["raw", "processed", "thumbnails", "exports", "backups", "logs"]
    for subdir in expected_subdirs:
        subpath = target_data_dir / subdir
        assert subpath.exists()
        assert subpath.is_dir()


def test_cli_doctor_reports_safely(tmp_path: Path, monkeypatch):
    """Verify stockforge doctor reports missing secrets without leaking any raw values."""
    test_secret = "sk-supersecretapikey123456"
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RESEARCH_API_KEY", test_secret)
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "12345,67890")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")  # Explicitly test missing secret behavior

    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "RUPIKA Doctor" in result.output

    # The secret must be masked, NEVER printed in full raw text
    assert test_secret not in result.output
    assert "3456" in result.output  # show_last=4

    # Missing secrets should be clearly indicated
    assert "BELUM DIISI" in result.output
    assert "TELEGRAM_BOT_TOKEN" in result.output


def test_settings_invariant_prevents_adobe_auto_submission():
    """Verify invariant: ALLOW_ADOBE_AUTO_SUBMISSION cannot be True."""
    with pytest.raises(ValueError, match="ALLOW_ADOBE_AUTO_SUBMISSION tidak boleh bernilai True"):
        Settings(ALLOW_ADOBE_AUTO_SUBMISSION=True)


def test_settings_admin_ids_parsing():
    """Verify parsing of comma-separated telegram admin IDs."""
    settings = Settings(TELEGRAM_ADMIN_IDS="111, 222, invalid, 333")
    assert settings.admin_ids_list == [111, 222, 333]


def test_logging_masks_secrets():
    """Verify logging filter redacts sensitive credentials from output."""
    raw_key = "api-key: my_secret_token_abc123"
    logger = logging.getLogger("test_masking_logger")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    # Use SecretMaskingFilter
    class ListHandler(logging.Handler):
        def __init__(self):
            super().__init__()
            self.records = []

        def emit(self, record):
            self.records.append(record.msg)

    handler = ListHandler()
    handler.addFilter(SecretMaskingFilter())
    logger.addHandler(handler)

    logger.info("Connecting with api_key: secret_abc_xyz")
    assert len(handler.records) == 1
    assert "secret_abc_xyz" not in handler.records[0]
    assert "***REDACTED***" in handler.records[0]


def test_required_configuration_files_exist():
    """Verify required project bootstrap templates exist in repository."""
    root = Path(__file__).parent.parent
    assert (root / ".env.example").exists()
    assert (root / "config" / "models.yaml").exists()
    assert (root / "config" / "research.yaml").exists()
    assert (root / ".gitignore").exists()

    gitignore_content = (root / ".gitignore").read_text()
    assert ".env" in gitignore_content
    assert "*.db" in gitignore_content
    assert "exports/" in gitignore_content

