"""SQLAlchemy 2.0 ORM models for StockForge (SF-002)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class TelegramUser(Base):
    __tablename__ = "telegram_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(Integer, unique=True, index=True, nullable=False)
    role: Mapped[str] = mapped_column(String(32), default="reviewer", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class AiProvider(Base):
    __tablename__ = "ai_providers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # text, image, vision
    base_url: Mapped[str] = mapped_column(String(255), nullable=False)
    secret_env: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    models: Mapped[List[AiModel]] = relationship("AiModel", back_populates="provider")


class AiModel(Base):
    __tablename__ = "ai_models"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider_id: Mapped[int] = mapped_column(Integer, ForeignKey("ai_providers.id", ondelete="CASCADE"), nullable=False)
    model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    capabilities_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    pricing_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    license_evidence: Mapped[str] = mapped_column(Text, default="", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    provider: Mapped[AiProvider] = relationship("AiProvider", back_populates="models")


class ModelRoute(Base):
    __tablename__ = "model_routes"

    role: Mapped[str] = mapped_column(String(32), primary_key=True)  # research, prompt, image, vision_qc, metadata
    primary_model_id: Mapped[int] = mapped_column(Integer, ForeignKey("ai_models.id"), nullable=False)
    fallback_model_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("ai_models.id"), nullable=True)


class ModelRouteCandidate(Base):
    __tablename__ = "model_route_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    model_id: Mapped[int] = mapped_column(Integer, ForeignKey("ai_models.id"), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (
        UniqueConstraint("role", "model_id", name="uq_model_route_role_model"),
    )


class ProviderHealth(Base):
    __tablename__ = "provider_health"

    provider_id: Mapped[int] = mapped_column(Integer, ForeignKey("ai_providers.id", ondelete="CASCADE"), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), default="healthy", nullable=False)  # healthy, cooldown, disabled
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cooldown_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class Niche(Base):
    __tablename__ = "niches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    source_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    concepts: Mapped[List[Concept]] = relationship("Concept", back_populates="niche")


class Concept(Base):
    __tablename__ = "concepts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    niche_id: Mapped[int] = mapped_column(Integer, ForeignKey("niches.id", ondelete="CASCADE"), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    research_sources_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    niche: Mapped[Niche] = relationship("Niche", back_populates="concepts")


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False)  # image_batch, research
    concept_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("concepts.id"), nullable=True)
    created_by: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("telegram_users.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    operation_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    lease_owner: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    reserved_usd_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)

    items: Mapped[List[JobItem]] = relationship("JobItem", back_populates="job", cascade="all, delete-orphan")


class JobItem(Base):
    __tablename__ = "job_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    item_index: Mapped[int] = mapped_column(Integer, nullable=False)
    stage: Mapped[str] = mapped_column(String(32), default="prompt", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    provider: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    asset_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("assets.id"), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)

    job: Mapped[Job] = relationship("Job", back_populates="items")

    __table_args__ = (
        UniqueConstraint("job_id", "item_index", name="uq_job_item_index"),
    )


class JobAttempt(Base):
    __tablename__ = "job_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    job_item_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("job_items.id"), nullable=True)
    attempt_number: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    request_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    operation_id: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)
    http_status: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    normalized_error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    provider_error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    retry_after: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    estimated_cost_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    actual_cost_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class JobStageTiming(Base):
    __tablename__ = "job_stage_timings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    item_index: Mapped[int] = mapped_column(Integer, nullable=False)
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    duration_ms: Mapped[float] = mapped_column(Float, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    is_retry: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_fallback: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("jobs.id"), nullable=True)
    filename: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    raw_path: Mapped[str] = mapped_column(String(512), nullable=False)
    final_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    phash: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(64), default="image/jpeg", nullable=False)
    ai_generated: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    license_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    license_evidence: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="generated", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    qc_results: Mapped[List[QcResult]] = relationship("QcResult", back_populates="asset", cascade="all, delete-orphan")
    metadata_entry: Mapped[Optional[AssetMetadata]] = relationship("AssetMetadata", back_populates="asset", uselist=False, cascade="all, delete-orphan")


class QcResult(Base):
    __tablename__ = "qc_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_id: Mapped[int] = mapped_column(Integer, ForeignKey("assets.id", ondelete="CASCADE"), nullable=False)
    check_name: Mapped[str] = mapped_column(String(64), nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    details_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    asset: Mapped[Asset] = relationship("Asset", back_populates="qc_results")


class AssetMetadata(Base):
    __tablename__ = "asset_metadata"

    asset_id: Mapped[int] = mapped_column(Integer, ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    keywords_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    category_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    content_type: Mapped[str] = mapped_column(String(32), default="photo", nullable=False)
    metadata_reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    asset: Mapped[Asset] = relationship("Asset", back_populates="metadata_entry")


class Review(Base):
    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_id: Mapped[int] = mapped_column(Integer, ForeignKey("assets.id", ondelete="CASCADE"), nullable=False)
    telegram_user_id: Mapped[int] = mapped_column(Integer, ForeignKey("telegram_users.id"), nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class AiUsage(Base):
    __tablename__ = "ai_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("jobs.id"), nullable=True)
    model_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("ai_models.id"), nullable=True)
    operation_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    estimated_usd_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    actual_usd_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    provider_request_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="success", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class BudgetLedger(Base):
    __tablename__ = "budget_ledger"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("jobs.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # reserve, settle, release
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)  # exact integer cents
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class ExportBatch(Base):
    __tablename__ = "export_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_by: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("telegram_users.id"), nullable=True)
    archive_path: Mapped[str] = mapped_column(String(512), nullable=False)
    manifest_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class ExportItem(Base):
    __tablename__ = "export_items"

    batch_id: Mapped[int] = mapped_column(Integer, ForeignKey("export_batches.id", ondelete="CASCADE"), primary_key=True)
    asset_id: Mapped[int] = mapped_column(Integer, ForeignKey("assets.id"), primary_key=True)


class Submission(Base):
    __tablename__ = "submissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_id: Mapped[int] = mapped_column(Integer, ForeignKey("assets.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    adobe_asset_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class JobEvent(Base):
    __tablename__ = "job_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class TelegramOutbox(Base):
    __tablename__ = "telegram_outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(Integer, nullable=False)
    event_key: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class TelegramProgressMessage(Base):
    __tablename__ = "telegram_progress_messages"

    job_id: Mapped[int] = mapped_column(Integer, ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True)
    chat_id: Mapped[int] = mapped_column(Integer, nullable=False)
    message_id: Mapped[int] = mapped_column(Integer, nullable=False)
    last_render_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    last_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class ResearchSetting(Base):
    __tablename__ = "research_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    mode: Mapped[str] = mapped_column(String(32), default="manual", nullable=False)
    auto_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    cron_expr: Mapped[str] = mapped_column(String(64), default="0 8 * * 1", nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Jakarta", nullable=False)
    max_niches: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    research_daily_limit_cents: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    auto_paid_research_approved_by: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("telegram_users.id"), nullable=True)
    auto_paid_research_approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_by: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("telegram_users.id"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class ResearchRun(Base):
    __tablename__ = "research_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trigger: Mapped[str] = mapped_column(String(32), default="manual", nullable=False)  # manual, scheduled
    schedule_key: Mapped[Optional[str]] = mapped_column(String(128), unique=True, index=True, nullable=True)
    topic: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    estimated_cost_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    actual_cost_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_by: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("telegram_users.id"), nullable=True)

    candidates: Mapped[List[ResearchCandidate]] = relationship("ResearchCandidate", back_populates="research_run", cascade="all, delete-orphan")


class ResearchCandidate(Base):
    __tablename__ = "research_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    research_run_id: Mapped[int] = mapped_column(Integer, ForeignKey("research_runs.id", ondelete="CASCADE"), nullable=False)
    niche_name: Mapped[str] = mapped_column(String(128), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    score_method: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    evidence_quality: Mapped[str] = mapped_column(String(32), default="unverified", nullable=False)
    sources_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="proposed", nullable=False)

    research_run: Mapped[ResearchRun] = relationship("ResearchRun", back_populates="candidates")


class NicheSelection(Base):
    __tablename__ = "niche_selections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    research_run_id: Mapped[int] = mapped_column(Integer, ForeignKey("research_runs.id", ondelete="CASCADE"), nullable=False)
    candidate_id: Mapped[int] = mapped_column(Integer, ForeignKey("research_candidates.id", ondelete="CASCADE"), nullable=False)
    telegram_user_id: Mapped[int] = mapped_column(Integer, ForeignKey("telegram_users.id"), nullable=False)
    decision: Mapped[str] = mapped_column(String(32), default="selected", nullable=False)
    selected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("research_run_id", "candidate_id", "telegram_user_id", name="uq_niche_selection"),
    )


class ProductionPlan(Base):
    __tablename__ = "production_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    research_run_id: Mapped[int] = mapped_column(Integer, ForeignKey("research_runs.id", ondelete="CASCADE"), nullable=False)
    selected_niches_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    concepts_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    image_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    provider_routes_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    estimated_max_cost_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    plan_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)  # draft, awaiting_approval, approved, consumed, expired, revoked
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    approved_by: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("telegram_users.id"), nullable=True)
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed_job_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("jobs.id"), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
