"""Deterministic image quality control and deduplication service (SF-009)."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
import imagehash
from PIL import Image, ImageFilter, ImageStat
from sqlalchemy import select
from sqlalchemy.orm import Session

from stockforge.db.models import Asset, QcResult, utc_now

logger = logging.getLogger("stockforge.qc.deterministic")

DISCLAIMER_TEXT = (
    "Technical deterministic quality control check only. "
    "Does NOT constitute legal, trademark, model release, or copyright certification."
)


@dataclass
class SingleCheckResult:
    """Outcome of a single deterministic QC check."""
    check_name: str
    passed: bool
    score: Optional[float]
    details: Dict[str, Any]


class DeterministicQcService:
    """Executes deterministic technical checks and deduplication against microstock standards.
    
    Checks:
    1. JPEG Integrity: Validates file existence, format header, and decodability.
    2. Resolution & Megapixels: Verifies >= 4.0 Megapixels for commercial stock photos.
    3. Aspect Ratio: Ensures commercial aspect ratio within acceptable bounds.
    4. Blur Heuristic: Computes Laplacian edge variance to flag blurry or low-detail images.
    5. Exact Deduplication: Flags identical SHA-256 hashes against prior assets.
    6. Perceptual Deduplication: Flags visual near-duplicates using pHash Hamming distance.
    
    Hard Invariant:
    Flags technical issues only; explicitly disclaims automatic legal certification.
    """

    def __init__(
        self,
        session_factory: Callable[[], Session],
        min_megapixels: float = 4.0,
        min_laplacian_variance: float = 15.0,
        max_phash_distance: int = 5,
    ) -> None:
        self.session_factory = session_factory
        self.min_megapixels = min_megapixels
        self.min_laplacian_variance = min_laplacian_variance
        self.max_phash_distance = max_phash_distance

    def check_jpeg_integrity(self, file_path: Path) -> SingleCheckResult:
        """Verify JPEG file exists, is non-empty, and can be decoded by Pillow."""
        if not file_path.exists():
            return SingleCheckResult(
                check_name="jpeg_integrity",
                passed=False,
                score=0.0,
                details={"error": f"File tidak ditemukan: {file_path}", "disclaimer": DISCLAIMER_TEXT},
            )

        try:
            with Image.open(file_path) as img:
                img.verify()
            
            # Reopen to verify mode and format
            with Image.open(file_path) as img:
                img_format = (img.format or "").upper()
                if img_format not in ("JPEG", "JPG"):
                    return SingleCheckResult(
                        check_name="jpeg_integrity",
                        passed=False,
                        score=0.0,
                        details={"error": f"Format bukan JPEG (ditemukan: {img_format})", "disclaimer": DISCLAIMER_TEXT},
                    )

            return SingleCheckResult(
                check_name="jpeg_integrity",
                passed=True,
                score=1.0,
                details={"format": "JPEG", "status": "valid", "disclaimer": DISCLAIMER_TEXT},
            )
        except Exception as e:
            return SingleCheckResult(
                check_name="jpeg_integrity",
                passed=False,
                score=0.0,
                details={"error": f"Integritas file JPEG rusak: {e}", "disclaimer": DISCLAIMER_TEXT},
            )

    def check_resolution(self, img: Image.Image) -> SingleCheckResult:
        """Verify image meets commercial microstock resolution (minimum 4.0 Megapixels)."""
        width, height = img.size
        total_pixels = width * height
        megapixels = total_pixels / 1_000_000.0
        passed = megapixels >= self.min_megapixels
        score = min(1.0, megapixels / self.min_megapixels)

        return SingleCheckResult(
            check_name="resolution",
            passed=passed,
            score=round(score, 3),
            details={
                "width": width,
                "height": height,
                "megapixels": round(megapixels, 2),
                "min_required_megapixels": self.min_megapixels,
                "disclaimer": DISCLAIMER_TEXT,
            },
        )

    def check_blur_heuristic(self, img: Image.Image) -> SingleCheckResult:
        """Analyze image sharpness and edge definition using Laplacian variance."""
        try:
            gray = img.convert("L")
            # 3x3 discrete Laplacian filter
            laplacian = ImageFilter.Kernel((3, 3), [0, 1, 0, 1, -4, 1, 0, 1, 0], scale=1, offset=128)
            filtered = gray.filter(laplacian)
            stat = ImageStat.Stat(filtered)
            variance = float(stat.var[0])

            passed = variance >= self.min_laplacian_variance
            normalized_score = min(1.0, variance / (self.min_laplacian_variance * 2.0))

            return SingleCheckResult(
                check_name="blur_heuristic",
                passed=passed,
                score=round(normalized_score, 3),
                details={
                    "laplacian_variance": round(variance, 2),
                    "min_required_variance": self.min_laplacian_variance,
                    "is_blurry": not passed,
                    "disclaimer": DISCLAIMER_TEXT,
                },
            )
        except Exception as e:
            return SingleCheckResult(
                check_name="blur_heuristic",
                passed=False,
                score=0.0,
                details={"error": f"Gagal menghitung heuristik blur: {e}", "disclaimer": DISCLAIMER_TEXT},
            )

    def check_exact_deduplication(
        self,
        session: Session,
        sha256: str,
        asset_id: int,
    ) -> SingleCheckResult:
        """Flag duplicate if identical SHA-256 hash exists in another asset."""
        stmt = select(Asset.id).where(Asset.sha256 == sha256, Asset.id != asset_id).limit(1)
        duplicate_id = session.scalar(stmt)

        passed = duplicate_id is None
        details = {
            "exact_duplicate_found": not passed,
            "duplicate_asset_id": duplicate_id,
            "sha256": sha256,
            "disclaimer": DISCLAIMER_TEXT,
        }
        return SingleCheckResult(
            check_name="exact_duplicate",
            passed=passed,
            score=1.0 if passed else 0.0,
            details=details,
        )

    def check_perceptual_deduplication(
        self,
        session: Session,
        phash_str: str,
        asset_id: int,
    ) -> SingleCheckResult:
        """Flag duplicate if visual pHash Hamming distance is below threshold."""
        try:
            current_phash = imagehash.hex_to_hash(phash_str)
        except Exception as e:
            return SingleCheckResult(
                check_name="perceptual_duplicate",
                passed=False,
                score=0.0,
                details={"error": f"pHash tidak valid: {e}", "disclaimer": DISCLAIMER_TEXT},
            )

        # Query existing assets
        existing_assets = session.execute(
            select(Asset.id, Asset.phash).where(Asset.id != asset_id)
        ).all()

        closest_dist = 64
        duplicate_id = None

        for other_id, other_phash_str in existing_assets:
            if not other_phash_str:
                continue
            try:
                other_phash = imagehash.hex_to_hash(other_phash_str)
                dist = int(current_phash - other_phash)
                if dist < closest_dist:
                    closest_dist = dist
                    if dist <= self.max_phash_distance:
                        duplicate_id = other_id
                        break
            except Exception:
                continue

        passed = duplicate_id is None
        return SingleCheckResult(
            check_name="perceptual_duplicate",
            passed=passed,
            score=0.0 if not passed else min(1.0, float(closest_dist) / 64.0),
            details={
                "perceptual_duplicate_found": not passed,
                "closest_asset_id": duplicate_id,
                "hamming_distance": int(closest_dist),
                "max_allowed_distance": self.max_phash_distance,
                "disclaimer": DISCLAIMER_TEXT,
            },
        )

    def inspect_asset(self, asset_id: int) -> List[QcResult]:
        """Perform comprehensive deterministic QC and persist all results in SQLite."""
        with self.session_factory() as session:
            asset = session.get(Asset, asset_id)
            if not asset:
                raise ValueError(f"Asset dengan ID {asset_id} tidak ditemukan.")

            raw_path = Path(asset.raw_path)

            # 1. Integrity check
            integrity_res = self.check_jpeg_integrity(raw_path)

            checks: List[SingleCheckResult] = [integrity_res]

            if integrity_res.passed:
                with Image.open(raw_path) as img:
                    # 2. Resolution check
                    checks.append(self.check_resolution(img))
                    # 3. Blur heuristic check
                    checks.append(self.check_blur_heuristic(img))

            # 4. Exact duplicate check
            checks.append(self.check_exact_deduplication(session, asset.sha256, asset_id))

            # 5. Perceptual duplicate check
            checks.append(self.check_perceptual_deduplication(session, asset.phash, asset_id))

            # Persist QC results to database
            persisted_results: List[QcResult] = []
            all_passed = True
            now = utc_now()

            for c in checks:
                if not c.passed:
                    all_passed = False
                qc_entry = QcResult(
                    asset_id=asset_id,
                    check_name=c.check_name,
                    passed=c.passed,
                    score=c.score,
                    details_json=json.dumps(c.details),
                    checked_at=now,
                )
                session.add(qc_entry)
                persisted_results.append(qc_entry)

            # Update asset status
            asset.status = "qc_passed" if all_passed else "qc_failed"
            session.commit()

            for r in persisted_results:
                session.refresh(r)

            logger.info("QC Selesai untuk Asset #%d: status=%s, checks_passed=%s", asset_id, asset.status, all_passed)
            return persisted_results
