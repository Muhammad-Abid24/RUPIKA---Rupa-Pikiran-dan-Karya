"""Unit and table-driven tests for provider error classification and diagnostics (SF-017)."""
from __future__ import annotations

import pytest

from stockforge.errors import (
    ERROR_CODE_DEFAULTS,
    ErrorAction,
    ErrorCode,
    ErrorSeverity,
    NormalizedProviderError,
    classify_error,
    format_telegram_error_alert,
    sanitize_sensitive_details,
)


def test_all_17_error_codes_supported():
    """Verify all 17 error codes specified in ARCHITECTURE.md are present with complete defaults."""
    expected_codes = {
        "INSUFFICIENT_CREDITS",
        "TOKEN_QUOTA_EXCEEDED",
        "DAILY_QUOTA_EXCEEDED",
        "INVALID_API_KEY",
        "RATE_LIMITED",
        "CONCURRENCY_LIMIT",
        "MODEL_NOT_FOUND",
        "MODEL_UNAVAILABLE",
        "PROVIDER_SERVER_ERROR",
        "REQUEST_TIMEOUT",
        "CONTENT_REJECTED",
        "INVALID_REQUEST",
        "IMAGE_DOWNLOAD_FAILED",
        "IMAGE_QC_FAILED",
        "DISK_FULL",
        "BUDGET_EXCEEDED",
        "UNKNOWN_PROVIDER_ERROR",
    }
    defined_codes = {code.value for code in ErrorCode}
    assert expected_codes == defined_codes, f"Missing codes: {expected_codes - defined_codes}"

    # Every code must have complete default settings
    for code in ErrorCode:
        defaults = ERROR_CODE_DEFAULTS.get(code)
        assert defaults is not None, f"Defaults missing for {code}"
        assert isinstance(defaults["retryable"], bool)
        assert isinstance(defaults["fallback_allowed"], bool)
        assert defaults["severity"] in [s.value for s in ErrorSeverity]
        assert defaults["action"] in [a.value for a in ErrorAction]
        assert "description" in defaults


def test_generic_http_429_never_classified_as_quota_exhaustion():
    """INVARIANT: Never classify generic HTTP 429 as token or credit exhaustion without explicit evidence."""
    # Generic HTTP 429
    res = classify_error(http_status=429, body={"message": "Too Many Requests"}, provider="generic")
    assert res.code == ErrorCode.RATE_LIMITED
    assert res.code != ErrorCode.INSUFFICIENT_CREDITS
    assert res.code != ErrorCode.TOKEN_QUOTA_EXCEEDED
    assert res.retryable is True
    assert res.fallback_allowed is True

    # OpenAI generic 429
    res_oa = classify_error(
        http_status=429,
        body={"error": {"message": "Rate limit reached. Please try again later.", "type": "rate_limit_error"}},
        provider="openai",
    )
    assert res_oa.code == ErrorCode.RATE_LIMITED
    assert res_oa.code != ErrorCode.INSUFFICIENT_CREDITS

    # Anthropic generic 429
    res_ant = classify_error(
        http_status=429,
        body={"type": "error", "error": {"type": "rate_limit_error", "message": "Number of requests per minute exceeded."}},
        provider="anthropic",
    )
    assert res_ant.code == ErrorCode.RATE_LIMITED
    assert res_ant.code != ErrorCode.INSUFFICIENT_CREDITS

    # Gemini generic 429
    res_gem = classify_error(
        http_status=429,
        body={"error": {"status": "RESOURCE_EXHAUSTED", "message": "Rate limit exceeded for requests"}},
        provider="gemini",
    )
    assert res_gem.code == ErrorCode.RATE_LIMITED
    assert res_gem.code != ErrorCode.INSUFFICIENT_CREDITS


def test_explicit_insufficient_funds_and_quota_detection():
    """Explicit provider billing and quota depletion errors must map to INSUFFICIENT_CREDITS or QUOTA codes."""
    # OpenAI explicit insufficient_quota
    res_oa = classify_error(
        http_status=429,
        body={
            "error": {
                "message": "You exceeded your current quota, please check your plan and billing details.",
                "type": "insufficient_quota",
                "code": "insufficient_quota",
            }
        },
        provider="openai",
    )
    assert res_oa.code == ErrorCode.INSUFFICIENT_CREDITS
    assert res_oa.action == ErrorAction.COOLDOWN_PROVIDER.value
    assert res_oa.fallback_allowed is True
    assert res_oa.retryable is False

    # Anthropic credit balance low
    res_ant = classify_error(
        http_status=400,
        body={"type": "error", "error": {"type": "invalid_request_error", "message": "Your credit balance is too low to access the Claude API."}},
        provider="anthropic",
    )
    assert res_ant.code == ErrorCode.INSUFFICIENT_CREDITS
    assert res_ant.action == ErrorAction.COOLDOWN_PROVIDER.value

    # Stability AI payment required (HTTP 402)
    res_stab = classify_error(
        http_status=402,
        body={"name": "payment_required", "message": "Not enough balance to execute request."},
        provider="stability",
    )
    assert res_stab.code == ErrorCode.INSUFFICIENT_CREDITS
    assert res_stab.action == ErrorAction.COOLDOWN_PROVIDER.value

    # Explicit daily quota
    res_daily = classify_error(
        http_status=429,
        body={"message": "Your daily quota has been reached for this resource."},
        provider="generic",
    )
    assert res_daily.code == ErrorCode.DAILY_QUOTA_EXCEEDED

    # Explicit token quota
    res_token = classify_error(
        http_status=429,
        body={"message": "Monthly token quota exhausted for organization."},
        provider="generic",
    )
    assert res_token.code == ErrorCode.TOKEN_QUOTA_EXCEEDED


def test_authentication_and_invalid_api_key_classification():
    """Invalid credentials or HTTP 401 must trigger INVALID_API_KEY and DISABLE_ROUTE."""
    cases = [
        ("openai", 401, {"error": {"message": "Incorrect API key provided", "type": "invalid_request_error", "code": "invalid_api_key"}}),
        ("anthropic", 401, {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}),
        ("gemini", 403, {"error": {"status": "PERMISSION_DENIED", "message": "API key not valid. Please pass a valid API key."}}),
        ("stability", 401, {"message": "Unauthorized: invalid api key"}),
        ("generic", 401, {"message": "Unauthorized"}),
    ]
    for prov, status, body in cases:
        res = classify_error(http_status=status, body=body, provider=prov)
        assert res.code == ErrorCode.INVALID_API_KEY, f"Failed for {prov}"
        assert res.action == ErrorAction.DISABLE_ROUTE.value
        assert res.retryable is False
        assert res.fallback_allowed is True


def test_content_moderation_policy_refusal():
    """Policy rejections must be CONTENT_REJECTED with NO fallback allowed to prevent evasion."""
    cases = [
        ("openai", 400, {"error": {"code": "content_policy_violation", "message": "Your request was rejected as a result of our safety system."}}),
        ("anthropic", 400, {"type": "error", "error": {"type": "invalid_request_error", "message": "Content moderation filter triggered."}}),
        ("gemini", 200, {"error": {"status": "SAFETY", "message": "Candidate blocked due to SAFETY harm_category"}}),
        ("stability", 400, {"name": "nsfw_detected", "message": "Image refused by NSFW filter."}),
    ]
    for prov, status, body in cases:
        res = classify_error(http_status=status, body=body, provider=prov)
        assert res.code == ErrorCode.CONTENT_REJECTED, f"Failed for {prov}"
        assert res.retryable is False
        assert res.fallback_allowed is False  # Invariant: no policy evasion via fallback
        assert res.action == ErrorAction.STOP_ITEM.value


def test_transient_server_errors_and_retry_after():
    """Server 5xx and Retry-After header parsing."""
    headers = {"Retry-After": "45", "X-Request-Id": "req_abc123"}
    res = classify_error(http_status=503, headers=headers, body="Service Unavailable", provider="openai")

    assert res.code == ErrorCode.MODEL_UNAVAILABLE
    assert res.retryable is True
    assert res.retry_after_seconds == 45.0
    assert res.request_id == "req_abc123"

    # 504 gateway timeout -> REQUEST_TIMEOUT
    res_to = classify_error(http_status=504, headers={"x-request-id": "req_to_99"}, provider="openai")
    assert res_to.code == ErrorCode.REQUEST_TIMEOUT
    assert res_to.action == ErrorAction.RECONCILE.value
    assert res_to.retryable is False  # Must reconcile before blind retry


def test_system_level_errors_classification():
    """Disk full, budget exceeded, and QC failures."""
    disk_err = classify_error(body={"message": "No space left on device: disk full"}, provider="generic")
    assert disk_err.code == ErrorCode.DISK_FULL
    assert disk_err.action == ErrorAction.PAUSE_QUEUE.value

    budget_err = classify_error(body={"message": "Daily budget exceeded for account"}, provider="generic")
    assert budget_err.code == ErrorCode.BUDGET_EXCEEDED
    assert budget_err.action == ErrorAction.PAUSE_QUEUE.value

    qc_err = classify_error(body={"message": "Image QC failed: resolution below 4.0 MP"}, provider="generic")
    assert qc_err.code == ErrorCode.IMAGE_QC_FAILED
    assert qc_err.action == ErrorAction.STOP_ITEM.value


def test_secret_redaction_and_safe_telegram_formatting():
    """Ensure no API keys, Bearer tokens, or passwords leak into DB, logs, or Telegram alerts."""
    secret_key = "sk-live-9999888877776666"
    raw_error_text = f"Failed to authenticate with Bearer {secret_key} and key={secret_key} on endpoint"

    sanitized = sanitize_sensitive_details(raw_error_text)
    assert secret_key not in sanitized
    assert "***REDACTED***" in sanitized

    # Classify error with secret payload
    res = classify_error(
        http_status=401,
        headers={"Authorization": f"Bearer {secret_key}", "x-request-id": "req_safe_001"},
        body={"error": {"message": raw_error_text, "code": f"err_{secret_key}"}},
        provider="openai",
        model="dall-e-3",
    )

    assert secret_key not in (res.safe_user_message or "")
    assert secret_key not in (res.raw_code_redacted or "")

    # Format Telegram alert
    alert_text = format_telegram_error_alert(
        error=res,
        batch_id=42,
        item_index=3,
        total_items=10,
        fallback_provider="stability",
        fallback_model="sd3-turbo",
    )

    assert secret_key not in alert_text
    assert "<b>Provider Error Alert: INVALID_API_KEY</b>" in alert_text
    assert "Batch #42 | Image 3/10" in alert_text
    assert "<code>openai (dall-e-3)</code>" in alert_text
    assert "<code>req_safe_001</code>" in alert_text
    assert "Routing to: <code>stability (sd3-turbo)</code>" in alert_text

