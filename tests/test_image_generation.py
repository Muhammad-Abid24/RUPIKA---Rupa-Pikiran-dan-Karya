"""Acceptance tests for SF-007 Image generation adapter, storage, and asset persistence."""
from __future__ import annotations

import asyncio
import io
from pathlib import Path
import pytest
from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from stockforge.db.models import Asset, Base
from stockforge.images.service import (
    BatchGenerationResult,
    BatchLimitExceededError,
    ImageGenerationService,
    UnverifiedCommercialLicenseError,
)
from stockforge.providers.base import (
    ProviderCapability,
    ProviderKind,
    RoleType,
)
from stockforge.providers.mock import MockImageProvider
from stockforge.providers.registry import ProviderRegistry
from stockforge.providers.router import ProviderRouter
from stockforge.storage.service import (
    CorruptedImageError,
    SecurityStorageError,
    StorageService,
)


@pytest.fixture
def test_env(tmp_path: Path):
    """Setup isolated database and storage directory."""
    db_path = tmp_path / "test_images.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    data_dir = tmp_path / "data"
    storage = StorageService(data_dir)

    return session_factory, storage, data_dir


def test_storage_atomic_write_and_checksum(test_env):
    """Verify atomic write, SHA-256, pHash, format, and dimension inspection."""
    _, storage, data_dir = test_env

    # Create real valid test JPEG image using Pillow
    img = Image.new("RGB", (800, 600), color=(120, 150, 180))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    jpeg_bytes = buf.getvalue()

    dest_path, sha256_hash, phash_str, width, height = storage.atomic_write(
        data=jpeg_bytes,
        filename="test_photo.jpg",
        category="raw",
    )

    assert dest_path.exists()
    assert dest_path.name == "test_photo.jpg"
    assert width == 800
    assert height == 600
    assert len(sha256_hash) == 64
    assert len(phash_str) > 0

    # Ensure no lingering .tmp files
    raw_dir = data_dir / "raw"
    tmp_files = list(raw_dir.glob(".*.tmp"))
    assert len(tmp_files) == 0, "File sementara (.tmp) harus dibersihkan setelah penulisan atomik"


def test_storage_prevents_path_traversal(test_env):
    """Verify that path traversal attempts are strictly prevented."""
    _, storage, _ = test_env

    traversal_filenames = [
        "../../etc/passwd",
        "../../../malicious.jpg",
        "/etc/shadow",
        "subdir/../../root.jpg",
        "..",
        ".",
    ]

    for bad_name in traversal_filenames:
        with pytest.raises(SecurityStorageError):
            storage.resolve_safe_path(bad_name, category="raw")


def test_storage_detects_corrupted_image(test_env):
    """Verify that non-image or corrupted bytes are rejected with CorruptedImageError."""
    _, storage, _ = test_env

    corrupted_bytes = b"NOT_AN_IMAGE_RANDOM_CORRUPT_BYTES_1234567890"
    with pytest.raises(CorruptedImageError):
        storage.atomic_write(corrupted_bytes, "corrupted.jpg", category="raw")


def test_mock_provider_generates_1_to_10_files_successfully(test_env):
    """Verify acceptance criterion: mocked provider generates 1–10 files successfully."""
    session_factory, storage, _ = test_env
    mock_provider = MockImageProvider(provider_name="mock-image")

    service = ImageGenerationService(
        session_factory=session_factory,
        storage_service=storage,
        image_provider=mock_provider,
        license_evidence="stock_commercial_license_verified",
    )

    prompts = [f"Commercial stock photo concept #{i}" for i in range(1, 11)]  # 10 prompts
    result = asyncio.run(service.generate_batch(prompts=prompts, max_batch_size=10))

    assert result.total_count == 10
    assert result.success_count == 10
    assert result.failure_count == 0

    # Verify assets in database
    with session_factory() as session:
        assets = list(session.scalars(select(Asset)).all())
        assert len(assets) == 10
        for a in assets:
            assert a.status == "generated"
            assert a.ai_generated is True
            assert a.license_verified is True
            assert a.license_evidence == "stock_commercial_license_verified"
            assert Path(a.raw_path).exists()


def test_batch_cap_enforced(test_env):
    """Verify that requesting more than max_batch_size is rejected."""
    session_factory, storage, _ = test_env
    mock_provider = MockImageProvider(provider_name="mock-image")
    service = ImageGenerationService(
        session_factory=session_factory,
        storage_service=storage,
        image_provider=mock_provider,
    )

    prompts = [f"Prompt #{i}" for i in range(11)]  # 11 prompts > max 10
    with pytest.raises(BatchLimitExceededError):
        asyncio.run(service.generate_batch(prompts=prompts, max_batch_size=10))


def test_provider_failure_does_not_corrupt_asset_table(test_env):
    """Verify acceptance criterion: failure during generation does not corrupt the asset table."""
    session_factory, storage, _ = test_env
    mock_provider = MockImageProvider(provider_name="mock-image")

    service = ImageGenerationService(
        session_factory=session_factory,
        storage_service=storage,
        image_provider=mock_provider,
    )

    # 1. Successful generation of first item
    asyncio.run(service.generate_and_store_image("Valid first prompt"))

    # 2. Simulate provider failure on second item
    mock_provider.should_fail = True
    with pytest.raises(RuntimeError, match="Simulated failure"):
        asyncio.run(service.generate_and_store_image("Failing prompt"))

    # 3. Verify asset table integrity: contains exactly 1 valid record, no corrupt entries
    with session_factory() as session:
        assets = list(session.scalars(select(Asset)).all())
        assert len(assets) == 1
        assert assets[0].status == "generated"
        assert Path(assets[0].raw_path).exists()


def test_commercial_license_verification_enforcement(test_env):
    """Verify that unverified or missing commercial sublicensing rights fail-closed."""
    session_factory, storage, _ = test_env

    registry = ProviderRegistry()
    registry.register_provider("mock_img", ProviderKind.IMAGE, "https://mock.img", "KEY")

    # Model WITHOUT commercial license capability
    registry.register_model(
        model_id="unlicensed-model",
        provider_name="mock_img",
        kind=ProviderKind.IMAGE,
        capabilities={ProviderCapability.IMAGE_GENERATION},  # Missing COMMERCIAL_LICENSE_VERIFIED
        license_evidence="",
    )

    # Valid model with verified license
    registry.register_model(
        model_id="licensed-model",
        provider_name="mock_img",
        kind=ProviderKind.IMAGE,
        capabilities={ProviderCapability.IMAGE_GENERATION, ProviderCapability.COMMERCIAL_LICENSE_VERIFIED},
        license_evidence="commercial_stock_sublicensable_v1",
    )

    router = ProviderRouter(registry)
    mock_provider = MockImageProvider(provider_name="mock_img")
    registry._provider_instances = {"mock_img": mock_provider}

    # Test unlicensed model rejection
    router.set_route(RoleType.IMAGE, "licensed-model")  # router validates capabilities
    service = ImageGenerationService(
        session_factory=session_factory,
        storage_service=storage,
        router=router,
    )

    # Directly verifying unlicensed model throws UnverifiedCommercialLicenseError
    with pytest.raises(UnverifiedCommercialLicenseError):
        service.verify_commercial_license("unlicensed-model")

    # Verifying licensed model succeeds
    evidence = service.verify_commercial_license("licensed-model")
    assert evidence == "commercial_stock_sublicensable_v1"

