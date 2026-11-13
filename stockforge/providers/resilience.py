"""Safe retries, compatible fallback, jittered backoff, and pre-billable budget enforcement (SF-018)."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from stockforge.budget.service import BudgetExceededError, BudgetService
from stockforge.errors import ErrorCode, NormalizedProviderError, classify_error
from stockforge.providers.base import RoleType
from stockforge.providers.health import ProviderHealthTracker, default_health_tracker
from stockforge.providers.registry import IncompatibleFallbackError, ProviderRegistry
from stockforge.providers.router import ProviderRouter

logger = logging.getLogger("stockforge.providers.resilience")


class ResilienceError(Exception):
    """Base exception for resilience coordinator failures."""
    pass


class ContentPolicyViolationError(ResilienceError):
    """Raised when an operation is rejected by content moderation safety policy (no fallback allowed)."""
    def __init__(self, error: NormalizedProviderError) -> None:
        self.error = error
        super().__init__(f"Konten ditolak oleh kebijakan keamanan provider: {error.safe_user_message}")


class AmbiguousTimeoutError(ResilienceError):
    """Raised when an uncertain billable timeout occurs requiring reconciliation rather than blind retry."""
    def __init__(self, error: NormalizedProviderError, operation_id: Optional[str] = None) -> None:
        self.error = error
        self.operation_id = operation_id
        super().__init__(
            f"Timeout pada operasi berbayar '{operation_id or 'unknown'}'; "
            "butuh rekonsiliasi status untuk menghindari biaya ganda."
        )


class AllFallbacksExhaustedError(ResilienceError):
    """Raised when all primary and compatible fallback providers have been exhausted."""
    def __init__(self, role: RoleType, attempted_models: List[str], last_error: Optional[NormalizedProviderError] = None) -> None:
        self.role = role
        self.attempted_models = attempted_models
        self.last_error = last_error
        super().__init__(
            f"Semua rute fallback untuk role '{role.value}' telah habis. Model dicoba: {attempted_models}"
        )


def calculate_backoff_delay(
    attempt: int,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    retry_after: Optional[float] = None,
    jitter: bool = True,
) -> float:
    """Calculate bounded exponential backoff with jitter and Retry-After adherence."""
    if retry_after is not None and retry_after > 0:
        return min(max_delay, float(retry_after))

    # Bounded exponential: base_delay * 2^(attempt - 1)
    exp_delay = base_delay * (2 ** max(0, attempt - 1))
    bounded = min(max_delay, exp_delay)
    if jitter:
        # Full or decorrelated jitter (0.5 * bounded .. bounded)
        bounded = random.uniform(bounded * 0.5, bounded)
    return max(0.01, bounded)


@dataclass
class FallbackEvent:
    job_id: Optional[int]
    item_index: Optional[int]
    role: RoleType
    from_model: str
    to_model: str
    reason_code: str
    message: str


class ResilientExecutionCoordinator:
    """Coordinates retries, role fallback chains, pre-billable budget checks, and attempt logging."""

    def __init__(
        self,
        router: ProviderRouter,
        registry: ProviderRegistry,
        health_tracker: Optional[ProviderHealthTracker] = None,
        budget_service: Optional[BudgetService] = None,
        max_attempts_per_provider: int = 3,
        base_delay_seconds: float = 1.0,
        max_delay_seconds: float = 30.0,
        sleeper: Optional[Callable[[float], None]] = None,
    ) -> None:
        self.router = router
        self.registry = registry
        self.health_tracker = health_tracker if health_tracker is not None else ProviderHealthTracker()
        self.budget_service = budget_service
        self.max_attempts_per_provider = max_attempts_per_provider
        self.base_delay_seconds = base_delay_seconds
        self.max_delay_seconds = max_delay_seconds
        self.sleeper = sleeper or time.sleep
        self.fallback_events: List[FallbackEvent] = []

    def get_candidate_models_for_role(self, role: RoleType) -> List[str]:
        """Return ordered list of valid, compatible candidate models for a role."""
        route = self.router.get_route(role)
        candidates = [route.primary_model_id] + list(route.fallback_model_ids)
        return candidates

    def execute_with_resilience(
        self,
        role: RoleType,
        call_fn: Callable[[str], Any],
        job_id: Optional[int] = None,
        item_index: Optional[int] = None,
        is_billable: bool = False,
        estimated_cost_cents: int = 0,
        operation_id: Optional[str] = None,
        attempt_recorder: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Any:
        """Execute provider call with retry backoff, compatible fallback, and pre-billable checks."""
        candidates = self.get_candidate_models_for_role(role)
        attempted_models: List[str] = []
        last_error: Optional[NormalizedProviderError] = None
        global_attempt_num = 0

        for candidate_idx, model_id in enumerate(candidates):
            model_info = self.registry.get_model(model_id)
            provider_name = model_info.provider_name if model_info else "unknown"

            # Check provider health
            if not self.health_tracker.is_available(provider_name):
                logger.info(
                    "Melewati model '%s' (provider '%s' tidak tersedia / cooldown)",
                    model_id,
                    provider_name,
                )
                continue

            attempted_models.append(model_id)
            current_model_attempts = 0

            while current_model_attempts < self.max_attempts_per_provider:
                current_model_attempts += 1
                global_attempt_num += 1

                # 1. Budget check before EVERY billable attempt
                if is_billable and estimated_cost_cents > 0 and self.budget_service and job_id:
                    try:
                        self.budget_service.reserve_budget(
                            job_id=job_id,
                            estimated_cost_cents=estimated_cost_cents,
                        )
                    except BudgetExceededError as be:
                        logger.error("Batas anggaran terlampaui sebelum pemanggilan berbayar: %s", be)
                        # Record attempt failure
                        if attempt_recorder:
                            attempt_recorder({
                                "attempt_number": global_attempt_num,
                                "provider": provider_name,
                                "model": model_id,
                                "role": role.value,
                                "normalized_error": ErrorCode.BUDGET_EXCEEDED.value,
                                "http_status": None,
                                "cost_cents": 0,
                            })
                        raise be

                # 2. Invoke the provider function
                try:
                    result = call_fn(model_id)
                    # Mark success
                    self.health_tracker.record_success(provider_name)
                    if attempt_recorder:
                        attempt_recorder({
                            "attempt_number": global_attempt_num,
                            "provider": provider_name,
                            "model": model_id,
                            "role": role.value,
                            "normalized_error": None,
                            "http_status": 200,
                            "cost_cents": estimated_cost_cents if is_billable else 0,
                        })
                    return result
                except BudgetExceededError:
                    raise
                except Exception as exc:
                    norm_err = classify_error(
                        exception=exc,
                        provider=provider_name,
                        model=model_id,
                    )
                    last_error = norm_err
                    self.health_tracker.record_error(provider_name, norm_err)

                    if attempt_recorder:
                        attempt_recorder({
                            "attempt_number": global_attempt_num,
                            "provider": provider_name,
                            "model": model_id,
                            "role": role.value,
                            "normalized_error": norm_err.code.value,
                            "http_status": norm_err.http_status,
                            "retry_after": norm_err.retry_after_seconds,
                            "cost_cents": 0,
                        })

                    # Case A: Content moderation policy rejection -> STOP ITEM IMMEDIATELY
                    if norm_err.code == ErrorCode.CONTENT_REJECTED:
                        logger.warning("Konten ditolak oleh provider %s: penghentian segera", provider_name)
                        raise ContentPolicyViolationError(norm_err)

                    # Case B: Ambiguous timeout on a billable call -> RECONCILE, DO NOT BLIND RETRY
                    if norm_err.code == ErrorCode.REQUEST_TIMEOUT and is_billable:
                        logger.warning("Timeout operasi berbayar pada %s: butuh rekonsiliasi status", model_id)
                        raise AmbiguousTimeoutError(norm_err, operation_id=operation_id)

                    # Case C: Retryable error and attempts left
                    can_retry = norm_err.retryable and current_model_attempts < self.max_attempts_per_provider
                    if can_retry and (self.health_tracker.is_available(provider_name) or norm_err.code == ErrorCode.RATE_LIMITED):
                        delay = calculate_backoff_delay(
                            attempt=current_model_attempts,
                            base_delay=self.base_delay_seconds,
                            max_delay=self.max_delay_seconds,
                            retry_after=norm_err.retry_after_seconds,
                        )
                        logger.info(
                            "Retry attempt %d/%d untuk model %s setelah delay %.2fs",
                            current_model_attempts,
                            self.max_attempts_per_provider,
                            model_id,
                            delay,
                        )
                        self.sleeper(delay)
                        # After waiting the backoff/retry-after delay, reset rate-limit cooldown for the retry attempt
                        if norm_err.code == ErrorCode.RATE_LIMITED:
                            self.health_tracker.manual_reset(provider_name)
                        continue

                    # If not retryable or retries exhausted, check if fallback is allowed
                    if norm_err.fallback_allowed:
                        next_idx = candidate_idx + 1
                        if next_idx < len(candidates):
                            next_model = candidates[next_idx]
                            event = FallbackEvent(
                                job_id=job_id,
                                item_index=item_index,
                                role=role,
                                from_model=model_id,
                                to_model=next_model,
                                reason_code=norm_err.code.value,
                                message=norm_err.safe_user_message,
                            )
                            self.fallback_events.append(event)
                            logger.warning(
                                "Fallback diaktifkan untuk role %s: beralih dari %s ke %s (alasan: %s)",
                                role.value,
                                model_id,
                                next_model,
                                norm_err.code.value,
                            )
                        break
                    else:
                        # Fallback not allowed for this error type (e.g. INVALID_REQUEST)
                        raise ResilienceError(f"Error tidak mengizinkan fallback: {norm_err.safe_user_message}")

        # If loop finishes without returning, all fallbacks are exhausted
        raise AllFallbacksExhaustedError(role, attempted_models, last_error=last_error)
