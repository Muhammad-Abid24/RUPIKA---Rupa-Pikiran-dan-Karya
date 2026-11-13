"""Acceptance tests for SF-009 Deterministic image QC and deduplication."""
from __future__ import annotations

import io
import json
from pathlib import Path
import imagehash
import pytest
from PIL import Image, ImageDraw
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from stockforge.db.models import Asset, Base, QcResult
from stockforge.qc.deterministic import DISCLAIMER_TEXT, DeterministicQcService


@pytest.fixture
def test_env(tmp_path: Path):
    """Setup isolated SQLite DB and storage directory."""
    db_path = tmp_path / "test_qc.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir(parents=True, exist_ok=True)
    return session_factory, storage_dir


def _create_image_file(path: Path, width: int, height: int, pattern: str = "sharp") -> tuple[str, str]:
    """Helper to generate test JPEG images with distinct patterns and compute hashes."""
    if pattern == "sharp":
        img = Image.new("RGB", (width, height), color="navy")
        draw = ImageDraw.Draw(img)
        draw.ellipse([(200, 200), (800, 800)], fill="gold", outline="orange", width=10)
        draw.rectangle([(500, 500), (1500, 1500)], fill="teal")
        draw.line([(0, 0), (width, height)], fill="white", width=8)
    elif pattern == "flat":
        # Flat solid color with no edges
        img = Image.new("RGB", (width, height), color=(128, 128, 128))
    else:
        img = Image.new("RGB", (width, height), color="navy")

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    data = buf.getvalue()

    path.write_bytes(data)
    import hashlib
    sha256 = hashlib.sha256(data).hexdigest()
    with Image.open(path) as decoded:
        phash = str(imagehash.phash(decoded))
    return sha256, phash


def _persist_test_asset(session_factory, asset_id: int, file_path: Path, width: int, height: int, sha256: str, phash: str) -> Asset:
    with session_factory() as session:
        asset = Asset(
            id=asset_id,
            filename=file_path.name,
            raw_path=str(file_path),
            sha256=sha256,
            phash=phash,
            width=width,
            height=height,
            status="generated",
        )
        session.add(asset)
        session.commit()
        session.refresh(asset)
        return asset


def test_detect_corrupt_jpeg_asset(test_env):
    """Verify acceptance criterion: fixture detects corrupted JPEG asset."""
    session_factory, storage_dir = test_env
    corrupt_file = storage_dir / "corrupt.jpg"
    corrupt_file.write_bytes(b"\xff\xd8\xff\xe0NOT_A_REAL_IMAGE_TRUNCATED_GARBAGE")

    _persist_test_asset(session_factory, 1, corrupt_file, 2048, 2048, "dummy_hash_1", "0000000000000000")

    service = DeterministicQcService(session_factory=session_factory)
    results = service.inspect_asset(asset_id=1)

    integrity_check = next(r for r in results if r.check_name == "jpeg_integrity")
    assert integrity_check.passed is False
    assert integrity_check.score == 0.0

    with session_factory() as s:
        asset = s.get(Asset, 1)
        assert asset.status == "qc_failed"


def test_detect_low_resolution_asset(test_env):
    """Verify acceptance criterion: fixture detects low-resolution asset (< 4 Megapixels)."""
    session_factory, storage_dir = test_env
    low_res_file = storage_dir / "low_res.jpg"
    # 1000 x 1000 = 1.0 MP (< 4.0 MP)
    sha, phash = _create_image_file(low_res_file, 1000, 1000, pattern="sharp")

    _persist_test_asset(session_factory, 2, low_res_file, 1000, 1000, sha, phash)

    service = DeterministicQcService(session_factory=session_factory, min_megapixels=4.0)
    results = service.inspect_asset(asset_id=2)

    res_check = next(r for r in results if r.check_name == "resolution")
    assert res_check.passed is False
    details = json.loads(res_check.details_json)
    assert details["megapixels"] == 1.0
    assert details["min_required_megapixels"] == 4.0

    with session_factory() as s:
        asset = s.get(Asset, 2)
        assert asset.status == "qc_failed"


def test_detect_blurry_or_flat_asset(test_env):
    """Verify acceptance criterion: fixture detects blurry or flat low-contrast asset."""
    session_factory, storage_dir = test_env
    flat_file = storage_dir / "flat.jpg"
    # 2048 x 2048 = 4.19 MP (> 4.0 MP), but flat solid color (no edges)
    sha, phash = _create_image_file(flat_file, 2048, 2048, pattern="flat")

    _persist_test_asset(session_factory, 3, flat_file, 2048, 2048, sha, phash)

    service = DeterministicQcService(session_factory=session_factory, min_laplacian_variance=15.0)
    results = service.inspect_asset(asset_id=3)

    blur_check = next(r for r in results if r.check_name == "blur_heuristic")
    assert blur_check.passed is False
    details = json.loads(blur_check.details_json)
    assert details["is_blurry"] is True

    with session_factory() as s:
        asset = s.get(Asset, 3)
        assert asset.status == "qc_failed"


def test_detect_duplicate_assets_exact_and_perceptual(test_env):
    """Verify acceptance criterion: fixture detects exact and perceptual near-duplicate assets."""
    session_factory, storage_dir = test_env

    # 1. Base sharp image (2048x2048)
    base_file = storage_dir / "base_sharp.jpg"
    sha1, phash1 = _create_image_file(base_file, 2048, 2048, pattern="sharp")
    _persist_test_asset(session_factory, 10, base_file, 2048, 2048, sha1, phash1)

    service = DeterministicQcService(session_factory=session_factory)

    # 2. Test exact duplicate check directly against DB
    with session_factory() as session:
        exact_res = service.check_exact_deduplication(session, sha256=sha1, asset_id=99)
        assert exact_res.passed is False
        assert exact_res.details["exact_duplicate_found"] is True
        assert exact_res.details["duplicate_asset_id"] == 10

    # 3. Test perceptual near-duplicate: create second image with slight pixel tweak (different sha256, but near-identical pHash)
    near_dup_file = storage_dir / "near_dup.jpg"
    with Image.open(base_file) as base_img:
        dup_img = base_img.copy()
        dup_img.putpixel((10, 10), (250, 0, 0))
        buf = io.BytesIO()
        dup_img.save(buf, format="JPEG", quality=90)
        data = buf.getvalue()
        near_dup_file.write_bytes(data)

    import hashlib
    sha2 = hashlib.sha256(data).hexdigest()
    with Image.open(near_dup_file) as decoded_dup:
        phash2 = str(imagehash.phash(decoded_dup))

    assert sha2 != sha1, "SHA-256 harus berbeda agar dapat tersimpan di tabel assets"

    _persist_test_asset(session_factory, 11, near_dup_file, 2048, 2048, sha2, phash2)

    results = service.inspect_asset(asset_id=11)
    perceptual_check = next(r for r in results if r.check_name == "perceptual_duplicate")
    assert perceptual_check.passed is False
    details = json.loads(perceptual_check.details_json)
    assert details["perceptual_duplicate_found"] is True
    assert details["closest_asset_id"] == 10


def test_valid_commercial_asset_passes_all_checks(test_env):
    """Verify that a sharp, high-resolution (>4 MP), unique JPEG passes all deterministic checks."""
    session_factory, storage_dir = test_env
    valid_file = storage_dir / "valid_commercial.jpg"
    # 2048 x 2048 = 4.19 MP (> 4.0 MP)
    sha, phash = _create_image_file(valid_file, 2048, 2048, pattern="sharp")
    _persist_test_asset(session_factory, 20, valid_file, 2048, 2048, sha, phash)

    service = DeterministicQcService(session_factory=session_factory)
    results = service.inspect_asset(asset_id=20)

    for r in results:
        assert r.passed is True, f"Check '{r.check_name}' harus lulus"

    with session_factory() as s:
        asset = s.get(Asset, 20)
        assert asset.status == "qc_passed"


def test_disclaimer_invariant_enforced(test_env):
    """Verify hard invariant: deterministic QC does not claim legal certification."""
    session_factory, storage_dir = test_env
    valid_file = storage_dir / "disclaimer_check.jpg"
    sha, phash = _create_image_file(valid_file, 2048, 2048, pattern="sharp")
    _persist_test_asset(session_factory, 30, valid_file, 2048, 2048, sha, phash)

    service = DeterministicQcService(session_factory=session_factory)
    results = service.inspect_asset(asset_id=30)

    for r in results:
        details = json.loads(r.details_json)
        assert "disclaimer" in details
        assert DISCLAIMER_TEXT in details["disclaimer"]
