"""Protocols and response models for multi-provider routing (SF-005)."""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional, Protocol, Set
from pydantic import BaseModel, Field


class ProviderKind(str, Enum):
    TEXT = "text"
    IMAGE = "image"
    VISION = "vision"


class RoleType(str, Enum):
    RESEARCH = "research"
    PROMPT = "prompt"
    IMAGE = "image"
    VISION_QC = "vision_qc"
    METADATA = "metadata"


class ProviderCapability(str, Enum):
    TEXT_GENERATION = "text_generation"
    IMAGE_GENERATION = "image_generation"
    VISION_INSPECTION = "vision_inspection"
    COMMERCIAL_LICENSE_VERIFIED = "commercial_license_verified"


ROLE_CAPABILITY_REQUIREMENTS: Dict[RoleType, Set[ProviderCapability]] = {
    RoleType.RESEARCH: {ProviderCapability.TEXT_GENERATION},
    RoleType.PROMPT: {ProviderCapability.TEXT_GENERATION},
    RoleType.IMAGE: {ProviderCapability.IMAGE_GENERATION, ProviderCapability.COMMERCIAL_LICENSE_VERIFIED},
    RoleType.VISION_QC: {ProviderCapability.VISION_INSPECTION},
    RoleType.METADATA: {ProviderCapability.VISION_INSPECTION},
}


class ProviderResponse(BaseModel):
    """Base response model recording request ID, usage, and cost with redacted secrets."""
    request_id: str
    provider: str
    model: str
    role: RoleType
    estimated_cost_cents: int = Field(default=0, ge=0)
    actual_cost_cents: int = Field(default=0, ge=0)
    raw_response_redacted: str = "{}"


class ProviderTextResponse(ProviderResponse):
    """Text completion/generation response."""
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0


class ProviderImageResponse(ProviderResponse):
    """Image generation response containing image bytes, dimensions, and format."""
    image_bytes: bytes
    width: int
    height: int
    format: str = "JPEG"
    mime_type: str = "image/jpeg"


class ProviderVisionResponse(ProviderResponse):
    """Vision QC or visual metadata generation response."""
    analysis: Dict[str, Any]
    passed: bool = True


class TextProvider(Protocol):
    """Protocol for text/LLM completion providers."""
    async def generate_text(
        self,
        prompt: str,
        model: str,
        max_tokens: int = 1000,
        temperature: float = 0.7,
        operation_id: Optional[str] = None,
        **kwargs: Any,
    ) -> ProviderTextResponse:
        ...


class ImageProvider(Protocol):
    """Protocol for image generation providers."""
    async def generate_image(
        self,
        prompt: str,
        model: str,
        width: int = 2048,
        height: int = 2048,
        operation_id: Optional[str] = None,
        **kwargs: Any,
    ) -> ProviderImageResponse:
        ...


class VisionProvider(Protocol):
    """Protocol for vision model inspection and metadata derivation."""
    async def analyze_image(
        self,
        image_bytes: bytes,
        prompt: str,
        model: str,
        operation_id: Optional[str] = None,
        **kwargs: Any,
    ) -> ProviderVisionResponse:
        ...
