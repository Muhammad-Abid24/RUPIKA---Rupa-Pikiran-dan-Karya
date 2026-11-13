"""Anthropic error adapter for error classification (SF-017)."""
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


class AnthropicErrorAdapter(BaseProviderErrorAdapter):
    provider_name: str = "anthropic"

    def can_handle(self, provider: Optional[str]) -> bool:
        if not provider:
            return False
        p = provider.lower()
        return p in ("anthropic", "claude")

    def classify(
        self,
        http_status: Optional[int],
        headers: Optional[Dict[str, str]],
        body: Optional[Any],
        request_id: Optional[str] = None,
        provider: Optional[str] = "anthropic",
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

        err_type = str(err_dict.get("type") or "")
        raw_msg = str(err_dict.get("message") or "")
        low_msg = raw_msg.lower()

        raw_code_redacted = sanitize_sensitive_details(err_type or (f"HTTP_{http_status}" if http_status else None))

        # Explicit authentication / invalid key
        if http_status == 401 or err_type == "authentication_error" or "api key" in low_msg:
            code = ErrorCode.INVALID_API_KEY
            user_msg = "Anthropic API key is invalid or revoked."
        # Insufficient credits
        elif "credit balance is too low" in low_msg or "plans & billing" in low_msg or "insufficient credits" in low_msg:
            code = ErrorCode.INSUFFICIENT_CREDITS
            user_msg = "Anthropic account has insufficient prepaid credits."
        # Overloaded / unavailable
        elif err_type == "overloaded_error" or http_status == 529:
            code = ErrorCode.MODEL_UNAVAILABLE
            user_msg = "Anthropic API is temporarily overloaded; backing off."
        # Rate limit
        elif http_status == 429 or err_type == "rate_limit_error":
            code = ErrorCode.RATE_LIMITED
            user_msg = "Anthropic rate limit reached; backoff in progress."
        # Not found
        elif http_status == 404 or err_type == "not_found_error":
            code = ErrorCode.MODEL_NOT_FOUND
            user_msg = f"Requested Anthropic model '{model}' was not found."
        # Content policy
        elif "content filtering" in low_msg or "moderation" in low_msg or "policy" in low_msg:
            code = ErrorCode.CONTENT_REJECTED
            user_msg = "Request rejected by Anthropic content policy."
        # Server error
        elif http_status and http_status >= 500:
            if http_status == 504:
                code = ErrorCode.REQUEST_TIMEOUT
                user_msg = "Anthropic request timed out."
            else:
                code = ErrorCode.PROVIDER_SERVER_ERROR
                user_msg = f"Anthropic server error (HTTP {http_status})."
        # Invalid request
        elif http_status == 400 or err_type == "invalid_request_error":
            code = ErrorCode.INVALID_REQUEST
            user_msg = f"Invalid request to Anthropic: {sanitize_sensitive_details(raw_msg)[:150]}"
        elif http_status == 408:
            code = ErrorCode.REQUEST_TIMEOUT
            user_msg = "Request to Anthropic timed out."
        else:
            code = ErrorCode.UNKNOWN_PROVIDER_ERROR
            user_msg = f"Unknown Anthropic error: {sanitize_sensitive_details(raw_msg)[:150]}"

        defaults = ERROR_CODE_DEFAULTS.get(code, ERROR_CODE_DEFAULTS[ErrorCode.UNKNOWN_PROVIDER_ERROR])

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
            details={"raw_type": sanitize_sensitive_details(err_type)},
        )
