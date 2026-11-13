"""Acceptance tests for SF-010 Vision QC and metadata generation."""
from __future__ import annotations

import io
import json
from pathlib import Path
import pytest
from PIL import Image, ImageDraw
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from stockforge.db.models import Asset, AssetMetadata, Base, QcResult
from stockforge.providers.base import ProviderKind, RoleType
from stockforge.providers.mock import MockVisionProvider
from stockforge.providers.registry import ProviderRegistry
from stockforge.providers.router import ProviderRouter
from stockforge.qc.vision import (
    ADOBE_CATEGORIES,
    VISION_QC_DISCLAIMER,
    VisionQcService,
    clean_keywords,
    clean_title,
    safe_extract_json,
    validate_category_id,
)
from stockforge.storage.service import StorageService


@pytest.fixture
def test_env(tmp_path: Path):
    """Setup isolated SQLite DB and storage directory."""
    db_path = tmp_path / "test_vision_qc.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir(parents=True, exist_ok=True)
    storage_service = StorageService(base_data_dir=storage_dir)
    return session_factory, storage_service, storage_dir


def _create_sample_asset(session_factory, storage_dir: Path, asset_id: int = 1) -> Asset:
    raw_dir = storage_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    img_path = raw_dir / f"test_asset_{asset_id}.jpg"

    img = Image.new("RGB", (2000, 2000), color="skyblue")
    draw = ImageDraw.Draw(img)
    draw.rectangle([(200, 200), (1800, 1800)], fill="forestgreen")
    draw.ellipse([(600, 600), (1400, 1400)], fill="gold")

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    data = buf.getvalue()
    img_path.write_bytes(data)

    import hashlib
    sha256 = hashlib.sha256(data).hexdigest()

    with session_factory() as session:
        asset = Asset(
            id=asset_id,
            filename=img_path.name,
            raw_path=str(img_path),
            sha256=sha256,
            phash="0000111122223333",
            width=2000,
            height=2000,
            status="generated",
        )
        session.add(asset)
        session.commit()
        session.refresh(asset)
        return asset



# ---------------------------------------------------------------------------
# Test 1: Title cleaning and strict length enforcement (<= 70 chars)
# ---------------------------------------------------------------------------
def test_clean_title_enforcement():
    # Title <= 70 chars remains intact
    short_title = "Lush Tropical Rainforest with Sunbeam"
    assert clean_title(short_title) == short_title

    # Title > 70 chars is truncated at word boundary <= 70 chars
    long_title = (
        "A breathtaking aerial view of a majestic mountain range during golden hour "
        "with colorful sunset clouds illuminating the high snowy peaks in alpine valley"
    )
    cleaned = clean_title(long_title)
    assert len(cleaned) <= 70
    assert not cleaned.endswith(" ")
    assert not cleaned.endswith(",")
    # Make sure words are not cut in half
    assert cleaned == "A breathtaking aerial view of a majestic mountain range during golden"

    # Single long word without spaces
    long_single_word = "A" * 90
    cleaned_single = clean_title(long_single_word)
    assert len(cleaned_single) == 70

    # Empty / None title fallback
    assert clean_title("") == "Commercial Stock Photo Asset"
    assert clean_title(None) == "Commercial Stock Photo Asset"


# ---------------------------------------------------------------------------
# Test 2: Keywords cleaning, ordering, deduplication, and <= 40 cap
# ---------------------------------------------------------------------------
def test_clean_keywords_enforcement():
    raw = [
        "Nature", "NATURE", "landscape", "Mountain", "mountain",
        "forest", "Forest", "Trees", "sky", "outdoor", "Outdoor"
    ]
    cleaned = clean_keywords(raw, max_count=40)
    # Deduplicated case-insensitively, preserved relevance order
    assert cleaned == ["nature", "landscape", "mountain", "forest", "trees", "sky", "outdoor"]

    # Comma-separated string format
    str_raw = "tropical, beach, Ocean, ocean, sunny, Summer, sunny"
    cleaned_str = clean_keywords(str_raw, max_count=40)
    assert cleaned_str == ["tropical", "beach", "ocean", "sunny", "summer"]

    # Cap at conservative MVP limit <= 40
    fifty_keywords = [f"tag{i:02d}" for i in range(50)]
    capped = clean_keywords(fifty_keywords, max_count=40)
    assert len(capped) == 40
    assert capped[0] == "tag00"
    assert capped[-1] == "tag39"


# ---------------------------------------------------------------------------
# Test 3: Anti-hallucination and brand/AI terms scrubbing
# ---------------------------------------------------------------------------
def test_anti_hallucination_and_brand_scrubbing():
    dirty_keywords = [
        "mountain", "Apple", "iphone", "hiking", "Nike", "shoes",
        "Midjourney", "dall-e", "stablediffusion", "landscape", "Sony"
    ]
    cleaned = clean_keywords(dirty_keywords, max_count=40)
    # Trademarked brands and AI terms must be scrubbed
    for forbidden in ["apple", "iphone", "nike", "midjourney", "dall-e", "stablediffusion", "sony"]:
        assert forbidden not in cleaned

    assert cleaned == ["mountain", "hiking", "shoes", "landscape"]


# ---------------------------------------------------------------------------
# Test 4: Adobe Stock category validation (1 to 21)
# ---------------------------------------------------------------------------
def test_category_validation():
    # Valid integers
    assert validate_category_id(1) == 1
    assert validate_category_id(11) == 11
    assert validate_category_id(21) == 21

    # Valid string digits
    assert validate_category_id("11") == 11

    # Valid category names (case-insensitive)
    assert validate_category_id("Landscapes") == 11
    assert validate_category_id("animals") == 1
    assert validate_category_id("Technology") == 19

    # Invalid integers & strings
    assert validate_category_id(0) is None
    assert validate_category_id(22) is None
    assert validate_category_id(-5) is None
    assert validate_category_id("unrecognized_category_name") is None
    assert validate_category_id(None) is None


# ---------------------------------------------------------------------------
# Test 5: Vision QC Inspection (passed and failed scenarios)
# ---------------------------------------------------------------------------
def test_vision_qc_inspection(test_env):
    import asyncio
    session_factory, storage_service, storage_dir = test_env
    asset = _create_sample_asset(session_factory, storage_dir, asset_id=101)

    mock_provider = MockVisionProvider("test-vision-qc")
    mock_provider.custom_analysis = {
        "passed": True,
        "confidence": 0.96,
        "issues": [],
        "notes": "Sharp, clean composition, no AI artifacts",
    }
    mock_provider.custom_passed = True

    service = VisionQcService(
        session_factory=session_factory,
        storage_service=storage_service,
        router_or_provider=mock_provider,
    )

    qc_res = asyncio.run(service.inspect_image_vision(asset_id=asset.id))
    assert qc_res.check_name == "vision_qc"
    assert qc_res.passed is True
    assert qc_res.score == pytest.approx(0.96)

    details = json.loads(qc_res.details_json)
    assert details["passed"] is True
    assert details["confidence"] == pytest.approx(0.96)
    assert details["issues"] == []
    assert VISION_QC_DISCLAIMER in details["disclaimer"]

    # Verify second test with detected issues / failure
    mock_provider.custom_analysis = {
        "passed": False,
        "confidence": 0.88,
        "issues": ["visible_hallucinated_hand", "blurry_texture"],
        "notes": "Visible anatomical distortion in human hand",
    }
    mock_provider.custom_passed = False

    qc_res_failed = asyncio.run(service.inspect_image_vision(asset_id=asset.id))
    assert qc_res_failed.passed is False
    assert qc_res_failed.score == pytest.approx(0.88)
    details_failed = json.loads(qc_res_failed.details_json)
    assert "visible_hallucinated_hand" in details_failed["issues"]


# ---------------------------------------------------------------------------
# Test 6: Metadata Generation & Persistence
# ---------------------------------------------------------------------------
def test_metadata_generation_and_persistence(test_env):
    import asyncio
    session_factory, storage_service, storage_dir = test_env
    asset = _create_sample_asset(session_factory, storage_dir, asset_id=202)

    mock_provider = MockVisionProvider("test-vision-meta")
    mock_provider.custom_analysis = {
        "title": "Sunlit Emerald Forest with Tall Pines and Mossy Rocks",
        "keywords": [
            "forest", "trees", "nature", "moss", "pines", "sunlight",
            "foliage", "woods", "scenic", "wilderness", "green", "outdoor"
        ],
        "category_id": 11,  # Landscapes
        "content_type": "photo",
    }

    service = VisionQcService(
        session_factory=session_factory,
        storage_service=storage_service,
        router_or_provider=mock_provider,
    )

    meta_entry = asyncio.run(service.generate_metadata(asset_id=asset.id))
    assert meta_entry.asset_id == asset.id
    assert meta_entry.title == "Sunlit Emerald Forest with Tall Pines and Mossy Rocks"
    assert meta_entry.category_id == 11
    assert meta_entry.content_type == "photo"

    keywords = json.loads(meta_entry.keywords_json)
    assert len(keywords) == 12
    assert "forest" in keywords
    assert "moss" in keywords

    # Verify DB persistence directly
    with session_factory() as session:
        loaded = session.get(AssetMetadata, asset.id)
        assert loaded is not None
        assert loaded.title == meta_entry.title
        assert loaded.category_id == 11


# ---------------------------------------------------------------------------
# Test 7: Safe recovery from malformed output or provider failure
# ---------------------------------------------------------------------------
def test_malformed_and_failure_safe_recovery(test_env):
    import asyncio
    session_factory, storage_service, storage_dir = test_env
    asset = _create_sample_asset(session_factory, storage_dir, asset_id=303)

    mock_provider = MockVisionProvider("test-vision-fail")
    # Simulate provider throwing an unhandled network error
    mock_provider.should_fail = True

    service = VisionQcService(
        session_factory=session_factory,
        storage_service=storage_service,
        router_or_provider=mock_provider,
    )

    # 1. Vision QC should safely record a failed QC record without unhandled crash
    qc_res = asyncio.run(service.inspect_image_vision(asset_id=asset.id, safe_fallback=True))
    assert qc_res.passed is False
    assert qc_res.score == 0.0
    details = json.loads(qc_res.details_json)
    assert "vision_qc_processing_error" in details["issues"]

    # 2. Metadata generation should safely generate fallback metadata flagged for manual review
    meta = asyncio.run(service.generate_metadata(asset_id=asset.id, safe_fallback=True))
    assert meta.asset_id == asset.id
    assert meta.title.startswith("Stock Asset")
    assert len(json.loads(meta.keywords_json)) >= 4



# ---------------------------------------------------------------------------
# Test 8: Extraction from markdown-wrapped JSON
# ---------------------------------------------------------------------------
def test_safe_extract_json():
    # Direct dict
    d = {"a": 1}
    assert safe_extract_json(d) == d

    # Markdown block
    md = "Here is the response:\n```json\n{\"title\": \"Test Title\", \"category_id\": 5}\n```\nHope this helps!"
    extracted = safe_extract_json(md)
    assert extracted["title"] == "Test Title"
    assert extracted["category_id"] == 5

    # Outer braces
    raw_text = "Some intro text: {\"title\": \"Plain Text Title\"} and outro."
    extracted_braces = safe_extract_json(raw_text)
    assert extracted_braces["title"] == "Plain Text Title"

    # Invalid payload raises ValueError
    with pytest.raises(ValueError):
        safe_extract_json("Not a json at all")


# ---------------------------------------------------------------------------
# Test 9: Fail-Closed Guardrail: Error / Timeout never marks asset as passed
# ---------------------------------------------------------------------------
def test_fail_closed_guardrail_on_error_or_timeout(test_env):
    import asyncio
    session_factory, storage_service, storage_dir = test_env
    asset = _create_sample_asset(session_factory, storage_dir, asset_id=301)

    # Initially asset status is generated or qc_passed
    with session_factory() as session:
        a = session.get(Asset, 301)
        a.status = "qc_passed"
        session.commit()

    # Provider raises timeout
    failing_provider = MockVisionProvider("flaky-vision-qc")
    failing_provider.should_fail = True

    service = VisionQcService(
        session_factory=session_factory,
        storage_service=storage_service,
        router_or_provider=failing_provider,
    )

    qc_res = asyncio.run(service.inspect_image_vision(asset_id=301, safe_fallback=True))

    # Assert FAIL-CLOSED: QC passed is strictly False
    assert qc_res.passed is False
    assert qc_res.score == 0.0

    # Assert Asset status is reverted to qc_failed in database
    with session_factory() as session:
        updated_asset = session.get(Asset, 301)
        assert updated_asset.status == "qc_failed"


# ---------------------------------------------------------------------------
# Test 10: Anatomy Detection Guardrail: Hands, Fingers, Face, and Interactions
# ---------------------------------------------------------------------------
def test_anatomy_detection_guardrail(test_env):
    import asyncio
    session_factory, storage_service, storage_dir = test_env
    asset = _create_sample_asset(session_factory, storage_dir, asset_id=302)

    mock_provider = MockVisionProvider("anatomy-vision-qc")
    # Scenario: Model claims passed=true, but anatomy evaluation contains finger defect
    mock_provider.custom_analysis = {
        "passed": True,  # Model incorrectly said true
        "confidence": 0.82,
        "anatomy": {
            "has_humans": True,
            "hands_and_fingers_defect": True,
            "face_defect": False,
            "interaction_defect": True,
            "details": "Left hand holding cup has 6 fingers and blends into cup handle"
        },
        "issues": ["polydactyly detected", "finger blending into cup handle"],
        "notes": "Hand has six fingers with melting joints",
    }
    mock_provider.custom_passed = True

    service = VisionQcService(
        session_factory=session_factory,
        storage_service=storage_service,
        router_or_provider=mock_provider,
    )

    qc_res = asyncio.run(service.inspect_image_vision(asset_id=302))

    # Fail-Closed Guardrail: anatomy defect MUST force passed=False
    assert qc_res.passed is False
    details = json.loads(qc_res.details_json)
    assert details["passed"] is False
    assert details["anatomy"]["hands_and_fingers_defect"] is True
    assert details["anatomy"]["interaction_defect"] is True
    assert "anatomical_defect_hands_or_fingers" in details["issues"]
    assert "anatomical_defect_interaction" in details["issues"]

    # Verify asset status is qc_failed
    with session_factory() as session:
        updated_asset = session.get(Asset, 302)
        assert updated_asset.status == "qc_failed"


# ---------------------------------------------------------------------------
# Test 11: QC Evidence Guardrail: Persist Model, Timestamp, Score, and Evidence
# ---------------------------------------------------------------------------
def test_qc_evidence_persistence(test_env):
    import asyncio
    session_factory, storage_service, storage_dir = test_env
    asset = _create_sample_asset(session_factory, storage_dir, asset_id=303)

    mock_provider = MockVisionProvider("evidence-model-v2")
    mock_provider.custom_analysis = {
        "passed": True,
        "confidence": 0.94,
        "anatomy": {
            "has_humans": True,
            "hands_and_fingers_defect": False,
            "face_defect": False,
            "interaction_defect": False,
            "details": "All 5 fingers clearly visible and anatomically correct"
        },
        "issues": [],
        "notes": "Crisp human hands with accurate anatomical proportions",
    }
    mock_provider.custom_passed = True

    service = VisionQcService(
        session_factory=session_factory,
        storage_service=storage_service,
        router_or_provider=mock_provider,
    )

    qc_res = asyncio.run(service.inspect_image_vision(asset_id=303, model_override="rupika-qc-expert"))

    assert qc_res.passed is True
    assert qc_res.score == pytest.approx(0.94)

    details = json.loads(qc_res.details_json)
    # Check that model, score, timestamp, and anatomy evidence are recorded
    assert details["model"] == "rupika-qc-expert"
    assert "checked_at" in details and len(details["checked_at"]) > 0
    assert details["anatomy"]["has_humans"] is True
    assert details["anatomy"]["hands_and_fingers_defect"] is False
    assert details["notes"] == "Crisp human hands with accurate anatomical proportions"

