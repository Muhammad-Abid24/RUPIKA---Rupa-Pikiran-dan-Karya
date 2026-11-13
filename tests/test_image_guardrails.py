"""Unit tests for anti-plagiarism, trademark avoidance, and deduplication guardrails."""
from __future__ import annotations

import asyncio
from pathlib import Path
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from stockforge.db.models import Asset, Base
from stockforge.images.guardrails import (
    PROHIBITED_CHARACTERS,
    PROHIBITED_TRADEMARKS,
    compute_prompt_fingerprint,
    sanitize_and_enforce_plagiarism_free_prompt,
    validate_prompt_anti_plagiarism,
)
from stockforge.images.service import (
    DuplicateAssetError,
    ImageGenerationService,
)
from stockforge.providers.mock import MockImageProvider
from stockforge.storage.service import StorageService


def test_validate_clean_prompt_passes():
    prompt = "A smiling barista pouring specialty coffee in a warm rustic cafe"
    passed, violations = validate_prompt_anti_plagiarism(prompt)
    assert passed is True
    assert len(violations) == 0


def test_validate_prohibited_trademark_fails():
    prompt = "Woman holding a white Apple iPhone 15 near Starbucks cafe"
    passed, violations = validate_prompt_anti_plagiarism(prompt)
    assert passed is False
    assert any("apple" in v.lower() or "iphone" in v.lower() for v in violations)
    assert any("starbucks" in v.lower() for v in violations)


def test_validate_prohibited_copyrighted_character_fails():
    prompt = "A toddler playing with a Mickey Mouse and Spiderman toy in kindergarten"
    passed, violations = validate_prompt_anti_plagiarism(prompt)
    assert passed is False
    assert any("mickey mouse" in v.lower() for v in violations)
    assert any("spiderman" in v.lower() for v in violations)


def test_validate_artist_mimicry_fails():
    prompt = "Futuristic neon city at sunset in the style of Greg Rutkowski, trending on artstation"
    passed, violations = validate_prompt_anti_plagiarism(prompt)
    assert passed is False
    assert any("in the style of" in v.lower() or "artstation" in v.lower() for v in violations)


def test_sanitize_and_enforce_plagiarism_free_prompt():
    raw_prompt = "Young professional typing on a MacBook in the style of artgerm with Starbucks cup"
    sanitized = sanitize_and_enforce_plagiarism_free_prompt(raw_prompt)

    # Verifications
    assert "macbook" not in sanitized.lower()
    assert "sleek modern unbranded laptop" in sanitized.lower()
    assert "starbucks cup" not in sanitized.lower()
    assert "in the style of" not in sanitized.lower()
    assert "100% original composition" in sanitized
    assert "no trademarks" in sanitized
    assert "no logos" in sanitized


def test_compute_prompt_fingerprint_normalization():
    p1 = "Modern  Electric   Car, Charging at Sunset! "
    p2 = "modern electric car charging at sunset"
    assert compute_prompt_fingerprint(p1) == compute_prompt_fingerprint(p2)


def test_image_generation_rejects_exact_duplicate(tmp_path: Path, monkeypatch):
    """Verify that ImageGenerationService rejects duplicate image with DuplicateAssetError."""
    import collections
    monkeypatch.setattr(
        "shutil.disk_usage",
        lambda p: collections.namedtuple("usage", ["total", "used", "free"])(
            100 * 1024 * 1024 * 1024, 20 * 1024 * 1024 * 1024, 80 * 1024 * 1024 * 1024
        ),
    )
    db_path = tmp_path / "test_dedup.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    storage = StorageService(tmp_path / "data")

    # Fixed mock provider that returns identical image on repeated calls
    class FixedMockProvider(MockImageProvider):
        async def generate_image(self, *args, **kwargs):
            resp = await super().generate_image(*args, **kwargs)
            # Override with fixed bytes to simulate identical generated image
            from PIL import Image
            import io
            fixed_img = Image.new("RGB", (2048, 2048), color=(100, 150, 200))
            buf = io.BytesIO()
            fixed_img.save(buf, format="JPEG", quality=90)
            resp.image_bytes = buf.getvalue()
            return resp

    fixed_provider = FixedMockProvider()
    service = ImageGenerationService(
        session_factory=session_factory,
        storage_service=storage,
        image_provider=fixed_provider,
        enforce_deduplication=True,
    )

    # 1. First image generation succeeds
    asset1 = asyncio.run(service.generate_and_store_image("Unique prompt 1"))
    assert asset1.id is not None

    # 2. Second image generation with identical bytes MUST raise DuplicateAssetError
    with pytest.raises(DuplicateAssetError, match="duplikasi persis"):
        asyncio.run(service.generate_and_store_image("Unique prompt 2"))


def test_image_generation_rejects_perceptual_duplicate(tmp_path: Path, monkeypatch):
    """Verify that ImageGenerationService rejects perceptual near-duplicate image with DuplicateAssetError."""
    import collections
    import io
    from PIL import Image

    monkeypatch.setattr(
        "shutil.disk_usage",
        lambda p: collections.namedtuple("usage", ["total", "used", "free"])(
            100 * 1024 * 1024 * 1024, 20 * 1024 * 1024 * 1024, 80 * 1024 * 1024 * 1024
        ),
    )
    db_path = tmp_path / "test_perceptual_dedup.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    storage = StorageService(tmp_path / "data")

    class PerceptualNearDupProvider(MockImageProvider):
        def __init__(self):
            super().__init__()
            self.counter = 0

        async def generate_image(self, *args, **kwargs):
            self.counter += 1
            resp = await super().generate_image(*args, **kwargs)
            img = Image.new("RGB", (2830, 2830), color=(120, 150, 180))
            if self.counter == 2:
                # Tweak just one pixel: SHA-256 is different, but pHash is identical (Hamming distance 0 <= 5)
                img.putpixel((5, 5), (255, 0, 0))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=90)
            resp.image_bytes = buf.getvalue()
            return resp

    provider = PerceptualNearDupProvider()
    service = ImageGenerationService(
        session_factory=session_factory,
        storage_service=storage,
        image_provider=provider,
        enforce_deduplication=True,
        max_phash_distance=5,
    )

    # 1. First image succeeds
    asset1 = asyncio.run(service.generate_and_store_image("Scene 1"))
    assert asset1.id is not None

    # 2. Second image with different SHA-256 but identical pHash MUST raise DuplicateAssetError
    with pytest.raises(DuplicateAssetError, match="duplikasi perseptual"):
        asyncio.run(service.generate_and_store_image("Scene 2"))
