"""Base adapter protocol and sanitization helpers for provider errors (SF-017)."""
from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple
from stockforge.errors.codes import (
    ERROR_CODE_DEFAULTS,
    ErrorCode,
    NormalizedProviderError,
)


def sanitize_sensitive_details(text: Optional[str]) -> str:
    """Redact API keys, tokens, authorization headers, and secrets from error strings."""
    if not text:
        return ""
    s = str(text)
    # Mask Bearer tokens
    s = re.sub(r"(Bearer\s+)([A-Za-z0-9_\-\.]+)", r"\1***REDACTED***", s, flags=re.IGNORECASE)
    # Mask OpenAI-like and generic secret keys
    s = re.sub(r"(sk-[a-zA-Z0-9_\-]{8,})", r"***REDACTED***", s)
    s = re.sub(r"([kK]ey|[sS]ecret|[tT]oken|[pP]assword)=([^\s&]+)", r"\1=***REDACTED***", s)
    # Mask authorization headers
    s = re.sub(r"(Authorization:\s*)([^\r\n]+)", r"\1***REDACTED***", s, flags=re.IGNORECASE)
    return s


def parse_retry_after(headers: Optional[Dict[str, str]]) -> Optional[float]:
    """Extract retry-after header value in seconds if present."""
    if not headers:
        return None
    for k, v in headers.items():
        if k.lower() == "retry-after":
            try:
                val = float(v.strip())
                return val if val >= 0 else None
            except (ValueError, TypeError):
                return None
    return None


def extract_request_id(headers: Optional[Dict[str, str]], fallback: Optional[str] = None) -> Optional[str]:
    """Extract request ID from common headers (x-request-id, request-id, etc.)."""
    if headers:
        for k, v in headers.items():
            if k.lower() in ("x-request-id", "request-id", "x-amzn-requestid", "cf-ray"):
                return sanitize_sensitive_details(v.strip())
    return sanitize_sensitive_details(fallback) if fallback else None


class BaseProviderErrorAdapter:
    """Base adapter class for provider error normalization."""

    provider_name: str = "generic"

    def can_handle(self, provider: Optional[str]) -> bool:
        if not provider:
            return False
        return provider.lower() == self.provider_name.lower()

    def classify(
        self,
        http_status: Optional[int],
        headers: Optional[Dict[str, str]],
        body: Optional[Any],
        request_id: Optional[str] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> NormalizedProviderError:
        raise NotImplementedError

