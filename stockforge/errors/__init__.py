"""Error classification, normalization, and formatting package (SF-017)."""
from stockforge.errors.classifier import ErrorClassifier, classify_error, default_classifier
from stockforge.errors.codes import (
    ERROR_CODE_DEFAULTS,
    ErrorAction,
    ErrorCode,
    ErrorSeverity,
    NormalizedProviderError,
)
from stockforge.errors.formatter import format_telegram_error_alert
from stockforge.errors.provider_adapters.base import sanitize_sensitive_details

__all__ = [
    "ErrorCode",
    "ErrorSeverity",
    "ErrorAction",
    "ERROR_CODE_DEFAULTS",
    "NormalizedProviderError",
    "ErrorClassifier",
    "default_classifier",
    "classify_error",
    "format_telegram_error_alert",
    "sanitize_sensitive_details",
]
