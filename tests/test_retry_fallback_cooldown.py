"""Unit and integration tests for safe retries, compatible fallback, cooldown, and budget safety (SF-018)."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from stockforge.budget.service import BudgetExceededError, BudgetService
from stockforge.db.models import Base, Job, JobAttempt, JobItem
from stockforge.errors import ErrorCode
from stockforge.providers import (
    AllFallbacksExhaustedError,
    AmbiguousTimeoutError,
    ContentPolicyViolationError,
    ProviderCapability,
    ProviderHealthTracker,
    ProviderKind,
    ProviderRegistry,
    ProviderRouter,
    ProviderStatus,
    ResilientExecutionCoordinator,
    RoleType,
    calculate_backoff_delay,
)
from stockforge.queue.service import QueueService
from stockforge.queue.tracker import ItemProgressTracker
from stockforge.queue.worker import Worker


@pytest.fixture
def db_session_factory():
    """In-memory SQLite DB with all schema tables created."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture
def mock_router_and_registry():
    """Set up registry and router with primary and compatible fallback models."""
    registry = ProviderRegistry()
    # Provider 1: OpenAI
    registry.register_provider("openai", ProviderKind.IMAGE, base_url="https://api.openai.com/v1", secret_env="OPENAI_API_KEY")
    registry.register_model(
        model_id="openai-dalle3",
        provider_name="openai",
        kind=ProviderKind.IMAGE,
        capabilities={ProviderCapability.IMAGE_GENERATION, ProviderCapability.COMMERCIAL_LICENSE_VERIFIED},
        license_evidence="openai_commercial_license",
        cost_per_unit_cents=4,
    )

    # Provider 2: Stability AI
    registry.register_provider("stability", ProviderKind.IMAGE, base_url="https://api.stability.ai/v2", secret_env="STABILITY_API_KEY")
    registry.register_model(
        model_id="stability-sd3",
        provider_name="stability",
        kind=ProviderKind.IMAGE,
        capabilities={ProviderCapability.IMAGE_GENERATION, ProviderCapability.COMMERCIAL_LICENSE_VERIFIED},
        license_evidence="stability_commercial_license",
        cost_per_unit_cents=3,
    )

    # Provider 3: Fal AI
    registry.register_provider("fal", ProviderKind.IMAGE, base_url="https://api.fal.ai/v1", secret_env="FAL_KEY")
    registry.register_model(
        model_id="fal-flux",
        provider_name="fal",
        kind=ProviderKind.IMAGE,
        capabilities={ProviderCapability.IMAGE_GENERATION, ProviderCapability.COMMERCIAL_LICENSE_VERIFIED},
        license_evidence="fal_commercial_license",
        cost_per_unit_cents=3,
    )

    router = ProviderRouter(registry)
    router.set_route(
        role=RoleType.IMAGE,
        primary_model_id="openai-dalle3",
        fallbacks=["stability-sd3", "fal-flux"],
    )
    return router, registry


def test_retry_after_and_backoff_calculation():
    """Test exponential backoff and Retry-After header adherence."""
    # With explicit Retry-After
    delay = calculate_backoff_delay(attempt=1, retry_after=12.5, max_delay=30.0)
    assert delay == 12.5

    # Retry-After exceeding max_delay is capped
    delay_capped = calculate_backoff_delay(attempt=1, retry_after=120.0, max_delay=60.0)
    assert delay_capped == 60.0

    # Exponential growth without retry-after (jittered between 0.5 * delay and delay)
    d1 = calculate_backoff_delay(attempt=1, base_delay=2.0, jitter=False)
    d2 = calculate_backoff_delay(attempt=2, base_delay=2.0, jitter=False)
    d3 = calculate_backoff_delay(attempt=3, base_delay=2.0, jitter=False)
    assert d1 == 2.0
    assert d2 == 4.0
    assert d3 == 8.0


def test_insufficient_credits_triggers_fallback_and_cooldown(mock_router_and_registry):
    """Primary provider with insufficient credits triggers cooldown and falls back to candidate."""
    router, registry = mock_router_and_registry
    health_tracker = ProviderHealthTracker(default_cooldown_seconds=600.0)

    sleep_calls = []
    coordinator = ResilientExecutionCoordinator(
        router=router,
        registry=registry,
        health_tracker=health_tracker,
        max_attempts_per_provider=2,
        sleeper=lambda s: sleep_calls.append(s),
    )

    call_counts = {"openai-dalle3": 0, "stability-sd3": 0}

    def fake_image_api(model_id: str):
        call_counts[model_id] += 1
        if model_id == "openai-dalle3":
            # Simulate OpenAI 429 with insufficient_quota
            raise Exception("You exceeded your current quota, please check your plan and billing details (code: insufficient_quota)")
        return f"image_bytes_from_{model_id}"

    attempts_logged = []
    result = coordinator.execute_with_resilience(
        role=RoleType.IMAGE,
        call_fn=fake_image_api,
        job_id=1,
        item_index=1,
        attempt_recorder=lambda a: attempts_logged.append(a),
    )

    assert result == "image_bytes_from_stability-sd3"
    assert call_counts["openai-dalle3"] == 1
    assert call_counts["stability-sd3"] == 1

    # Verify OpenAI is now in COOLDOWN
    assert health_tracker.is_available("openai") is False
    assert health_tracker.get_cooldown_remaining("openai") > 0

    # Verify fallback event was recorded
    assert len(coordinator.fallback_events) == 1
    event = coordinator.fallback_events[0]
    assert event.from_model == "openai-dalle3"
    assert event.to_model == "stability-sd3"
    assert event.reason_code == ErrorCode.INSUFFICIENT_CREDITS.value

    # Subsequent request skips OpenAI directly without making a doomed HTTP call
    call_counts["openai-dalle3"] = 0
    call_counts["stability-sd3"] = 0
    res2 = coordinator.execute_with_resilience(
        role=RoleType.IMAGE,
        call_fn=fake_image_api,
        job_id=1,
        item_index=2,
    )
    assert res2 == "image_bytes_from_stability-sd3"
    assert call_counts["openai-dalle3"] == 0  # Skipped!
    assert call_counts["stability-sd3"] == 1


def test_rate_limited_429_honors_retry_after(mock_router_and_registry):
    """HTTP 429 rate limit triggers bounded retry with Retry-After adherence."""
    router, registry = mock_router_and_registry
    sleep_durations = []
    coordinator = ResilientExecutionCoordinator(
        router=router,
        registry=registry,
        max_attempts_per_provider=3,
        sleeper=lambda s: sleep_durations.append(s),
    )

    attempts = 0

    def rate_limited_api(model_id: str):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            # Raise exception mimicking HTTP 429 with Retry-After header
            class MockHttp429(Exception):
                status_code = 429
                headers = {"Retry-After": "10"}
                body = {"message": "Rate limit reached. Please wait."}
            raise MockHttp429("Rate limit exceeded")
        return "success_after_backoff"

    res = coordinator.execute_with_resilience(role=RoleType.IMAGE, call_fn=rate_limited_api)
    assert res == "success_after_backoff"
    assert attempts == 2
    assert len(sleep_durations) == 1
    assert sleep_durations[0] == 10.0


def test_content_policy_refusal_prohibits_fallback_evasion(mock_router_and_registry):
    """INVARIANT: Content policy violation stops item immediately; no fallback allowed."""
    router, registry = mock_router_and_registry
    coordinator = ResilientExecutionCoordinator(router=router, registry=registry)

    def safety_violation_api(model_id: str):
        class MockPolicyError(Exception):
            status_code = 400
            body = {"error": {"code": "content_policy_violation", "message": "Prompt refused by safety filters"}}
        raise MockPolicyError("Policy rejection")

    with pytest.raises(ContentPolicyViolationError) as exc_info:
        coordinator.execute_with_resilience(role=RoleType.IMAGE, call_fn=safety_violation_api)

    assert exc_info.value.error.code == ErrorCode.CONTENT_REJECTED
    # Fallback events must be empty (did not attempt to evade policy)
    assert len(coordinator.fallback_events) == 0


def test_ambiguous_billable_timeout_demands_reconciliation(mock_router_and_registry):
    """Ambiguous timeout on billable call must NOT blind-retry; must raise AmbiguousTimeoutError."""
    router, registry = mock_router_and_registry
    coordinator = ResilientExecutionCoordinator(router=router, registry=registry, max_attempts_per_provider=3)

    call_count = 0

    def timeout_api(model_id: str):
        nonlocal call_count
        call_count += 1
        class MockTimeout(Exception):
            status_code = 504
            headers = {"x-request-id": "req_timeout_test"}
            body = {"message": "Gateway Timeout"}
        raise MockTimeout("Timeout")

    with pytest.raises(AmbiguousTimeoutError) as exc_info:
        coordinator.execute_with_resilience(
            role=RoleType.IMAGE,
            call_fn=timeout_api,
            is_billable=True,
            estimated_cost_cents=10,
            operation_id="op_timeout_123",
        )

    # Must NOT have retried 3 times blindly!
    assert call_count == 1
    assert exc_info.value.operation_id == "op_timeout_123"


def test_budget_checked_before_every_billable_attempt(db_session_factory, mock_router_and_registry):
    """INVARIANT: Budget limit must be checked and enforced before billable execution."""
    router, registry = mock_router_and_registry
    budget_service = BudgetService(db_session_factory, daily_limit_cents=50)

    # Exhaust budget beforehand using a prior job
    queue_service = QueueService(engine=db_session_factory.kw["bind"])
    prior_job = queue_service.enqueue_job(job_type="image_batch", operation_id="op_prior_01")
    target_job = queue_service.enqueue_job(job_type="image_batch", operation_id="op_target_01")

    # Reserve 45 cents on prior job (daily limit is 50 cents)
    budget_service.reserve_budget(job_id=prior_job.id, estimated_cost_cents=45)

    coordinator = ResilientExecutionCoordinator(
        router=router,
        registry=registry,
        budget_service=budget_service,
    )

    api_called = False

    def paid_generation_api(model_id: str):
        nonlocal api_called
        api_called = True
        return "paid_image"

    # Attempting an operation that needs 10 cents on target_job should fail with BudgetExceededError (45 + 10 = 55 > 50)
    with pytest.raises(BudgetExceededError):
        coordinator.execute_with_resilience(
            role=RoleType.IMAGE,
            call_fn=paid_generation_api,
            job_id=target_job.id,
            item_index=1,
            is_billable=True,
            estimated_cost_cents=10,
        )

    # API must NOT have been called!
    assert api_called is False


def test_exhaustion_marks_item_failed_and_continues_remaining_items(db_session_factory, mock_router_and_registry):
    """When all fallbacks for an item are exhausted, worker marks item failed and continues batch."""
    router, registry = mock_router_and_registry
    engine = db_session_factory.kw["bind"]
    queue_service = QueueService(engine=engine)
    tracker = ItemProgressTracker(session_factory=db_session_factory)
    worker = Worker(queue_service, worker_id="test-resilience-worker")

    job = queue_service.enqueue_job(job_type="image_batch", operation_id="op_exhaustion_test_01")
    job_id = job.id
    tracker.initialize_job_items(job_id=job_id, total_items=3, initial_stage="generating")

    coordinator = ResilientExecutionCoordinator(
        router=router,
        registry=registry,
        max_attempts_per_provider=1,
        sleeper=lambda s: None,
    )

    def resilient_item_processor(j_id: int, item_idx: int):
        def flaky_api(model_id: str):
            if item_idx == 1:
                # Item 1 always fails across all models with 500 error
                class Mock500(Exception):
                    status_code = 500
                    body = {"message": "Internal server error"}
                raise Mock500("Server Error")
            # Items 0 and 2 succeed
            return f"success_item_{item_idx}"

        coordinator.execute_with_resilience(
            role=RoleType.IMAGE,
            call_fn=flaky_api,
            job_id=j_id,
            item_index=item_idx,
        )

    # Run the worker over the batch
    claimed_id = worker.run_once(item_processor=resilient_item_processor)
    assert claimed_id == job_id

    # Verify job status in queue
    with db_session_factory() as session:
        persisted_job = session.get(Job, job_id)
        assert persisted_job is not None
        assert persisted_job.status == "completed_with_errors"

    # Verify per-item states
    tracker = ItemProgressTracker(db_session_factory)
    progress = tracker.get_batch_progress(job_id)
    assert progress.total_items == 3
    assert progress.completed_count == 2
    assert progress.failed_count == 1
    assert progress.pending_count == 0

    with db_session_factory() as session:
        items = session.query(JobItem).filter_by(job_id=job_id).order_by(JobItem.item_index).all()
        assert items[0].status == "failed"
        assert "Semua rute fallback" in items[0].last_error
        assert items[1].status == "completed"
        assert items[2].status == "completed"
