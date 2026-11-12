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

        # Return minimal valid JPEG header bytes
        dummy_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00\xff\xdb"
        return ProviderImageResponse(
            request_id=req_id,
            provider=self.provider_name,
            model=model,
            role=RoleType.IMAGE,
            image_bytes=dummy_jpeg,
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
        self.calls.append({
            "request_id": req_id,
            "model": model,
            "bytes_len": len(image_bytes),
        })
        return ProviderVisionResponse(
            request_id=req_id,
            provider=self.provider_name,
            model=model,
            role=RoleType.VISION_QC,
            analysis={"clarity": 0.95, "artifacts": False, "commercial_quality": True},
            passed=True,
            estimated_cost_cents=2,
            actual_cost_cents=2,
            raw_response_redacted=str(sanitized_kwargs),
        )

