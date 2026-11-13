"""Mock provider adapters for deterministic testing without external API calls (SF-005)."""
from __future__ import annotations

import uuid
from typing import Any, Dict, Optional

from stockforge.providers.base import (
    ImageProvider,
    ProviderImageResponse,
    ProviderTextResponse,
    ProviderVisionResponse,
    RoleType,
    TextProvider,
    VisionProvider,
)
from stockforge.providers.router import redact_sensitive_data


class MockTextProvider(TextProvider):
    """Deterministic mock provider for research and prompt roles."""

    def __init__(self, provider_name: str = "mock-text") -> None:
        self.provider_name = provider_name
        self.calls: list[Dict[str, Any]] = []

    async def generate_text(
        self,
        prompt: str,
        model: str,
        max_tokens: int = 1000,
        temperature: float = 0.7,
        operation_id: Optional[str] = None,
        **kwargs: Any,
    ) -> ProviderTextResponse:
        req_id = f"req-{uuid.uuid4().hex[:8]}"
        sanitized_kwargs = redact_sensitive_data(kwargs)
        self.calls.append({
            "request_id": req_id,
            "prompt": prompt,
            "model": model,
            "kwargs": sanitized_kwargs,
        })
        return ProviderTextResponse(
            request_id=req_id,
            provider=self.provider_name,
            model=model,
            role=RoleType.PROMPT,
            text=f"Mock response for prompt: {prompt[:30]}",
            prompt_tokens=25,
            completion_tokens=50,
            estimated_cost_cents=1,
            actual_cost_cents=1,
            raw_response_redacted=str(sanitized_kwargs),
        )


class MockImageProvider(ImageProvider):
    """Deterministic mock provider for image generation."""

    def __init__(self, provider_name: str = "mock-image") -> None:
        self.provider_name = provider_name
        self.calls: list[Dict[str, Any]] = []
        self.should_fail = False

    async def generate_image(
        self,
        prompt: str,
        model: str,
        width: int = 2048,
        height: int = 2048,
        operation_id: Optional[str] = None,
        **kwargs: Any,
    ) -> ProviderImageResponse:
        req_id = f"req-{uuid.uuid4().hex[:8]}"
        sanitized_kwargs = redact_sensitive_data(kwargs)
        self.calls.append({
            "request_id": req_id,
            "prompt": prompt,
            "model": model,
            "width": width,
            "height": height,
        })

        if self.should_fail:
            raise RuntimeError(f"Simulated failure from {self.provider_name}")

        # Return a valid decodable JPEG generated via Pillow
        import io
        from PIL import Image
        call_idx = len(self.calls)
        r = (60 + call_idx * 17) % 256
        g = (90 + call_idx * 23) % 256
        b = (120 + call_idx * 29) % 256
        img = Image.new("RGB", (width, height), color=(r, g, b))
        # Ensure distinct bytes by setting varied pixel based on prompt and call index
        img.putpixel((0, 0), (call_idx % 256, (call_idx * 7) % 256, (call_idx * 13) % 256))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        jpeg_bytes = buf.getvalue()

        return ProviderImageResponse(
            request_id=req_id,
            provider=self.provider_name,
            model=model,
            role=RoleType.IMAGE,
            image_bytes=jpeg_bytes,
            width=width,
            height=height,
            format="JPEG",
            mime_type="image/jpeg",
            estimated_cost_cents=5,
            actual_cost_cents=5,
            raw_response_redacted=str(sanitized_kwargs),
        )


class MockVisionProvider(VisionProvider):
    """Deterministic mock provider for vision QC and visual metadata generation."""

    def __init__(self, provider_name: str = "mock-vision") -> None:
        self.provider_name = provider_name
        self.calls: list[Dict[str, Any]] = []
        self.should_fail = False
        self.custom_analysis: Optional[Dict[str, Any]] = None
        self.custom_passed: Optional[bool] = None

    async def analyze_image(
        self,
        image_bytes: bytes,
        prompt: str,
        model: str,
        operation_id: Optional[str] = None,
        **kwargs: Any,
    ) -> ProviderVisionResponse:
        req_id = f"req-{uuid.uuid4().hex[:8]}"
        sanitized_kwargs = redact_sensitive_data(kwargs)
        role = kwargs.get("role", RoleType.VISION_QC)
        self.calls.append({
            "request_id": req_id,
            "model": model,
            "role": role,
            "prompt": prompt,
            "bytes_len": len(image_bytes),
        })

        if self.should_fail:
            raise RuntimeError(f"Simulated failure from {self.provider_name}")

        if self.custom_analysis is not None:
            analysis = self.custom_analysis
            passed = self.custom_passed if self.custom_passed is not None else True
        elif role == RoleType.METADATA:
            analysis = {
                "title": "Serene Mountain Lake at Sunrise",
                "keywords": [
                    "mountain", "lake", "sunrise", "nature",
                    "reflection", "serene", "landscape", "water"
                ],
                "category_id": 11,
                "content_type": "photo",
            }
            passed = True
        else:
            analysis = {
                "passed": True,
                "confidence": 0.95,
                "clarity": 0.95,
                "artifacts": False,
                "commercial_quality": True,
                "issues": [],
                "notes": "No visual defects detected",
            }
            passed = True

        return ProviderVisionResponse(
            request_id=req_id,
            provider=self.provider_name,
            model=model,
            role=role,
            analysis=analysis,
            passed=passed,
            estimated_cost_cents=2,
            actual_cost_cents=2,
            raw_response_redacted=str(sanitized_kwargs),
        )


