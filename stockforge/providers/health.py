"""Provider health, cooldown, and circuit breaker tracking (SF-018)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
import logging
from typing import Dict, Optional

from stockforge.errors.codes import ErrorCode, NormalizedProviderError

logger = logging.getLogger("stockforge.providers.health")


class ProviderStatus(str, Enum):
    HEALTHY = "HEALTHY"
    COOLDOWN = "COOLDOWN"
    DISABLED = "DISABLED"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class ProviderHealthRecord:
    provider: str
    status: ProviderStatus = ProviderStatus.HEALTHY
    cooldown_until: Optional[datetime] = None
    consecutive_errors: int = 0
    last_error_code: Optional[str] = None
    last_error_at: Optional[datetime] = None
    reason: Optional[str] = None


class ProviderHealthTracker:
    """Tracks provider availability, cooldown intervals, and permanent disables."""

    def __init__(
        self,
        default_cooldown_seconds: float = 300.0,
        max_consecutive_errors: int = 5,
    ) -> None:
        self.default_cooldown_seconds = default_cooldown_seconds
        self.max_consecutive_errors = max_consecutive_errors
        self._records: Dict[str, ProviderHealthRecord] = {}

    def _get_or_create(self, provider: str) -> ProviderHealthRecord:
        p = provider.lower()
        if p not in self._records:
            self._records[p] = ProviderHealthRecord(provider=p)
        return self._records[p]

    def is_available(self, provider: str) -> bool:
        """Check if provider is currently eligible to receive requests."""
        rec = self._get_or_create(provider)
        if rec.status == ProviderStatus.DISABLED:
            return False

        if rec.status == ProviderStatus.COOLDOWN:
            if rec.cooldown_until and utc_now() >= rec.cooldown_until:
                # Cooldown expired, transition back to HEALTHY
                rec.status = ProviderStatus.HEALTHY
                rec.cooldown_until = None
                rec.reason = None
                logger.info("Provider '%s' cooldown selesai, status kembali HEALTHY", provider)
                return True
            return False

        return True

    def get_cooldown_remaining(self, provider: str) -> float:
        """Return remaining cooldown seconds for provider, or 0.0 if not cooling down."""
        rec = self._get_or_create(provider)
        if rec.status != ProviderStatus.COOLDOWN or not rec.cooldown_until:
            return 0.0
        remaining = (rec.cooldown_until - utc_now()).total_seconds()
        return max(0.0, remaining)

    def record_success(self, provider: str) -> None:
        """Mark provider healthy and reset consecutive error count."""
        rec = self._get_or_create(provider)
        if rec.status != ProviderStatus.DISABLED:
            rec.status = ProviderStatus.HEALTHY
            rec.cooldown_until = None
            rec.consecutive_errors = 0
            rec.reason = None

    def record_error(self, provider: str, error: NormalizedProviderError) -> None:
        """Update provider health state based on normalized error."""
        rec = self._get_or_create(provider)
        now = utc_now()
        rec.consecutive_errors += 1
        rec.last_error_code = error.code.value
        rec.last_error_at = now

        # Case 1: Invalid API key -> Disable provider
        if error.code == ErrorCode.INVALID_API_KEY:
            rec.status = ProviderStatus.DISABLED
            rec.reason = f"Invalid API credentials ({error.raw_code_redacted or '401/403'})"
            logger.warning("Provider '%s' DINONAKTIFKAN: %s", provider, rec.reason)
            return

        # Case 2: Insufficient credits or daily quota -> Enter cooldown
        if error.code in (ErrorCode.INSUFFICIENT_CREDITS, ErrorCode.DAILY_QUOTA_EXCEEDED, ErrorCode.TOKEN_QUOTA_EXCEEDED):
            cooldown_secs = error.retry_after_seconds or self.default_cooldown_seconds
            rec.status = ProviderStatus.COOLDOWN
            rec.cooldown_until = now + timedelta(seconds=cooldown_secs)
            rec.reason = f"Quota/Credits depleted ({error.code.value})"
            logger.warning("Provider '%s' masuk COOLDOWN selama %.1fs: %s", provider, cooldown_secs, rec.reason)
            return

        # Case 3: Rate limited with Retry-After header
        if error.code == ErrorCode.RATE_LIMITED and error.retry_after_seconds:
            rec.status = ProviderStatus.COOLDOWN
            rec.cooldown_until = now + timedelta(seconds=error.retry_after_seconds)
            rec.reason = f"Rate limited; Retry-After {error.retry_after_seconds:.1f}s"
            logger.info("Provider '%s' rate limited, cooling down for %.1fs", provider, error.retry_after_seconds)
            return

        # Case 4: Repeated consecutive server/network errors
        if rec.consecutive_errors >= self.max_consecutive_errors:
            rec.status = ProviderStatus.COOLDOWN
            rec.cooldown_until = now + timedelta(seconds=self.default_cooldown_seconds)
            rec.reason = f"Exceeded {self.max_consecutive_errors} consecutive errors ({error.code.value})"
            logger.warning("Provider '%s' circuit-breaker dipicu: %s", provider, rec.reason)

    def manual_reset(self, provider: Optional[str] = None) -> None:
        """Manually reset health state for a single provider or all providers."""
        if provider:
            rec = self._get_or_create(provider)
            rec.status = ProviderStatus.HEALTHY
            rec.cooldown_until = None
            rec.consecutive_errors = 0
            rec.reason = None
        else:
            self._records.clear()


# Global tracker singleton
default_health_tracker = ProviderHealthTracker()

