"""OpenAI error adapter for error classification (SF-017)."""
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


class OpenAIErrorAdapter(BaseProviderErrorAdapter):
    provider_name: str = "openai"

    def can_handle(self, provider: Optional[str]) -> bool:
        if not provider:
            return False
        p = provider.lower()
        return p in ("openai", "dall-e", "dalle", "chatgpt")

    def classify(
        self,
        http_status: Optional[int],
        headers: Optional[Dict[str, str]],
        body: Optional[Any],
        request_id: Optional[str] = None,
        provider: Optional[str] = "openai",
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

        raw_code = str(err_dict.get("code") or "")
        err_type = str(err_dict.get("type") or "")
        raw_msg = str(err_dict.get("message") or "")
        low_msg = raw_msg.lower()

        raw_code_redacted = sanitize_sensitive_details(raw_code or err_type or (f"HTTP_{http_status}" if http_status else None))

        # Check explicit insufficient quota / credits
        if raw_code == "insufficient_quota" or err_type == "insufficient_quota" or "exceeded your current quota" in low_msg or "billing details" in low_msg:
            code = ErrorCode.INSUFFICIENT_CREDITS
            user_msg = "OpenAI account has insufficient funds or quota exceeded. Please check billing."
        # Explicit authentication / invalid key
        elif http_status == 401 or raw_code == "invalid_api_key" or "incorrect api key" in low_msg or "invalid api key" in low_msg:
            code = ErrorCode.INVALID_API_KEY
            user_msg = "OpenAI API key is invalid or revoked."
        # Content policy refusal
        elif raw_code == "content_policy_violation" or "safety system" in low_msg or "content moderation" in low_msg:
            code = ErrorCode.CONTENT_REJECTED
            user_msg = "Image generation prompt refused by OpenAI safety / content policy."
        # Model not found
        elif http_status == 404 or raw_code == "model_not_found" or "does not exist" in low_msg:
            code = ErrorCode.MODEL_NOT_FOUND
            user_msg = f"Requested OpenAI model '{model}' does not exist or is deprecated."
        # Rate limit vs quota: generic 429 must be RATE_LIMITED, not token exhaustion
        elif http_status == 429 or raw_code == "rate_limit_exceeded" or err_type == "rate_limit_error":
            if "tokens per min" in low_msg or "tpm" in low_msg:
                code = ErrorCode.RATE_LIMITED
                user_msg = "OpenAI token rate limit (TPM) reached; backoff in progress."
            elif "requests per min" in low_msg or "rpm" in low_msg:
                code = ErrorCode.RATE_LIMITED
                user_msg = "OpenAI request rate limit (RPM) reached; backoff in progress."
            else:
                code = ErrorCode.RATE_LIMITED
                user_msg = "OpenAI rate limit exceeded; backoff and retry scheduled."
        # Concurrency limit
        elif "concurrent" in low_msg or raw_code == "concurrency_limit_exceeded":
            code = ErrorCode.CONCURRENCY_LIMIT
            user_msg = "OpenAI concurrency limit reached; queueing request."
        # Server errors (5xx)
        elif http_status and http_status >= 500:
            if http_status in (503, 529) or "overloaded" in low_msg:
                code = ErrorCode.MODEL_UNAVAILABLE
                user_msg = "OpenAI service temporarily overloaded or unavailable."
            elif http_status == 504:
                code = ErrorCode.REQUEST_TIMEOUT
                user_msg = "OpenAI gateway timed out."
            else:
                code = ErrorCode.PROVIDER_SERVER_ERROR
                user_msg = f"OpenAI server error (HTTP {http_status})."
        # Invalid request / client error (400)
        elif http_status == 400 or err_type == "invalid_request_error":
            code = ErrorCode.INVALID_REQUEST
            user_msg = f"Invalid request to OpenAI: {sanitize_sensitive_details(raw_msg)[:150]}"
        # Timeout
        elif http_status == 408:
            code = ErrorCode.REQUEST_TIMEOUT
            user_msg = "Request to OpenAI timed out."
        else:
            code = ErrorCode.UNKNOWN_PROVIDER_ERROR
            user_msg = f"Unknown OpenAI error: {sanitize_sensitive_details(raw_msg)[:150]}"

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

