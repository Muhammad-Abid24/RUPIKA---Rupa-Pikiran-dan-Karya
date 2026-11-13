"""Acceptance tests for SF-012 Adobe compliance and CSV/ZIP export."""
from __future__ import annotations

import csv
import io
import json
import zipfile
from pathlib import Path
import pytest
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from stockforge.db.models import Asset, AssetMetadata, Base, ExportBatch, ExportItem
from stockforge.export import (
    ADOBE_CSV_HEADERS,
    AdobeCsvRow,
    ComplianceExportError,
    ExportSecurityError,
    ExportService,
    generate_adobe_csv,
    safe_add_bytes_to_zip,
    safe_add_file_to_zip,
    sanitize_adobe_filename,
    validate_asset_compliance,
)
from stockforge.storage.service import StorageService


@pytest.fixture
def export_env(tmp_path: Path):
    """Setup isolated database and storage directory."""
    db_path = tmp_path / "test_export.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    data_dir = tmp_path / "data"
    raw_dir = data_dir / "raw"
    exports_dir = data_dir / "exports"
    raw_dir.mkdir(parents=True, exist_ok=True)
    exports_dir.mkdir(parents=True, exist_ok=True)

    storage_service = StorageService(base_data_dir=data_dir)
    export_service = ExportService(session_factory=session_factory, storage_service=storage_service)
    return session_factory, export_service, raw_dir, exports_dir


def _create_exportable_asset(
    session_factory,
    raw_dir: Path,
    asset_id: int = 1,
    status: str = "approved",
    license_verified: bool = True,
    license_evidence: str = "Adobe Stock Commercial Sublicense v1",
    filename: str | None = None,
) -> Asset:
    """Helper to create a fully compliant or customized asset in the test database."""
    fname = filename or f"stock_asset_{asset_id}.jpg"
    img_path = raw_dir / fname

    img = Image.new("RGB", (2000, 2000), color="darkgreen")
    img.putpixel((0, 0), (asset_id % 256, (asset_id * 7) % 256, (asset_id * 13) % 256))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    data = buf.getvalue()
    img_path.write_bytes(data)

    import hashlib
    sha256 = hashlib.sha256(data).hexdigest()

    with session_factory() as session:
        asset = Asset(
            id=asset_id,
            filename=fname,
            raw_path=str(img_path),
            sha256=sha256,
            phash=f"phash_{asset_id}",
            width=2000,
            height=2000,
            status=status,
            license_verified=license_verified,
            license_evidence=license_evidence,
            ai_generated=True,
        )
        session.add(asset)

        from stockforge.db.models import utc_now
        meta = AssetMetadata(
            asset_id=asset_id,
            title=f"Lush Emerald Tropical Rainforest at Dawn #{asset_id}",
            keywords_json=json.dumps([
                "rainforest", "trees", "nature", "foliage", "green", "tropical", "wilderness"
            ]),
            category_id=11,  # Landscapes
            content_type="photo",
            metadata_reviewed_at=utc_now(),
        )

        session.add(meta)
        session.commit()
        session.refresh(asset)
        _ = asset.metadata_entry
        session.expunge_all()
        return asset



# ---------------------------------------------------------------------------
# Test 1: Unapproved asset cannot be exported
# ---------------------------------------------------------------------------
def test_unapproved_asset_cannot_export(export_env):
    session_factory, export_service, raw_dir, _ = export_env
    # Status is 'qc_passed', NOT 'approved'
    asset = _create_exportable_asset(session_factory, raw_dir, asset_id=1, status="qc_passed")

    is_valid, errors = validate_asset_compliance(asset)
    assert is_valid is False
    assert any("belum disetujui manusia" in e for e in errors)

    with pytest.raises(ComplianceExportError) as exc_info:
        export_service.export_approved_assets(asset_ids=[1])
    assert "gagal verifikasi kepatuhan" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Test 2: Unlicensed asset cannot be exported
# ---------------------------------------------------------------------------
def test_unlicensed_asset_cannot_export(export_env):
    session_factory, export_service, raw_dir, _ = export_env
    # Status is 'approved', but license_verified is False and evidence is empty
    asset = _create_exportable_asset(
        session_factory,
        raw_dir,
        asset_id=2,
        status="approved",
        license_verified=False,
        license_evidence="",
    )

    is_valid, errors = validate_asset_compliance(asset)
    assert is_valid is False
    assert any("lisensi komersial belum terverifikasi" in e for e in errors)

    with pytest.raises(ComplianceExportError):
        export_service.export_approved_assets(asset_ids=[2])


# ---------------------------------------------------------------------------
# Test 3: Safe filename generation <= 30 characters
# ---------------------------------------------------------------------------
def test_safe_filename_generation_under_30_chars():
    # Long original name > 30 chars
    long_name = "an_extremely_long_detailed_nature_landscape_photo_of_mountains_at_sunset.jpeg"
    safe_name = sanitize_adobe_filename(long_name, asset_id=42, sha256_hex="abcdef0123456789")
    assert len(safe_name) <= 30
    assert safe_name.endswith(".jpg")
    assert safe_name == "rf_00042_abcdef01.jpg"

    # Clean short name <= 30 chars remains preserved
    short_name = "mountain_peak.jpg"
    safe_short = sanitize_adobe_filename(short_name, asset_id=1, sha256_hex="12345678")
    assert safe_short == "mountain_peak.jpg"


# ---------------------------------------------------------------------------
# Test 4: Official Adobe Contributor CSV template check
# ---------------------------------------------------------------------------
def test_adobe_csv_format_and_escaping():
    rows = [
        AdobeCsvRow(
            filename="rf_00001_12345678.jpg",
            title="Serene Mountain Lake at Sunrise",
            keywords="mountain, lake, sunrise, nature, serene, outdoor, reflection",
            category=11,
        ),
        AdobeCsvRow(
            filename="rf_00002_87654321.jpg",
            title="Modern Business Office with Colleagues Working",
            keywords="business, office, teamwork, colleagues, corporate, technology",
            category=3,
        ),
    ]

    csv_output = generate_adobe_csv(rows)

    # Verify header line
    lines = csv_output.strip().split("\r\n")
    assert lines[0] == "Filename,Title,Keywords,Category"
    assert len(lines) == 3

    # Parse with standard csv.reader to check RFC 4180 parsing
    reader = list(csv.DictReader(io.StringIO(csv_output)))
    assert len(reader) == 2

    assert reader[0]["Filename"] == "rf_00001_12345678.jpg"
    assert reader[0]["Title"] == "Serene Mountain Lake at Sunrise"
    assert "mountain, lake" in reader[0]["Keywords"]
    assert reader[0]["Category"] == "11"


# ---------------------------------------------------------------------------
# Test 5: ZIP package generation, contents, and manifest.json
# ---------------------------------------------------------------------------
def test_zip_packaging_and_manifest(export_env):
    session_factory, export_service, raw_dir, exports_dir = export_env

    # Create 2 approved compliant assets
    _create_exportable_asset(session_factory, raw_dir, asset_id=10, status="approved")
    _create_exportable_asset(session_factory, raw_dir, asset_id=11, status="approved")

    batch, zip_path = export_service.export_approved_assets()

    assert zip_path.is_file()
    assert zip_path.suffix == ".zip"

    # Inspect zip contents
    with zipfile.ZipFile(zip_path, "r") as zf:
        namelist = zf.namelist()
        assert "adobe_metadata.csv" in namelist
        assert "manifest.json" in namelist
        # Ensure images are in the zip
        image_entries = [n for n in namelist if n.endswith(".jpg")]
        assert len(image_entries) == 2

        # Verify CSV in zip matches expected header
        csv_bytes = zf.read("adobe_metadata.csv")
        assert b"Filename,Title,Keywords,Category" in csv_bytes

        # Verify Manifest JSON
        manifest_bytes = zf.read("manifest.json")
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        assert manifest["total_assets"] == 2
        assert manifest["generative_ai_disclosure"] != ""
        assert manifest["submission_mode"] == "manual"
        assert len(manifest["assets"]) == 2

        asset_entry = manifest["assets"][0]
        assert asset_entry["license_verified"] is True
        assert asset_entry["generative_ai_disclosure"] == "required"
        assert asset_entry["human_reviewed_at"] is not None

    # Verify DB persistence
    with session_factory() as session:
        db_batch = session.get(ExportBatch, batch.id)
        assert db_batch is not None
        assert db_batch.archive_path == str(zip_path)

        items = session.query(ExportItem).filter_by(batch_id=batch.id).all()
        assert len(items) == 2

        # Verify asset statuses transitioned to 'exported'
        for aid in [10, 11]:
            a = session.get(Asset, aid)
            assert a.status == "exported"


# ---------------------------------------------------------------------------
# Test 6: Anti-Zip-Slip validation
# ---------------------------------------------------------------------------
def test_anti_zip_slip_protection(tmp_path: Path):
    zip_target = tmp_path / "security_test.zip"
    sample_file = tmp_path / "sample.txt"
    sample_file.write_text("secure content")

    with zipfile.ZipFile(zip_target, "w") as zf:
        # Legal flat name succeeds
        safe_add_file_to_zip(zf, sample_file, "clean_file.txt")
        safe_add_bytes_to_zip(zf, b"some bytes", "data.csv")

        # Zip-Slip path traversal patterns MUST be blocked
        with pytest.raises(ExportSecurityError):
            safe_add_file_to_zip(zf, sample_file, "../escaped.txt")

        with pytest.raises(ExportSecurityError):
            safe_add_file_to_zip(zf, sample_file, "subdir/nested.txt")

        with pytest.raises(ExportSecurityError):
            safe_add_file_to_zip(zf, sample_file, "/etc/passwd")

        with pytest.raises(ExportSecurityError):
            safe_add_bytes_to_zip(zf, b"payload", "../../root.sh")


# ---------------------------------------------------------------------------
# Test 7: Manual submission invariant (ALLOW_ADOBE_AUTO_SUBMISSION=False)
# ---------------------------------------------------------------------------
def test_manual_submission_invariant(export_env, monkeypatch):
    session_factory, export_service, raw_dir, _ = export_env
    _create_exportable_asset(session_factory, raw_dir, asset_id=99, status="approved")

    # Mock settings where auto submission was somehow enabled
    from stockforge import config
    settings = config.get_settings()
    # Force attribute to True via monkeypatch to test invariant guard in ExportService
    monkeypatch.setattr(settings, "allow_adobe_auto_submission", True)
    monkeypatch.setattr("stockforge.export.service.get_settings", lambda: settings)

    with pytest.raises(RuntimeError) as exc_info:
        export_service.export_approved_assets(asset_ids=[99])
    assert "Invarian keamanan dilanggar" in str(exc_info.value)

