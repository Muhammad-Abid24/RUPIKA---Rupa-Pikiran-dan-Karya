"""Application configuration and environment settings for StockForge (SF-001)."""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """StockForge runtime configuration loaded from environment or .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = Field(default="development", alias="APP_ENV")
    tz: str = Field(default="Asia/Jakarta", alias="TZ")
    database_url: str = Field(default="sqlite:////data/stockforge.db", alias="DATABASE_URL")
    data_dir: str = Field(default="/data", alias="DATA_DIR")

    telegram_bot_token: Optional[SecretStr] = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    telegram_admin_ids: str = Field(default="", alias="TELEGRAM_ADMIN_IDS")

    # Providers credentials & models
    research_base_url: Optional[str] = Field(default=None, alias="RESEARCH_BASE_URL")
    research_api_key: Optional[SecretStr] = Field(default=None, alias="RESEARCH_API_KEY")
    research_model: Optional[str] = Field(default=None, alias="RESEARCH_MODEL")

    prompt_base_url: Optional[str] = Field(default=None, alias="PROMPT_BASE_URL")
    prompt_api_key: Optional[SecretStr] = Field(default=None, alias="PROMPT_API_KEY")
    prompt_model: Optional[str] = Field(default=None, alias="PROMPT_MODEL")

    image_base_url: Optional[str] = Field(default=None, alias="IMAGE_BASE_URL")
    image_api_key: Optional[SecretStr] = Field(default=None, alias="IMAGE_API_KEY")
    image_model: Optional[str] = Field(default=None, alias="IMAGE_MODEL")

    vision_qc_base_url: Optional[str] = Field(default=None, alias="VISION_QC_BASE_URL")
    vision_qc_api_key: Optional[SecretStr] = Field(default=None, alias="VISION_QC_API_KEY")
    vision_qc_model: Optional[str] = Field(default=None, alias="VISION_QC_MODEL")

    metadata_base_url: Optional[str] = Field(default=None, alias="METADATA_BASE_URL")
    metadata_api_key: Optional[SecretStr] = Field(default=None, alias="METADATA_API_KEY")
    metadata_model: Optional[str] = Field(default=None, alias="METADATA_MODEL")

    # Queue & Concurrency
    max_concurrent_jobs: int = Field(default=1, ge=1, alias="MAX_CONCURRENT_JOBS")
    max_concurrent_images: int = Field(default=1, ge=1, alias="MAX_CONCURRENT_IMAGES")
    retry_max_attempts_per_provider: int = Field(default=3, ge=1, alias="RETRY_MAX_ATTEMPTS_PER_PROVIDER")
    telegram_progress_min_interval_seconds: int = Field(
        default=5, ge=1, alias="TELEGRAM_PROGRESS_MIN_INTERVAL_SECONDS"
    )
    enable_provider_fallback: bool = Field(default=True, alias="ENABLE_PROVIDER_FALLBACK")

    # Caps & Budgets
    max_images_per_batch: int = Field(default=10, ge=1, le=50, alias="MAX_IMAGES_PER_BATCH")
    max_images_per_day: int = Field(default=30, ge=1, alias="MAX_IMAGES_PER_DAY")
    daily_budget_usd: float = Field(default=3.00, ge=0.0, alias="DAILY_BUDGET_USD")
    monthly_budget_usd: float = Field(default=50.00, ge=0.0, alias="MONTHLY_BUDGET_USD")

    # Image Resolution Standards (Commercial Microstock)
    min_image_megapixels: float = Field(default=6.0, ge=1.0, alias="MIN_IMAGE_MEGAPIXELS")
    target_image_megapixels: float = Field(default=8.0, ge=1.0, alias="TARGET_IMAGE_MEGAPIXELS")

    # Anti-plagiarism & Deduplication Rules
    enforce_anti_plagiarism: bool = Field(default=True, alias="ENFORCE_ANTI_PLAGIARISM")
    enforce_deduplication: bool = Field(default=True, alias="ENFORCE_DEDUPLICATION")
    max_phash_distance: int = Field(default=5, ge=0, le=64, alias="MAX_PHASH_DISTANCE")

    # Hard Safety Invariants
    require_human_approval: bool = Field(default=True, alias="REQUIRE_HUMAN_APPROVAL")
    allow_adobe_auto_submission: bool = Field(default=False, alias="ALLOW_ADOBE_AUTO_SUBMISSION")

    @field_validator("allow_adobe_auto_submission")
    @classmethod
    def validate_never_auto_submit(cls, v: bool) -> bool:
        if v is True:
            raise ValueError(
                "Invarian keamanan dilanggar: ALLOW_ADOBE_AUTO_SUBMISSION tidak boleh bernilai True. "
                "Pengunggahan dan pengungkapan ke Adobe Stock harus dilakukan secara manual."
            )
        return False

    @property
    def admin_ids_list(self) -> List[int]:
        """Parse comma-separated admin IDs into numeric list."""
        if not self.telegram_admin_ids.strip():
            return []
        ids: List[int] = []
        for part in self.telegram_admin_ids.split(","):
            part_str = part.strip()
            if part_str:
                try:
                    ids.append(int(part_str))
                except ValueError:
                    pass
        return ids

    @property
    def admin_ids(self) -> List[int]:
        """Backward-compatible alias for admin_ids_list."""
        return self.admin_ids_list

    @property
    def resolved_data_dir(self) -> Path:
        """Resolve path to data directory."""
        return Path(self.data_dir).resolve()

    @property
    def resolved_database_path(self) -> Path:
        """Resolve filesystem path for SQLite database."""
        db_url = self.database_url
        if db_url.startswith("sqlite:///"):
            path_str = db_url.replace("sqlite:///", "")
            return Path(path_str).resolve()
        return (self.resolved_data_dir / "stockforge.db").resolve()


def get_settings() -> Settings:
    """Instantiate and return settings singleton/instance."""
    return Settings()

