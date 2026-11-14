"""Export packaging service for Adobe Stock bundles (SF-012)."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from sqlalchemy import select
from sqlalchemy.orm import Session

from stockforge.config import get_settings
from stockforge.db.models import Asset, ExportBatch, ExportItem, utc_now
from stockforge.export.adobe import (
    AdobeCsvRow,
    ComplianceExportError,
    generate_adobe_csv,
    sanitize_adobe_filename,
    validate_asset_compliance,
)
from stockforge.storage.service import StorageService

logger = logging.getLogger("stockforge.export.service")


class ExportSecurityError(Exception):
    """Raised when an archive operation poses a security risk (e.g. Zip-Slip, path traversal)."""
    pass


def safe_add_file_to_zip(
    zf: zipfile.ZipFile,
    source_path: Path,
    arcname: str,
) -> None:
    """Add a file to a zip archive while strictly enforcing anti-Zip-Slip validation.
    
    Guarantees:
    - Arcname cannot be empty.
    - Arcname cannot be an absolute path or contain directory traversal ('..').
    - Arcname must be a single flat basename without subdirectories.
    """
    if not arcname or not arcname.strip():
        raise ExportSecurityError("Nama entri arsip ZIP tidak boleh kosong")

    arc_path = Path(arcname)
    if arc_path.is_absolute() or ".." in arc_path.parts or "/" in arcname or "\\" in arcname:
        raise ExportSecurityError(
            f"Kerentanan Zip-Slip terdeteksi! Akses path ilegal di dalam ZIP diblokir: '{arcname}'"
        )

    if arc_path.name != arcname:
        raise ExportSecurityError(
            f"Entri arsip ZIP harus berupa nama file datar (flat basename): '{arcname}'"
        )

    zf.write(source_path, arcname=arcname)


def safe_add_bytes_to_zip(
    zf: zipfile.ZipFile,
    data: bytes,
    arcname: str,
) -> None:
    """Write in-memory bytes into a zip archive with anti-Zip-Slip validation."""
    if not arcname or not arcname.strip():
        raise ExportSecurityError("Nama entri arsip ZIP tidak boleh kosong")

    arc_path = Path(arcname)
    if arc_path.is_absolute() or ".." in arc_path.parts or "/" in arcname or "\\" in arcname:
        raise ExportSecurityError(
            f"Kerentanan Zip-Slip terdeteksi! Akses path ilegal di dalam ZIP diblokir: '{arcname}'"
        )

    zf.writestr(arcname, data)


class ExportService:
    """Manages compliance validation, CSV generation, ZIP packaging, and batch tracking."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        storage_service: StorageService,
        max_zip_size_bytes: int = 500 * 1024 * 1024,  # 500 MB zip limit
    ) -> None:
        self.session_factory = session_factory
        self.storage_service = storage_service
        self.max_zip_size_bytes = max_zip_size_bytes

    def export_approved_assets(
        self,
        asset_ids: Optional[List[int]] = None,
        created_by_user_id: Optional[int] = None,
    ) -> Tuple[ExportBatch, Path]:
        """Export approved and compliant assets into a signed ZIP bundle and record in DB.
        
        Guarantees:
        - Rejects unapproved or unlicensed assets (raises ComplianceExportError).
        - Enforces official Adobe Stock CSV headers, <=30 char filenames, <=70 char titles, 40-50 keywords.
        - Packs JPEG files, adobe_metadata.csv, and manifest.json into a ZIP archive safely.
        - Defends against Zip-Slip vulnerabilities.
        - Transitions asset.status to 'exported'.
        - Never attempts auto-submission (manual upload invariant).
        """
        settings = get_settings()
        if settings.allow_adobe_auto_submission:
            raise RuntimeError(
                "Invarian keamanan dilanggar: ALLOW_ADOBE_AUTO_SUBMISSION bernilai True. "
                "Pengunggahan ke Adobe Stock harus selalu dilakukan secara manual!"
            )

        with self.session_factory() as session:
            # 1. Query eligible assets
            if asset_ids:
                stmt = select(Asset).where(Asset.id.in_(asset_ids)).order_by(Asset.id.asc())
            else:
                stmt = select(Asset).where(Asset.status == "approved").order_by(Asset.id.asc())

            assets = list(session.scalars(stmt).all())
            if not assets:
                raise ValueError("Tidak ada aset yang berstatus 'approved' untuk diekspor")

            # Eagerly access relationships
            for a in assets:
                _ = a.metadata_entry

            # 2. Comprehensive compliance verification
            all_errors: Dict[int, List[str]] = {}
            for a in assets:
                is_valid, errors = validate_asset_compliance(a)
                if not is_valid:
                    all_errors[a.id] = errors

            if all_errors:
                error_summary = "; ".join(
                    f"Asset #{aid}: {', '.join(errs)}" for aid, errs in all_errors.items()
                )
                raise ComplianceExportError(
                    f"Ekspor dibatalkan karena aset gagal verifikasi kepatuhan lisensi/review: {error_summary}"
                )

            # 3. Prepare export items and Adobe CSV records
            csv_rows: List[AdobeCsvRow] = []
            manifest_items: List[Dict[str, Any]] = []
            export_mappings: List[Tuple[Asset, Path, str]] = []

            for a in assets:
                safe_name = sanitize_adobe_filename(a.filename, a.id, a.sha256)
                meta = a.metadata_entry
                assert meta is not None

                kw_list = json.loads(meta.keywords_json)
                # Form comma-separated keyword string (up to 50 keywords)
                keywords_str = ", ".join(kw_list[:50])

                csv_rows.append(
                    AdobeCsvRow(
                        filename=safe_name,
                        title=meta.title,
                        keywords=keywords_str,
                        category=meta.category_id or 1,
                    )
                )

                source_path = Path(a.raw_path)
                export_mappings.append((a, source_path, safe_name))

                manifest_items.append({
                    "asset_id": a.id,
                    "export_filename": safe_name,
                    "original_filename": a.filename,
                    "sha256": a.sha256,
                    "phash": a.phash,
                    "dimensions": f"{a.width}x{a.height}",
                    "ai_generated": a.ai_generated,
                    "generative_ai_disclosure": "required",
                    "license_verified": a.license_verified,
                    "license_evidence": a.license_evidence,
                    "title": meta.title,
                    "category_id": meta.category_id,
                    "keywords_count": len(kw_list),
                    "human_reviewed_at": meta.metadata_reviewed_at.isoformat() if meta.metadata_reviewed_at else None,
                })

            # 4. Generate CSV string and Manifest JSON
            csv_content = generate_adobe_csv(csv_rows)
            now = utc_now()
            manifest = {
                "manifest_version": "1.0",
                "export_tool": "RUPIKA Export Service",
                "exported_at": now.isoformat(),
                "created_by_user_id": created_by_user_id,
                "total_assets": len(assets),
                "generative_ai_disclosure": "Generative AI disclosure is strictly required upon submission to Adobe Stock",
                "submission_mode": "manual",
                "assets": manifest_items,
            }
            manifest_json_str = json.dumps(manifest, indent=2)

            # 5. Build ZIP archive safely
            export_dir = self.storage_service.base_data_dir / "exports"
            export_dir.mkdir(parents=True, exist_ok=True)

            timestamp_str = now.strftime("%Y%m%d_%H%M%S")
            zip_filename = f"rupika_export_{timestamp_str}_{len(assets)}items.zip"
            zip_target_path = export_dir / zip_filename
            temp_zip_path = export_dir / f".{zip_filename}.tmp"

            try:
                with zipfile.ZipFile(temp_zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                    # Add JPEGs
                    for _, src_path, safe_name in export_mappings:
                        safe_add_file_to_zip(zf, src_path, arcname=safe_name)

                    # Add CSV
                    safe_add_bytes_to_zip(zf, csv_content.encode("utf-8"), arcname="adobe_metadata.csv")

                    # Add Manifest
                    safe_add_bytes_to_zip(zf, manifest_json_str.encode("utf-8"), arcname="manifest.json")

                # Validate archive size
                zip_size = temp_zip_path.stat().st_size
                if zip_size > self.max_zip_size_bytes:
                    raise ExportSecurityError(
                        f"Ukuran archive ZIP ({zip_size} bytes) melebihi batas aman ({self.max_zip_size_bytes} bytes)"
                    )

                # Atomically rename
                os.replace(temp_zip_path, zip_target_path)
            except Exception as e:
                if temp_zip_path.exists():
                    try:
                        temp_zip_path.unlink()
                    except OSError:
                        pass
                logger.error("Gagal membuat arsip ekspor: %s", e)
                raise

            # 6. Compute ZIP archive checksum
            zip_sha256 = hashlib.sha256(zip_target_path.read_bytes()).hexdigest()
            manifest["archive_sha256"] = zip_sha256

            # 7. Record in Database
            batch = ExportBatch(
                created_by=created_by_user_id,
                archive_path=str(zip_target_path),
                manifest_json=json.dumps(manifest),
                created_at=now,
            )
            session.add(batch)
            session.flush()

            for a in assets:
                item = ExportItem(
                    batch_id=batch.id,
                    asset_id=a.id,
                )
                session.add(item)
                a.status = "exported"

            session.commit()
            session.refresh(batch)

            logger.info(
                "Batch ekspor #%d berhasil dibuat: %d aset dipaketkan di '%s' (SHA-256: %s)",
                batch.id,
                len(assets),
                zip_target_path.name,
                zip_sha256,
            )
            return batch, zip_target_path

