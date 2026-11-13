"""High-quality resolution scaling for commercial stock assets."""
from __future__ import annotations

import io
import math
import logging
from PIL import Image

logger = logging.getLogger("stockforge.images.scaling")


def scale_image_bytes_to_target(
    image_bytes: bytes,
    target_megapixels: float = 8.0,
    min_megapixels: float = 6.0,
) -> tuple[bytes, int, int]:
    """Ensure image meets or exceeds target megapixels using high-quality Lanczos resampling.

    If the image already meets or exceeds target_megapixels, it is returned unmodified.
    If it is below target_megapixels, it is scaled up using PIL Image.Resampling.LANCZOS,
    maintaining the exact aspect ratio, such that (new_w * new_h) / 1_000_000 >= target_megapixels.
    Both width and height are rounded to even numbers for clean JPEG macroblock alignment.

    Returns:
        tuple[bytes, int, int]: (output_jpeg_bytes, output_width, output_height)
    """
    with Image.open(io.BytesIO(image_bytes)) as img:
        orig_w, orig_h = img.size
        current_pixels = orig_w * orig_h
        target_pixels = int(target_megapixels * 1_000_000)

        if current_pixels >= target_pixels:
            return image_bytes, orig_w, orig_h

        # Calculate scale factor
        scale = math.sqrt(target_pixels / current_pixels)
        new_w = int(math.ceil(orig_w * scale))
        new_h = int(math.ceil(orig_h * scale))

        # Align to even dimensions
        if new_w % 2 != 0:
            new_w += 1
        if new_h % 2 != 0:
            new_h += 1

        while (new_w * new_h) < target_pixels:
            new_w += 2
            new_h += 2

        logger.info(
            "Upscaling image from %dx%d (%.2f MP) to %dx%d (%.2f MP) to meet target %s MP",
            orig_w,
            orig_h,
            current_pixels / 1_000_000.0,
            new_w,
            new_h,
            (new_w * new_h) / 1_000_000.0,
            target_megapixels,
        )

        if img.mode != "RGB":
            converted = img.convert("RGB")
        else:
            converted = img

        resized = converted.resize((new_w, new_h), resample=Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        resized.save(buf, format="JPEG", quality=95, optimize=True)
        return buf.getvalue(), new_w, new_h

