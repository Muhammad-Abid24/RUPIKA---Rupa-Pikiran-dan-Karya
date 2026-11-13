"""Normalized provider error codes, actions, and data models (SF-017)."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class ErrorCode(str, Enum):
    INSUFFICIENT_CREDITS = "INSUFFICIENT_CREDITS"
    TOKEN_QUOTA_EXCEEDED = "TOKEN_QUOTA_EXCEEDED"
    DAILY_QUOTA_EXCEEDED = "DAILY_QUOTA_EXCEEDED"
    INVALID_API_KEY = "INVALID_API_KEY"
    RATE_LIMITED = "RATE_LIMITED"
    CONCURRENCY_LIMIT = "CONCURRENCY_LIMIT"
    MODEL_NOT_FOUND = "MODEL_NOT_FOUND"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    PROVIDER_SERVER_ERROR = "PROVIDER_SERVER_ERROR"
    REQUEST_TIMEOUT = "REQUEST_TIMEOUT"
    CONTENT_REJECTED = "CONTENT_REJECTED"
    INVALID_REQUEST = "INVALID_REQUEST"
    IMAGE_DOWNLOAD_FAILED = "IMAGE_DOWNLOAD_FAILED"
    IMAGE_QC_FAILED = "IMAGE_QC_FAILED"
    DISK_FULL = "DISK_FULL"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    UNKNOWN_PROVIDER_ERROR = "UNKNOWN_PROVIDER_ERROR"


class ErrorSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class ErrorAction(str, Enum):
    RETRY = "retry"
    FALLBACK = "fallback"
    COOLDOWN_PROVIDER = "cooldown_provider"
    DISABLE_ROUTE = "disable_route"
    STOP_ITEM = "stop_item"
    PAUSE_QUEUE = "pause_queue"
    RECONCILE = "reconcile"
    ALERT_OPERATOR = "alert_operator"


# Default behavior mappings as required by ARCHITECTURE.md
ERROR_CODE_DEFAULTS: Dict[ErrorCode, Dict[str, Any]] = {
    ErrorCode.INSUFFICIENT_CREDITS: {
        "retryable": False,
        "fallback_allowed": True,
        "severity": ErrorSeverity.CRITICAL.value,
        "action": ErrorAction.COOLDOWN_PROVIDER.value,
        "description": "Provider account balance or prepaid credits exhausted.",
    },
    ErrorCode.TOKEN_QUOTA_EXCEEDED: {
        "retryable": False,
        "fallback_allowed": True,
        "severity": ErrorSeverity.ERROR.value,
        "action": ErrorAction.FALLBACK.value,
        "description": "Token usage quota exhausted for billing cycle.",
    },
    ErrorCode.DAILY_QUOTA_EXCEEDED: {
        "retryable": False,
        "fallback_allowed": True,
        "severity": ErrorSeverity.ERROR.value,
        "action": ErrorAction.FALLBACK.value,
        "description": "Daily request or token quota limit reached.",
    },
    ErrorCode.INVALID_API_KEY: {
        "retryable": False,
        "fallback_allowed": True,
        "severity": ErrorSeverity.CRITICAL.value,
        "action": ErrorAction.DISABLE_ROUTE.value,
        "description": "Provider API credentials invalid, expired, or revoked.",
    },
    ErrorCode.RATE_LIMITED: {
        "retryable": True,
        "fallback_allowed": True,
        "severity": ErrorSeverity.WARNING.value,
        "action": ErrorAction.RETRY.value,
        "description": "Rate limit exceeded; backoff with jitter and honor Retry-After.",
    },
    ErrorCode.CONCURRENCY_LIMIT: {
        "retryable": True,
        "fallback_allowed": False,
        "severity": ErrorSeverity.WARNING.value,
        "action": ErrorAction.RETRY.value,
        "description": "Concurrent in-flight request limit reached; queue and back off.",
    },
    ErrorCode.MODEL_NOT_FOUND: {
        "retryable": False,
        "fallback_allowed": True,
        "severity": ErrorSeverity.ERROR.value,
        "action": ErrorAction.DISABLE_ROUTE.value,
        "description": "Requested model does not exist or is deprecated.",
    },
    ErrorCode.MODEL_UNAVAILABLE: {
        "retryable": True,
        "fallback_allowed": True,
        "severity": ErrorSeverity.WARNING.value,
        "action": ErrorAction.FALLBACK.value,
        "description": "Model temporarily unavailable or experiencing high load.",
    },
    ErrorCode.PROVIDER_SERVER_ERROR: {
        "retryable": True,
        "fallback_allowed": True,
        "severity": ErrorSeverity.WARNING.value,
        "action": ErrorAction.RETRY.value,
        "description": "Provider internal server error (5xx); retry with jitter or fallback.",
    },
    ErrorCode.REQUEST_TIMEOUT: {
        "retryable": False,  # Must reconcile uncertain billable operations first
        "fallback_allowed": False,
        "severity": ErrorSeverity.ERROR.value,
        "action": ErrorAction.RECONCILE.value,
        "description": "Network or transport timeout; requires status reconciliation.",
    },
    ErrorCode.CONTENT_REJECTED: {
        "retryable": False,
        "fallback_allowed": False,  # Prohibit policy-evasion fallback
        "severity": ErrorSeverity.ERROR.value,
        "action": ErrorAction.STOP_ITEM.value,
        "description": "Prompt or output refused by provider safety / content moderation policy.",
    },
    ErrorCode.INVALID_REQUEST: {
        "retryable": False,
        "fallback_allowed": False,
        "severity": ErrorSeverity.ERROR.value,
        "action": ErrorAction.STOP_ITEM.value,
        "description": "Malformed request parameters, dimensions, or unparseable payload.",
    },
    ErrorCode.IMAGE_DOWNLOAD_FAILED: {
        "retryable": True,
        "fallback_allowed": False,
        "severity": ErrorSeverity.WARNING.value,
        "action": ErrorAction.RETRY.value,
        "description": "Image download from provider CDN failed; retry if safe.",
    },
    ErrorCode.IMAGE_QC_FAILED: {
        "retryable": False,
        "fallback_allowed": False,
        "severity": ErrorSeverity.WARNING.value,
        "action": ErrorAction.STOP_ITEM.value,
        "description": "Image failed deterministic or vision quality control.",
    },
    ErrorCode.DISK_FULL: {
        "retryable": False,
        "fallback_allowed": False,
        "severity": ErrorSeverity.CRITICAL.value,
        "action": ErrorAction.PAUSE_QUEUE.value,
        "description": "Storage exhaustion or critical disk space threshold reached.",
    },
    ErrorCode.BUDGET_EXCEEDED: {
        "retryable": False,
        "fallback_allowed": False,
        "severity": ErrorSeverity.CRITICAL.value,
        "action": ErrorAction.PAUSE_QUEUE.value,
        "description": "Internal budget cap reached; pause queue without fallback bypass.",
    },
    ErrorCode.UNKNOWN_PROVIDER_ERROR: {
        "retryable": True,
        "fallback_allowed": True,
        "severity": ErrorSeverity.ERROR.value,
        "action": ErrorAction.ALERT_OPERATOR.value,
        "description": "Unclassified provider or network error; handle with safe bounded retry.",
    },
}


@dataclass
class NormalizedProviderError:
    """Normalized, typed provider error representation."""
    code: ErrorCode
    retryable: bool
    fallback_allowed: bool
    severity: str
    action: str
    retry_after_seconds: Optional[float] = None
    safe_user_message: str = ""
    raw_code_redacted: Optional[str] = None
    http_status: Optional[int] = None
    request_id: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code.value if isinstance(self.code, ErrorCode) else str(self.code),
            "retryable": self.retryable,
            "fallback_allowed": self.fallback_allowed,
            "severity": self.severity,
            "action": self.action,
            "retry_after_seconds": self.retry_after_seconds,
            "safe_user_message": self.safe_user_message,
            "raw_code_redacted": self.raw_code_redacted,
            "http_status": self.http_status,
            "request_id": self.request_id,
            "provider": self.provider,
            "model": self.model,
            "details": self.details,
        }

