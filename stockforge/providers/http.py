"""HTTP provider adapters for OpenAI-compatible REST endpoints (SF-005)."""
from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Optional
import httpx

from stockforge.errors.classifier import ErrorClassifier
from stockforge.providers.base import (
    ProviderCapability,
    ProviderKind,
    ProviderTextResponse,
    RoleType,
    TextProvider,
)
from stockforge.providers.router import redact_sensitive_data

logger = logging.getLogger("stockforge.providers.http")


class HttpTextProvider(TextProvider):
    """OpenAI-compatible text generation provider using httpx."""

    def __init__(
        self,
        provider_name: str,
        base_url: str,
        api_key: str,
        timeout: float = 45.0,
    ) -> None:
        self.provider_name = provider_name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    async def generate_text(
        self,
        prompt: str,
        model: str,
        max_tokens: int = 1500,
        temperature: float = 0.7,
        operation_id: Optional[str] = None,
        **kwargs: Any,
    ) -> ProviderTextResponse:
        req_id = f"req-{uuid.uuid4().hex[:8]}"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }

        sanitized_payload = redact_sensitive_data(payload)
        logger.info("Calling provider %s at %s with model %s", self.provider_name, self.base_url, model)

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                resp = await client.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                )
            except Exception as e:
                logger.error("Connection error to %s: %s", self.provider_name, e)
                raise RuntimeError(f"Connection error to {self.provider_name}: {e}") from e

        if resp.status_code != 200:
            logger.error("Provider %s error HTTP %s: %s", self.provider_name, resp.status_code, resp.text[:200])
            classifier = ErrorClassifier()
            norm = classifier.classify(
                http_status=resp.status_code,
                headers=dict(resp.headers),
                body=resp.text,
                provider=self.provider_name,
                model=model,
            )
            raise RuntimeError(f"Provider {self.provider_name} failed: [{norm.code.value}] {norm.clean_message}")

        data = resp.json()
        choice = data.get("choices", [{}])[0]
        msg = choice.get("message", {})
        text = msg.get("content", "")

        usage = data.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)

        return ProviderTextResponse(
            request_id=req_id,
            provider=self.provider_name,
            model=model,
            role=RoleType.RESEARCH,
            text=text,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            estimated_cost_cents=1,
            actual_cost_cents=1,
            raw_response_redacted=str(sanitized_payload),
        )
