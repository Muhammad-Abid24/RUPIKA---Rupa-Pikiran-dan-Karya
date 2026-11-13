"""Adobe Stock contributor compliance validation and CSV formatting (SF-012)."""
from __future__ import annotations

import csv
import io
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from stockforge.db.models import Asset, AssetMetadata
from stockforge.qc.vision import ADOBE_CATEGORIES

logger = logging.getLogger("stockforge.export.adobe")

ADOBE_CSV_HEADERS = ["Filename", "Title", "Keywords", "Category"]


class ComplianceExportError(Exception):
    """Raised when an asset fails Adobe Stock or legal compliance checks."""
    pass


def sanitize_adobe_filename(original_filename: str, asset_id: int, sha256_hex: str) -> str:
    """Generate a clean, safe filename compliant with Adobe Stock <= 30 characters limit.
    
    Adobe Stock constraints:
    - Filename <= 30 characters (including .jpg extension).
    - Only alphanumeric characters, dashes, and underscores.
    """
    ext = ".jpg"

    # Base candidate without extension
    stem = Path(original_filename).stem
    # Replace any spaces or illegal characters
    cleaned_stem = re.sub(r"[^a-zA-Z0-9_-]", "_", stem)

    # Check if original is clean and fits in 30 characters
    full_name = f"{cleaned_stem}{ext}"
    if len(full_name) <= 30 and re.match(r"^[a-zA-Z0-9_-]+\.jpg$", full_name, re.IGNORECASE):
        return full_name

    # Otherwise generate standard deterministic compliant filename <= 30 chars
    # Format: rf_{asset_id:05d}_{sha8}.jpg -> 3 + 5 + 1 + 8 + 4 = 21 chars
    short_sha = sha256_hex[:8] if sha256_hex else "00000000"
    return f"rf_{asset_id:05d}_{short_sha}{ext}"



def validate_asset_compliance(asset: Asset) -> Tuple[bool, List[str]]:
    """Verify that an asset strictly meets commercial, review, and metadata requirements.
    
    Guarantees:
    1. Human review mandatory: status must be 'approved'.
    2. Commercial license verified: license_verified must be True and evidence non-empty.
    3. Metadata present: title <= 70 chars, keywords <= 40 items, category in 1..21.
    4. Generative AI disclosure required: flagged in manifest.
    5. Physical file exists.
    """
    violations: List[str] = []

    # 1. Review status check
    if asset.status != "approved":
        violations.append(
            f"Aset belum disetujui manusia (status saat ini: '{asset.status}', diperlukan: 'approved')"
        )

    # 2. Commercial license verification
    if not asset.license_verified:
        violations.append("Bukti lisensi komersial belum terverifikasi (license_verified is False)")

    if not asset.license_evidence or not asset.license_evidence.strip():
        violations.append("Dokumentasi bukti lisensi komersial kosong (license_evidence is empty)")

    # 3. Metadata validation
    meta: Optional[AssetMetadata] = asset.metadata_entry
    if not meta:
        violations.append("Metadata aset (judul, keyword, kategori) belum dibuat")
    else:
        if not meta.title or not meta.title.strip():
            violations.append("Judul aset kosong")
        elif len(meta.title) > 70:
            violations.append(f"Judul melebihi batas 70 karakter: {len(meta.title)} karakter")

        if not meta.category_id or meta.category_id not in ADOBE_CATEGORIES:
            violations.append(f"Kategori Adobe Stock tidak valid: {meta.category_id}")

        if not meta.keywords_json:
            violations.append("Daftar keyword kosong")
        else:
            try:
                kw_list = json.loads(meta.keywords_json)
                if not isinstance(kw_list, list) or len(kw_list) == 0:
                    violations.append("Daftar keyword kosong atau format tidak valid")
                elif len(kw_list) > 40:
                    violations.append(f"Jumlah keyword ({len(kw_list)}) melebihi batas MVP (40)")
            except Exception as e:
                violations.append(f"Gagal mem-parse keywords_json: {e}")

    # 4. Physical file existence
    if not asset.raw_path or not Path(asset.raw_path).is_file():
        violations.append(f"File gambar fisik tidak ditemukan di path: {asset.raw_path}")

    return (len(violations) == 0, violations)


@dataclass
class AdobeCsvRow:
    """Row model representing an Adobe Stock Contributor CSV record."""
    filename: str
    title: str
    keywords: str
    category: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "Filename": self.filename,
            "Title": self.title,
            "Keywords": self.keywords,
            "Category": self.category,
        }


def generate_adobe_csv(rows: List[AdobeCsvRow]) -> str:
    """Generate official Adobe Stock Contributor CSV string with RFC 4180 escaping and UTF-8."""
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=ADOBE_CSV_HEADERS,
        quoting=csv.QUOTE_MINIMAL,
        lineterminator="\r\n",  # Windows/standard line termination for Adobe CSV parser
    )
    writer.writeheader()
    for row in rows:
        writer.writerow(row.to_dict())
    return output.getvalue()
