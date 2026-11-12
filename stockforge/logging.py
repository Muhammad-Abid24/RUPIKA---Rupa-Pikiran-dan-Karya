"""Structured, secret-safe logging for StockForge (SF-001)."""
from __future__ import annotations

import logging
import re
from typing import Any, List, Optional


# Regex patterns to detect and mask common secrets in log messages
SECRET_PATTERNS = [
    (re.compile(r"(api[_-]?key\s*[:=]\s*['\"]?)([\w\-]+)(['\"]?)", re.IGNORECASE), r"\1***REDACTED***\3"),
    (re.compile(r"(token\s*[:=]\s*['\"]?)([\w\-]+)(['\"]?)", re.IGNORECASE), r"\1***REDACTED***\3"),
    (re.compile(r"(authorization\s*[:=]\s*['\"]?Bearer\s+)([\w\-\.]+)(['\"]?)", re.IGNORECASE), r"\1***REDACTED***\3"),
    (re.compile(r"(secret\s*[:=]\s*['\"]?)([\w\-]+)(['\"]?)", re.IGNORECASE), r"\1***REDACTED***\3"),
    (re.compile(r"bot\d+:[\w\-]+", re.IGNORECASE), r"bot***REDACTED***"),
]


def mask_secret(value: Optional[str], show_last: int = 4) -> str:
    """Mask a secret string, revealing at most the last `show_last` characters."""
    if not value:
        return "[NOT_SET]"
    val_str = str(value)
    if len(val_str) <= show_last:
        return "***REDACTED***"
    return f"{'*' * (len(val_str) - show_last)}{val_str[-show_last:]}"


class SecretMaskingFilter(logging.Filter):
    """Logging filter that scrubs sensitive keys and tokens from log output."""

    def __init__(self, additional_secrets: Optional[List[str]] = None) -> None:
        super().__init__()
        self.exact_secrets = [s for s in (additional_secrets or []) if s and len(s) > 3]

    def _mask_text(self, text: str) -> str:
        for pattern, repl in SECRET_PATTERNS:
            text = pattern.sub(repl, text)
        for secret in self.exact_secrets:
            text = text.replace(secret, "***REDACTED***")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self._mask_text(record.msg)

        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: (self._mask_text(v) if isinstance(v, str) else v)
                    for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    self._mask_text(a) if isinstance(a, str) else a
                    for a in record.args
                )
        return True


def setup_logging(
    level: int = logging.INFO,
    known_secrets: Optional[List[str]] = None,
) -> logging.Logger:
    """Configure root stockforge logger with safety filters and consistent formatting."""
    logger = logging.getLogger("stockforge")
    logger.setLevel(level)

    if not logger.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s (%(filename)s:%(lineno)d): %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)
        handler.addFilter(SecretMaskingFilter(additional_secrets=known_secrets))
        logger.addHandler(handler)

    return logger

