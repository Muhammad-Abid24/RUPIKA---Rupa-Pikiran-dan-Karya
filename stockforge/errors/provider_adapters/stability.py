"""Stability AI and image generation adapter for error classification (SF-017)."""
from __future__ import annotations

import json
from typing import Any, Dict, Optional
from stockforge.errors.codes import (
    ERROR_CODE_DEFAULTS,
    ErrorCode,
    NormalizedProviderError,
)
from stockforge.errors.provider_adapters.base import (
    BaseProviderErrorAdapter,
    extract_request_id,
    parse_retry_after,
    sanitize_sensitive_details,
)


class StabilityErrorAdapter(BaseProviderErrorAdapter):
    provider_name: str = "stability"

    def can_handle(self, provider: Optional[str]) -> bool:
        if not provider:
            return False
        p = provider.lower()
        return p in ("stability", "stabilityai", "fal", "fal_ai", "replicate", "midjourney")

    def classify(
        self,
        http_status: Optional[int],
        headers: Optional[Dict[str, str]],
        body: Optional[Any],
        request_id: Optional[str] = None,
        provider: Optional[str] = "stability",
        model: Optional[str] = None,
    ) -> NormalizedProviderError:
        req_id = extract_request_id(headers, request_id)
        retry_after = parse_retry_after(headers)

        err_dict: Dict[str, Any] = {}
        if isinstance(body, dict):
            err_dict = body.get("error", body) if isinstance(body.get("error"), dict) else body
        elif isinstance(body, str):
            try:
                parsed = json.loads(body)
                if isinstance(parsed, dict):
                    err_dict = parsed.get("error", parsed) if isinstance(parsed.get("error"), dict) else parsed
            except Exception:
                err_dict = {"message": body}

        raw_name = str(err_dict.get("name") or err_dict.get("code") or "")
        raw_msg = str(err_dict.get("message") or "")
        low_msg = raw_msg.lower()

        raw_code_redacted = sanitize_sensitive_details(raw_name or (f"HTTP_{http_status}" if http_status else None))

        # Insufficient credits / payment required (402 or explicit message)
        if http_status == 402 or raw_name == "payment_required" or "insufficient credits" in low_msg or "not enough balance" in low_msg or "credits" in low_msg and "exhausted" in low_msg:
            code = ErrorCode.INSUFFICIENT_CREDITS
            user_msg = f"{provider} balance exhausted or payment required."
        # Invalid API key
        elif http_status in (401, 403) and ("unauthorized" in low_msg or "invalid api key" in low_msg or "forbidden" in low_msg):
            code = ErrorCode.INVALID_API_KEY
            user_msg = f"{provider} API key is unauthorized or invalid."
        # Content rejection (NSFW, moderation filter)
        elif raw_name in ("content_moderation", "nsfw_detected") or "moderation" in low_msg or "nsfw" in low_msg or "sensitive content" in low_msg:
            code = ErrorCode.CONTENT_REJECTED
            user_msg = f"Image generation prompt refused by {provider} content filter."
        # Rate limit
        elif http_status == 429 or raw_name == "rate_limit_exceeded":
            code = ErrorCode.RATE_LIMITED
            user_msg = f"{provider} rate limit exceeded; backoff in progress."
        # Model not found
        elif http_status == 404 or raw_name == "model_not_found":
            code = ErrorCode.MODEL_NOT_FOUND
            user_msg = f"Requested {provider} model '{model}' not found."
        # Model unavailable
        elif http_status in (503, 529) or "overloaded" in low_msg:
            code = ErrorCode.MODEL_UNAVAILABLE
            user_msg = f"{provider} is temporarily unavailable or overloaded."
        # Server error
        elif http_status and http_status >= 500:
            if http_status == 504:
                code = ErrorCode.REQUEST_TIMEOUT
                user_msg = f"{provider} gateway timed out."
            else:
                code = ErrorCode.PROVIDER_SERVER_ERROR
                user_msg = f"{provider} server error (HTTP {http_status})."
        # Invalid request / dimensions
        elif http_status == 400 or raw_name == "invalid_parameters":
            code = ErrorCode.INVALID_REQUEST
            user_msg = f"Invalid request parameters to {provider}: {sanitize_sensitive_details(raw_msg)[:150]}"
        elif http_status == 408:
            code = ErrorCode.REQUEST_TIMEOUT
            user_msg = f"Request to {provider} timed out."
        else:
            code = ErrorCode.UNKNOWN_PROVIDER_ERROR
            user_msg = f"Unknown {provider} error: {sanitize_sensitive_details(raw_msg)[:150]}"

        defaults = ERROR_CODE_DEFAULTS.get(code) or ERROR_CODE_DEFAULTS[ErrorCode.UNKNOWN_PROVIDER_ERROR]

        return NormalizedProviderError(
            code=code,
            retryable=defaults["retryable"],
            fallback_allowed=defaults["fallback_allowed"],
            severity=defaults["severity"],
            action=defaults["action"],
            retry_after_seconds=retry_after,
            safe_user_message=user_msg,
            raw_code_redacted=raw_code_redacted,
            http_status=http_status,
            request_id=req_id,
            provider=provider,
            model=model,
            details={"raw_name": sanitize_sensitive_details(raw_name)},
        )

