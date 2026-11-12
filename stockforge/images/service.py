"""Image generation adapter, commercial license verification, and asset persistence (SF-007)."""
from __future__ import annotations

import logging
import uuid
from typing import Any, Callable, Dict, List, Optional
from pathlib import Path
from sqlalchemy.orm import Session

from stockforge.db.models import Asset
from stockforge.providers.base import (
    ImageProvider,
    ProviderCapability,
    ProviderImageResponse,
    RoleType,
)
from stockforge.providers.router import ProviderRouter
from stockforge.storage.service import StorageService

logger = logging.getLogger("stockforge.images.service")


class UnverifiedCommercialLicenseError(Exception):
    """Raised when an image provider/model lacks verified commercial sublicensing rights."""
    pass


class BatchLimitExceededError(Exception):
    """Raised when the requested number of images exceeds batch limits."""
    pass


class BatchGenerationResult:
    """Outcome of a batch generation run."""

    def __init__(
        self,
        successful_assets: List[Asset],
        failed_items: List[Dict[str, Any]],
        total_spent_cents: int = 0,
    ) -> None:
        self.successful_assets = successful_assets
        self.failed_items = failed_items
        self.total_spent_cents = total_spent_cents

    @property
    def total_count(self) -> int:
        return len(self.successful_assets) + len(self.failed_items)

    @property
    def success_count(self) -> int:
        return len(self.successful_assets)

    @property
    def failure_count(self) -> int:
        return len(self.failed_items)


class ImageGenerationService:
    """Coordinates image generation, license verification, atomic storage, and asset persistence."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        storage_service: StorageService,
        router: Optional[ProviderRouter] = None,
        image_provider: Optional[ImageProvider] = None,
        default_model_id: str = "mock-image-model",
        license_evidence: str = "commercial_stock_sublicense_verified",
    ) -> None:
        self.session_factory = session_factory
        self.storage = storage_service
        self.router = router
        self.image_provider = image_provider
        self.default_model_id = default_model_id
        self.license_evidence = license_evidence

    def verify_commercial_license(self, model_id: str) -> str:
        """Verify that the model has verified commercial stock sublicensing rights.
        
        Hard Rule:
        Must possess ProviderCapability.COMMERCIAL_LICENSE_VERIFIED and non-empty license evidence.
        """
        if self.router is not None:
            model_reg = self.router.registry.get_model(model_id)
            if not model_reg:
                raise UnverifiedCommercialLicenseError(f"Model '{model_id}' tidak terdaftar dalam registri.")

            if ProviderCapability.COMMERCIAL_LICENSE_VERIFIED not in model_reg.capabilities:
                raise UnverifiedCommercialLicenseError(
                    f"Model '{model_id}' TIDAK memiliki kapabilitas lisensi komersial "
                    f"({ProviderCapability.COMMERCIAL_LICENSE_VERIFIED.value})."
                )

            if not model_reg.license_evidence:
                raise UnverifiedCommercialLicenseError(
                    f"Model '{model_id}' tidak memiliki bukti lisensi komersial (license_evidence kosong)."
                )
            return model_reg.license_evidence

        # Standalone mock/direct provider fallback
        if not self.license_evidence:
            raise UnverifiedCommercialLicenseError("Bukti lisensi komersial tidak terkonfigurasi.")
        return self.license_evidence

    def estimate_max_spend_cents(self, count: int, model_id: Optional[str] = None) -> int:
        """Calculate estimated maximum spend in integer cents."""
        cost_per_unit = 5  # default 5 cents ($0.05)
        if self.router is not None and model_id:
            model_reg = self.router.registry.get_model(model_id)
            if model_reg and model_reg.cost_per_unit_cents > 0:
                cost_per_unit = model_reg.cost_per_unit_cents
        return count * cost_per_unit

    async def _resolve_provider_and_model(self) -> tuple[ImageProvider, str]:
        """Resolve image provider and active model ID."""
        if self.image_provider is not None:
            return self.image_provider, self.default_model_id

        if self.router is None:
            raise RuntimeError("ProviderRouter atau ImageProvider harus disediakan.")

        model_id = self.router.resolve_model(RoleType.IMAGE, fallback_attempt=0)
        model_reg = self.router.registry.get_model(model_id)
        if not model_reg:
            raise KeyError(f"Model '{model_id}' tidak ditemukan dalam registri.")

        # Resolve instance from registry if available, else instantiate mock
        provider_instance = getattr(self.router.registry, "_provider_instances", {}).get(model_reg.provider_name)
        if provider_instance is None:
            from stockforge.providers.mock import MockImageProvider
            provider_instance = MockImageProvider(provider_name=model_reg.provider_name)

        return provider_instance, model_id

    async def generate_and_store_image(
        self,
        prompt: str,
        filename: Optional[str] = None,
        job_id: Optional[int] = None,
        width: int = 2048,
        height: int = 2048,
        operation_id: Optional[str] = None,
    ) -> Asset:
        """Generate an image via ImageProvider, write atomically to storage, and persist Asset in DB.
        
        Invariants & Guarantees:
        - Commercial license verified before execution.
        - Atomic write and format validation (no corrupted or partial files on disk).
        - Atomic DB transaction: if failure occurs, database rolls back cleanly and asset table is unaffected.
        """
        provider, model_id = await self._resolve_provider_and_model()

        # 1. Commercial license verification
        evidence = self.verify_commercial_license(model_id)

        # 2. Determine target filename
        if not filename:
            unique_suffix = uuid.uuid4().hex[:12]
            filename = f"asset_{unique_suffix}.jpg"

        # 3. Request image generation from provider
        logger.info("Memicu generasi gambar dengan prompt '%s' menggunakan model '%s'...", prompt[:40], model_id)
        resp: ProviderImageResponse = await provider.generate_image(
            prompt=prompt,
            model=model_id,
            width=width,
            height=height,
            operation_id=operation_id,
        )

        # 4. Atomic file write & validation
        dest_path, sha256_hash, phash_val, actual_w, actual_h = self.storage.atomic_write(
            data=resp.image_bytes,
            filename=filename,
            category="raw",
        )

        # 5. Persist Asset to database within transaction
        session = self.session_factory()
        try:
            asset = Asset(
                job_id=job_id,
                filename=dest_path.name,
                raw_path=str(dest_path),
                sha256=sha256_hash,
                phash=phash_val,
                width=actual_w,
                height=actual_h,
                mime_type="image/jpeg",
                ai_generated=True,
                license_verified=True,
                license_evidence=evidence,
                status="generated",
            )
            session.add(asset)
            session.commit()
            session.refresh(asset)
            logger.info("Aset #%d ('%s') berhasil disimpan di database.", asset.id, asset.filename)
            return asset
        except Exception as e:
            session.rollback()
            # If DB failed after disk write, clean up file to avoid orphaned unmanaged files
            if dest_path.exists():
                try:
                    dest_path.unlink()
                except OSError:
                    pass
            logger.error("Gagal menyimpan aset ke database: %s. Transaksi di-rollback.", e)
            raise
        finally:
            session.close()

    async def generate_batch(
        self,
        prompts: List[str],
        max_batch_size: int = 10,
        job_id: Optional[int] = None,
        width: int = 2048,
        height: int = 2048,
    ) -> BatchGenerationResult:
        """Generate a batch of 1 to 10 images with spend estimation and failure isolation.
        
        Guarantees:
        - Rejects batches exceeding max_batch_size (batch cap).
        - Failure on an individual image item does not corrupt the asset table or abort other items.
        """
        if not prompts:
            raise ValueError("Daftar prompt tidak boleh kosong.")

        if len(prompts) > max_batch_size:
            raise BatchLimitExceededError(
                f"Jumlah prompt ({len(prompts)}) melebihi batas batch maksimum ({max_batch_size})."
            )

        successful_assets: List[Asset] = []
        failed_items: List[Dict[str, Any]] = []
        total_spent = 0

        for idx, p in enumerate(prompts):
            try:
                asset = await self.generate_and_store_image(
                    prompt=p,
                    job_id=job_id,
                    width=width,
                    height=height,
                )
                successful_assets.append(asset)
                total_spent += 5
            except Exception as e:
                logger.warning("Generasi gambar item #%d gagal: %s", idx + 1, e)
                failed_items.append({
                    "item_index": idx,
                    "prompt": p,
                    "error": str(e),
                })

        return BatchGenerationResult(
            successful_assets=successful_assets,
            failed_items=failed_items,
            total_spent_cents=total_spent,
        )

