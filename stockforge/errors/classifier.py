"""Centralized error classification engine (SF-017)."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from stockforge.errors.codes import (
    ErrorCode,
    NormalizedProviderError,
)
from stockforge.errors.provider_adapters.anthropic import AnthropicErrorAdapter
from stockforge.errors.provider_adapters.base import (
    BaseProviderErrorAdapter,
    sanitize_sensitive_details,
)
from stockforge.errors.provider_adapters.gemini import GeminiErrorAdapter
from stockforge.errors.provider_adapters.generic import GenericErrorAdapter
from stockforge.errors.provider_adapters.openai import OpenAIErrorAdapter
from stockforge.errors.provider_adapters.stability import StabilityErrorAdapter

logger = logging.getLogger("stockforge.errors.classifier")


class ErrorClassifier:
    """Classifies provider and transport errors into normalized domain errors."""

    def __init__(self, adapters: Optional[List[BaseProviderErrorAdapter]] = None) -> None:
        self.adapters = adapters or [
            OpenAIErrorAdapter(),
            AnthropicErrorAdapter(),
            GeminiErrorAdapter(),
            StabilityErrorAdapter(),
            GenericErrorAdapter(),  # Fallback must be last
        ]

    def get_adapter(self, provider: Optional[str]) -> BaseProviderErrorAdapter:
        if provider:
            for adapter in self.adapters:
                if adapter.can_handle(provider):
                    return adapter
        return self.adapters[-1]  # Generic fallback

    def classify(
        self,
        http_status: Optional[int] = None,
        headers: Optional[Dict[str, str]] = None,
        body: Optional[Any] = None,
        request_id: Optional[str] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        exception: Optional[Exception] = None,
    ) -> NormalizedProviderError:
        """Classify HTTP status, response body, headers, or Python exception into NormalizedProviderError."""
        status = http_status
        content_body = body
        req_headers = headers

        # Extract information from common exception types if provided
        if exception is not None:
            # Check for HTTP status attributes
            if status is None:
                status = getattr(exception, "status_code", None) or getattr(exception, "status", None)
            
            # Check for headers
            if req_headers is None and hasattr(exception, "headers") and isinstance(exception.headers, dict):
                req_headers = dict(exception.headers)

            # Check for response body or message
            if content_body is None:
                if hasattr(exception, "response") and hasattr(exception.response, "text"):
                    content_body = exception.response.text
                elif hasattr(exception, "body"):
                    content_body = exception.body
                else:
                    content_body = str(exception)

        adapter = self.get_adapter(provider)
        normalized = adapter.classify(
            http_status=status,
            headers=req_headers,
            body=content_body,
            request_id=request_id,
            provider=provider or "generic",
            model=model,
        )

        logger.debug(
            "Classified error: provider=%s model=%s status=%s code=%s action=%s",
            normalized.provider,
            normalized.model,
            normalized.http_status,
            normalized.code.value,
            normalized.action,
        )
        return normalized


# Global singleton instance
default_classifier = ErrorClassifier()


def classify_error(
    http_status: Optional[int] = None,
    headers: Optional[Dict[str, str]] = None,
    body: Optional[Any] = None,
    request_id: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    exception: Optional[Exception] = None,
) -> NormalizedProviderError:
    """Convenience functional wrapper for error classification."""
    return default_classifier.classify(
        http_status=http_status,
        headers=headers,
        body=body,
        request_id=request_id,
        provider=provider,
        model=model,
        exception=exception,
    )
