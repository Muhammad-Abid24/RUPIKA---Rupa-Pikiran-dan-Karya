"""Multi-provider role routing and compatible fallback resolution (SF-005)."""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
import yaml
from pydantic import BaseModel, Field

from stockforge.providers.base import RoleType
from stockforge.providers.registry import IncompatibleFallbackError, ProviderRegistry

logger = logging.getLogger("stockforge.providers.router")


class RoleRoute(BaseModel):
    """Routing specification for an individual AI role."""
    role: RoleType
    primary_model_id: str
    fallback_model_ids: List[str] = Field(default_factory=list)


def redact_sensitive_data(data: Any) -> Any:
    """Recursively scrub secrets, tokens, and authorization headers from payloads."""
    if isinstance(data, dict):
        sanitized = {}
        for k, v in data.items():
            k_lower = str(k).lower()
            if any(term in k_lower for term in ("key", "token", "secret", "auth", "password")):
                sanitized[k] = "***REDACTED***"
            else:
                sanitized[k] = redact_sensitive_data(v)
        return sanitized
    elif isinstance(data, list):
        return [redact_sensitive_data(item) for item in data]
    elif isinstance(data, str):
        # Scrub Bearer or sk- tokens if present in text
        masked = re.sub(r"(Bearer\s+)([\w\-\.]+)", r"\1***REDACTED***", data, flags=re.IGNORECASE)
        masked = re.sub(r"(sk-[a-zA-Z0-9_-]{8,})", r"***REDACTED***", masked)
        return masked
    return data


class ProviderRouter:
    """Routes generation and analysis roles to primary providers and validated fallbacks."""

    def __init__(self, registry: ProviderRegistry) -> None:
        self.registry = registry
        self._routes: Dict[RoleType, RoleRoute] = {}

    def set_route(
        self,
        role: RoleType,
        primary_model_id: str,
        fallbacks: Optional[List[str]] = None,
    ) -> None:
        """Configure route for a role, validating capability and license constraints."""
        # 1. Validate primary model
        self.registry.validate_capability_for_role(primary_model_id, role, is_fallback=False)

        # 2. Validate all fallback candidates
        validated_fallbacks: List[str] = []
        for fb_id in (fallbacks or []):
            self.registry.validate_capability_for_role(fb_id, role, is_fallback=True)
            validated_fallbacks.append(fb_id)

        self._routes[role] = RoleRoute(
            role=role,
            primary_model_id=primary_model_id,
            fallback_model_ids=validated_fallbacks,
        )
        logger.info(
            "Rute role '%s' berhasil dikonfigurasi: primary=%s, fallbacks=%s",
            role.value,
            primary_model_id,
            validated_fallbacks,
        )

    def get_route(self, role: RoleType) -> RoleRoute:
        """Retrieve routing configuration for a role."""
        if role not in self._routes:
            raise KeyError(f"Role '{role.value}' belum memiliki rute yang terkonfigurasi")
        return self._routes[role]

    def resolve_model(
        self,
        role: RoleType,
        fallback_attempt: int = 0,
    ) -> str:
        """Resolve model ID for a role based on attempt count.
        
        attempt 0 -> primary_model_id
        attempt 1 -> fallbacks[0]
        attempt 2 -> fallbacks[1], etc.
        """
        route = self.get_route(role)
        if fallback_attempt == 0:
            return route.primary_model_id

        idx = fallback_attempt - 1
        if idx < len(route.fallback_model_ids):
            return route.fallback_model_ids[idx]

        raise IncompatibleFallbackError(
            f"Kandidat fallback untuk role '{role.value}' telah habis (upaya fallback #{fallback_attempt})"
        )

    def load_from_yaml(
        self,
        yaml_path: Path,
        env_vars: Optional[Dict[str, str]] = None,
    ) -> None:
        """Load and bind provider models and roles from models.yaml configuration."""
        if not yaml_path.exists():
            raise FileNotFoundError(f"Konfigurasi models.yaml tidak ditemukan: {yaml_path}")

        env = env_vars if env_vars is not None else os.environ

        with open(yaml_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        providers_cfg = data.get("providers", {})
        roles_cfg = data.get("roles", {})

        # 1. Register providers from YAML
        for p_name, p_info in providers_cfg.items():
            base_url = env.get(p_info.get("base_url_env", ""), "")
            secret_env = p_info.get("api_key_env", "")
            kind_str = p_info.get("kind", "text")
            from stockforge.providers.base import ProviderKind
            kind = ProviderKind(kind_str)
            self.registry.register_provider(
                name=p_name,
                kind=kind,
                base_url=base_url,
                secret_env=secret_env,
            )

        # 2. Register and configure role routes
        for role_name, r_info in roles_cfg.items():
            role = RoleType(role_name)
            p_name = r_info.get("provider")
            model_env = r_info.get("model_env", "")
            model_id = env.get(model_env) or f"{p_name}-{role.value}-default"

            # Auto-register model with role-appropriate capabilities
            from stockforge.providers.base import ROLE_CAPABILITY_REQUIREMENTS, ProviderKind
            required_caps = set(ROLE_CAPABILITY_REQUIREMENTS.get(role, set()))
            p_kind = ProviderKind.IMAGE if role == RoleType.IMAGE else (
                ProviderKind.VISION if role in (RoleType.VISION_QC, RoleType.METADATA) else ProviderKind.TEXT
            )

            # Register if not already registered
            if not self.registry.get_model(model_id):
                self.registry.register_model(
                    model_id=model_id,
                    provider_name=p_name,
                    kind=p_kind,
                    capabilities=required_caps,
                    license_evidence="commercial_stock_sublicensable",
                    cost_per_unit_cents=10,
                )

            self.set_route(role=role, primary_model_id=model_id)
