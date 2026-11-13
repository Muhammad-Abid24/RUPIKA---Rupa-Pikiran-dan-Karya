"""Google Gemini error adapter for error classification (SF-017)."""
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


class GeminiErrorAdapter(BaseProviderErrorAdapter):
    provider_name: str = "gemini"

    def can_handle(self, provider: Optional[str]) -> bool:
        if not provider:
            return False
        p = provider.lower()
        return p in ("gemini", "google", "vertex", "vertex_ai")

    def classify(
        self,
        http_status: Optional[int],
        headers: Optional[Dict[str, str]],
        body: Optional[Any],
        request_id: Optional[str] = None,
        provider: Optional[str] = "gemini",
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

        status_str = str(err_dict.get("status") or "")
        raw_msg = str(err_dict.get("message") or "")
        low_msg = raw_msg.lower()

        raw_code_redacted = sanitize_sensitive_details(status_str or (f"HTTP_{http_status}" if http_status else None))

        # Permission denied / invalid API key
        if http_status in (401, 403) and ("api_key_invalid" in low_msg or status_str == "PERMISSION_DENIED" or "api key not valid" in low_msg):
            code = ErrorCode.INVALID_API_KEY
            user_msg = "Gemini API key is invalid or lacks necessary permissions."
        # Quota / credits vs Rate limit:
        # RESOURCE_EXHAUSTED or HTTP 429
        elif status_str == "RESOURCE_EXHAUSTED" or http_status == 429:
            if "quota exceeded" in low_msg and ("billing" in low_msg or "check plan and billing" in low_msg or "free tier quota" in low_msg):
                code = ErrorCode.INSUFFICIENT_CREDITS
                user_msg = "Google Gemini project quota/credits exhausted."
            elif "daily" in low_msg and "quota" in low_msg:
                code = ErrorCode.DAILY_QUOTA_EXCEEDED
                user_msg = "Google Gemini daily request quota reached."
            elif "per minute" in low_msg or "rate limit" in low_msg:
                code = ErrorCode.RATE_LIMITED
                user_msg = "Google Gemini rate limit (RPM/TPM) reached; backoff in progress."
            else:
                # Invariant: Never classify generic 429 as token exhaustion without explicit provider evidence!
                code = ErrorCode.RATE_LIMITED
                user_msg = "Google Gemini rate limit reached; backoff in progress."
        # Safety / Content filtering
        elif status_str == "SAFETY" or "safety" in low_msg or "blocked by safety filters" in low_msg or "harm_category" in low_msg:
            code = ErrorCode.CONTENT_REJECTED
            user_msg = "Prompt blocked by Google Gemini safety filters."
        # Not found
        elif http_status == 404 or status_str == "NOT_FOUND":
            code = ErrorCode.MODEL_NOT_FOUND
            user_msg = f"Requested Gemini model '{model}' not found."
        # Unavailable
        elif http_status == 503 or status_str == "UNAVAILABLE":
            code = ErrorCode.MODEL_UNAVAILABLE
            user_msg = "Gemini service temporarily unavailable."
        # Server error
        elif http_status and http_status >= 500:
            if http_status == 504:
                code = ErrorCode.REQUEST_TIMEOUT
                user_msg = "Gemini gateway timed out."
            else:
                code = ErrorCode.PROVIDER_SERVER_ERROR
                user_msg = f"Gemini server error (HTTP {http_status})."
        elif http_status == 400 or status_str == "INVALID_ARGUMENT":
            code = ErrorCode.INVALID_REQUEST
            user_msg = f"Invalid argument to Gemini: {sanitize_sensitive_details(raw_msg)[:150]}"
        elif http_status == 408 or status_str == "DEADLINE_EXCEEDED":
            code = ErrorCode.REQUEST_TIMEOUT
            user_msg = "Gemini request deadline exceeded."
        else:
            code = ErrorCode.UNKNOWN_PROVIDER_ERROR
            user_msg = f"Unknown Gemini error: {sanitize_sensitive_details(raw_msg)[:150]}"

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
            details={"raw_status": sanitize_sensitive_details(status_str)},
        )
