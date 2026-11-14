"""Provider registry and capability validation (SF-005)."""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Set
from pydantic import BaseModel, Field

from stockforge.providers.base import (
    ProviderCapability,
    ProviderKind,
    ROLE_CAPABILITY_REQUIREMENTS,
    RoleType,
)

logger = logging.getLogger("stockforge.providers.registry")


class IncompatibleProviderError(ValueError):
    """Raised when a model lacks the necessary capabilities or licensing for a role."""
    pass


class IncompatibleFallbackError(IncompatibleProviderError):
    """Raised when a fallback candidate cannot fulfill the required role capabilities."""
    pass


class ModelRegistration(BaseModel):
    """Registration entry for an AI model."""
    model_id: str
    provider_name: str
    kind: ProviderKind
    capabilities: Set[ProviderCapability] = Field(default_factory=set)
    license_evidence: str = ""
    cost_per_unit_cents: int = 0
    enabled: bool = True


class ProviderRegistration(BaseModel):
    """Registration entry for an AI provider."""
    name: str
    kind: ProviderKind
    base_url: str
    secret_env: str
    enabled: bool = True


class ProviderRegistry:
    """Central registry of AI providers and models with strict capability validation."""

    def __init__(self) -> None:
        self._providers: Dict[str, ProviderRegistration] = {}
        self._models: Dict[str, ModelRegistration] = {}
        self._provider_instances: Dict[str, Any] = {}

    def register_provider_instance(self, name: str, instance: Any) -> None:
        """Register an initialized client instance for a provider."""
        self._provider_instances[name] = instance

    def get_provider_instance(self, name: str) -> Optional[Any]:
        """Retrieve initialized client instance for a provider."""
        return self._provider_instances.get(name)

    def get_provider_for_model(self, model_id: str) -> Optional[Any]:
        """Retrieve initialized provider instance corresponding to a model ID."""
        model = self.get_model(model_id)
        if not model:
            return None
        return self.get_provider_instance(model.provider_name)

    def register_provider(
        self,
        name: str,
        kind: ProviderKind,
        base_url: str,
        secret_env: str,
        enabled: bool = True,
    ) -> None:
        """Register an AI provider."""
        self._providers[name] = ProviderRegistration(
            name=name,
            kind=kind,
            base_url=base_url,
            secret_env=secret_env,
            enabled=enabled,
        )

    def register_model(
        self,
        model_id: str,
        provider_name: str,
        kind: ProviderKind,
        capabilities: Set[ProviderCapability],
        license_evidence: str = "",
        cost_per_unit_cents: int = 0,
        enabled: bool = True,
    ) -> None:
        """Register a model under a provider with verified capabilities and licensing."""
        if provider_name not in self._providers:
            raise KeyError(f"Provider '{provider_name}' belum terdaftar")

        self._models[model_id] = ModelRegistration(
            model_id=model_id,
            provider_name=provider_name,
            kind=kind,
            capabilities=capabilities,
            license_evidence=license_evidence,
            cost_per_unit_cents=cost_per_unit_cents,
            enabled=enabled,
        )

    def get_model(self, model_id: str) -> Optional[ModelRegistration]:
        return self._models.get(model_id)

    def validate_capability_for_role(
        self,
        model_id: str,
        role: RoleType,
        is_fallback: bool = False,
    ) -> None:
        """Enforce capability and license requirements for a role.
        
        Rules:
        - Image role requires IMAGE_GENERATION and COMMERCIAL_LICENSE_VERIFIED.
        - Text roles (research, prompt) require TEXT_GENERATION.
        - Vision roles (vision_qc, metadata) require VISION_INSPECTION.
        - Never assume a chat/text model can generate images!
        """
        model = self.get_model(model_id)
        if not model:
            err_cls = IncompatibleFallbackError if is_fallback else IncompatibleProviderError
            raise err_cls(f"Model '{model_id}' tidak ditemukan dalam registri")

        if not model.enabled:
            err_cls = IncompatibleFallbackError if is_fallback else IncompatibleProviderError
            raise err_cls(f"Model '{model_id}' sedang dinonaktifkan")

        required_caps = ROLE_CAPABILITY_REQUIREMENTS.get(role, set())
        missing_caps = required_caps - model.capabilities

        if missing_caps:
            err_cls = IncompatibleFallbackError if is_fallback else IncompatibleProviderError
            missing_names = ", ".join(c.value for c in missing_caps)
            raise err_cls(
                f"Model '{model_id}' tidak kompatibel untuk role '{role.value}'. "
                f"Kapabilitas yang kurang: [{missing_names}]"
            )

