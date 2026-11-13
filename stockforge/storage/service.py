"""File storage service with atomic writes, checksumming, and path traversal prevention (SF-007)."""
from __future__ import annotations

import hashlib
import io
import logging
import os
import uuid
from pathlib import Path
from typing import Tuple

import imagehash
from PIL import Image

logger = logging.getLogger("stockforge.storage.service")


class SecurityStorageError(Exception):
    """Raised when an illegal path traversal or unauthorized filesystem access is attempted."""
    pass


class CorruptedImageError(Exception):
    """Raised when image bytes cannot be decoded, are corrupted, or fail format validation."""
    pass


class StorageService:
    """Manages file storage operations with strict path sandboxing and atomic writes."""

    def __init__(self, base_data_dir: Path) -> None:
        self.base_data_dir = base_data_dir.resolve()
        self.base_data_dir.mkdir(parents=True, exist_ok=True)

    def resolve_safe_path(self, filename: str, category: str = "raw") -> Path:
        """Resolve destination path inside category directory while strictly preventing path traversal.
        
        Rules:
        - Prevents directory traversal (e.g. '../../etc/passwd').
        - Disallows path separators in filenames or absolute paths.
        - Strict sandbox containment within category directory.
        """
        # Strict validation: reject any path traversal patterns or path separators
        if not filename or filename in (".", ".."):
            raise SecurityStorageError(f"Nama file tidak valid: '{filename}'")

        if ".." in filename or "/" in filename or "\\" in filename or Path(filename).is_absolute():
            raise SecurityStorageError(
                f"Percobaan Path Traversal atau karakter path ilegal terdeteksi: '{filename}'"
            )

        # Ensure category is a clean relative subfolder
        clean_category = category.strip("/\\")
        if ".." in clean_category:
            raise SecurityStorageError(f"Kategori tidak valid: '{category}'")

        category_dir = (self.base_data_dir / clean_category).resolve()
        category_dir.mkdir(parents=True, exist_ok=True)

        target_path = (category_dir / filename).resolve()

        # Strict sandbox check: must be strictly within category_dir
        try:
            target_path.relative_to(category_dir)
        except ValueError:
            raise SecurityStorageError(
                f"Percobaan Path Traversal terdeteksi! Akses di luar direktori diblokir: '{filename}'"
            )

        return target_path

    def inspect_and_validate_image(self, data: bytes) -> Tuple[str, int, int, str]:
        """Inspect image bytes using Pillow and compute perceptual hash (pHash).
        
        Returns:
            (format, width, height, phash_str)
        Raises:
            CorruptedImageError if bytes are not a valid decodable image.
        """
        if not data or len(data) < 32:
            raise CorruptedImageError("Ukuran payload gambar terlalu kecil atau kosong.")

        try:
            buf = io.BytesIO(data)
            with Image.open(buf) as img:
                img.verify()

            # Re-open for dimension extraction and perceptual hashing (since verify closes/invalidates the stream)
            buf.seek(0)
            with Image.open(buf) as img:
                img_format = (img.format or "JPEG").upper()
                width, height = img.size
                phash_val = str(imagehash.phash(img))

            return img_format, width, height, phash_val
        except Exception as e:
            logger.error(f"Gagal memvalidasi gambar: {e}")
            raise CorruptedImageError(f"File gambar rusak atau format tidak didukung: {e}") from e

    def atomic_write(
        self,
        data: bytes,
        filename: str,
        category: str = "raw",
    ) -> Tuple[Path, str, str, int, int]:
        """Atomically validate, checksum, and save image data to disk.
        
        Guarantees:
        - Validates format and integrity with Pillow before writing.
        - Writes to a temporary file (.tmp) in the same destination directory.
        - Calculates SHA-256 and perceptual hash (pHash).
        - Atomically replaces target file using `os.replace`.
        - Cleans up temporary file upon any exception.
        
        Returns:
            (destination_path, sha256_hex, phash_str, width, height)
        """
        # 1. Validate format & dimensions
        img_format, width, height, phash_val = self.inspect_and_validate_image(data)

        # 2. Resolve safe path (prevents traversal)
        destination_path = self.resolve_safe_path(filename, category=category)

        # 3. Compute SHA-256
        sha256_hash = hashlib.sha256(data).hexdigest()

        # 4. Atomic write via temp file in same directory
        temp_filename = f".{destination_path.name}.{uuid.uuid4().hex[:8]}.tmp"
        temp_path = destination_path.parent / temp_filename

        try:
            with open(temp_path, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())

            # Atomic move / replace
            os.replace(temp_path, destination_path)
            logger.info("File gambar berhasil disimpan secara atomik di: %s", destination_path)
            return destination_path, sha256_hash, phash_val, width, height
        except Exception as e:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass
            logger.error("Gagal melakukan penulisan atomik untuk %s: %e", filename, e)
            raise

    def read_bytes(self, filename: str, category: str = "raw") -> bytes:
        """Safely read binary content from the category directory."""
        path = self.resolve_safe_path(filename, category=category)
        if not path.is_file():
            raise FileNotFoundError(f"File tidak ditemukan di storage: {path}")
        with open(path, "rb") as f:
            return f.read()


