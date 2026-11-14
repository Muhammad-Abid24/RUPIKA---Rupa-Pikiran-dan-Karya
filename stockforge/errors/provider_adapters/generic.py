"""Generic HTTP and system error adapter for fallback classification (SF-017)."""
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


class GenericErrorAdapter(BaseProviderErrorAdapter):
    provider_name: str = "generic"

    def can_handle(self, provider: Optional[str]) -> bool:
        return True  # Fallback for any provider

    def classify(
        self,
        http_status: Optional[int],
        headers: Optional[Dict[str, str]],
        body: Optional[Any],
        request_id: Optional[str] = None,
        provider: Optional[str] = "generic",
        model: Optional[str] = None,
    ) -> NormalizedProviderError:
        req_id = extract_request_id(headers, request_id)
        retry_after = parse_retry_after(headers)

        msg_str = ""
        raw_code = None
        if isinstance(body, dict):
            raw_code = str(body.get("code") or body.get("error") or "")
            msg_str = str(body.get("message") or body)
        elif isinstance(body, str):
            try:
                parsed = json.loads(body)
                if isinstance(parsed, dict):
                    raw_code = str(parsed.get("code") or parsed.get("error") or "")
                    msg_str = str(parsed.get("message") or body)
                else:
                    msg_str = body
            except Exception:
                msg_str = body
        elif body is not None:
            msg_str = str(body)

        low_msg = msg_str.lower()
        raw_code_redacted = sanitize_sensitive_details(raw_code or (f"HTTP_{http_status}" if http_status else None))

        # Check explicit system errors first
        if "disk full" in low_msg or "no space left on device" in low_msg:
            code = ErrorCode.DISK_FULL
            user_msg = "Disk storage is full; write operations suspended."
        elif "budget exceeded" in low_msg or "daily budget" in low_msg or "monthly budget" in low_msg:
            code = ErrorCode.BUDGET_EXCEEDED
            user_msg = "Budget limit reached; generation suspended."
        elif "download failed" in low_msg or "failed to download" in low_msg or "image corrupt" in low_msg:
            code = ErrorCode.IMAGE_DOWNLOAD_FAILED
            user_msg = "Image artifact download from provider failed."
        elif "qc failed" in low_msg or "quality check failed" in low_msg:
            code = ErrorCode.IMAGE_QC_FAILED
            user_msg = "Generated image failed quality inspection."
        # HTTP Status-driven classification
        elif http_status == 401:
            code = ErrorCode.INVALID_API_KEY
            user_msg = f"{provider} credentials invalid or unauthorized (HTTP 401)."
        elif http_status == 402:
            code = ErrorCode.INSUFFICIENT_CREDITS
            user_msg = f"{provider} payment required or credits exhausted (HTTP 402)."
        elif http_status == 403:
            if "policy" in low_msg or "content" in low_msg or "moderation" in low_msg:
                code = ErrorCode.CONTENT_REJECTED
                user_msg = f"{provider} access forbidden due to content policy (HTTP 403)."
            else:
                code = ErrorCode.INVALID_API_KEY
                user_msg = f"{provider} access forbidden / invalid credentials (HTTP 403)."
        elif http_status == 404:
            code = ErrorCode.MODEL_NOT_FOUND
            user_msg = f"Requested resource or model '{model}' not found on {provider} (HTTP 404)."
        elif http_status == 408:
            code = ErrorCode.REQUEST_TIMEOUT
            user_msg = f"Request to {provider} timed out (HTTP 408)."
        elif http_status == 429:
            # INVARIANT: Never classify generic HTTP 429 as token exhaustion without explicit provider evidence!
            if ("insufficient" in low_msg and "quota" in low_msg) or "credit balance" in low_msg:
                code = ErrorCode.INSUFFICIENT_CREDITS
                user_msg = f"{provider} quota or credit balance exhausted."
            elif "daily quota" in low_msg or "quota reached for the day" in low_msg:
                code = ErrorCode.DAILY_QUOTA_EXCEEDED
                user_msg = f"{provider} daily quota exceeded."
            elif "token quota" in low_msg:
                code = ErrorCode.TOKEN_QUOTA_EXCEEDED
                user_msg = f"{provider} token quota exhausted."
            else:
                # Default generic 429 MUST be RATE_LIMITED
                code = ErrorCode.RATE_LIMITED
                user_msg = f"{provider} rate limit exceeded (HTTP 429); backoff in progress."
        elif http_status == 400:
            if "safety" in low_msg or "moderation" in low_msg or "nsfw" in low_msg:
                code = ErrorCode.CONTENT_REJECTED
                user_msg = f"{provider} rejected prompt under content policy."
            else:
                code = ErrorCode.INVALID_REQUEST
                user_msg = f"{provider} bad request (HTTP 400): {sanitize_sensitive_details(msg_str)[:150]}"
        elif http_status == 413 or "too large" in low_msg or "entity too large" in low_msg:
            code = ErrorCode.INVALID_REQUEST
            user_msg = f"{provider} payload too large (HTTP 413): entity size exceeds provider limit."
        elif http_status == 409:
            code = ErrorCode.CONCURRENCY_LIMIT
            user_msg = f"{provider} conflict / concurrent request limit (HTTP 409)."
        elif http_status in (502, 503, 529):
            code = ErrorCode.MODEL_UNAVAILABLE
            user_msg = f"{provider} temporarily unavailable (HTTP {http_status})."
        elif http_status == 504:
            code = ErrorCode.REQUEST_TIMEOUT
            user_msg = f"{provider} gateway timeout (HTTP 504)."
        elif http_status and http_status >= 500:
            code = ErrorCode.PROVIDER_SERVER_ERROR
            user_msg = f"{provider} server error (HTTP {http_status})."
        elif "timeout" in low_msg or "timed out" in low_msg:
            code = ErrorCode.REQUEST_TIMEOUT
            user_msg = f"Connection to {provider} timed out."
        else:
            code = ErrorCode.UNKNOWN_PROVIDER_ERROR
            user_msg = f"Unexpected error from {provider}: {sanitize_sensitive_details(msg_str)[:150]}"

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
            details={"raw_message": sanitize_sensitive_details(msg_str)[:200]},
        )

