"""Unit tests for stockforge.images.scaling."""
from __future__ import annotations

import io
from PIL import Image
import pytest

from stockforge.images.scaling import scale_image_bytes_to_target


def _create_jpeg(width: int, height: int) -> bytes:
    img = Image.new("RGB", (width, height), color=(100, 150, 200))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def test_scale_image_upscales_when_below_target():
    """An image smaller than target 8.0 MP should be scaled up using Lanczos."""
    # 2048 x 2048 = 4.19 MP (< 8.0 MP)
    raw_bytes = _create_jpeg(2048, 2048)
    scaled_bytes, w, h = scale_image_bytes_to_target(raw_bytes, target_megapixels=8.0, min_megapixels=6.0)

    assert w >= 2828
    assert h >= 2828
    assert (w * h) >= 8_000_000
    assert w % 2 == 0
    assert h % 2 == 0

    # Ensure output is a valid JPEG decodable by Pillow
    with Image.open(io.BytesIO(scaled_bytes)) as img:
        assert img.format == "JPEG"
        assert img.size == (w, h)


def test_scale_image_preserves_aspect_ratio_landscape():
    """Landscape image (3:2) should maintain its aspect ratio when upscaled."""
    # 1920 x 1280 = 2.46 MP (aspect ratio 1.5)
    raw_bytes = _create_jpeg(1920, 1280)
    scaled_bytes, w, h = scale_image_bytes_to_target(raw_bytes, target_megapixels=8.0, min_megapixels=6.0)

    assert (w * h) >= 8_000_000
    ratio = w / h
    assert abs(ratio - 1.5) < 0.01  # Maintained ~1.5 (3:2)


def test_scale_image_unmodified_when_already_exceeding_target():
    """Image meeting or exceeding target megapixels should remain untouched."""
    # 3500 x 2500 = 8.75 MP (> 8.0 MP)
    raw_bytes = _create_jpeg(3500, 2500)
    out_bytes, w, h = scale_image_bytes_to_target(raw_bytes, target_megapixels=8.0, min_megapixels=6.0)

    assert out_bytes == raw_bytes
    assert w == 3500
    assert h == 2500

