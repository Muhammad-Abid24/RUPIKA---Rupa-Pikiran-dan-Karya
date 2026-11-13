"""Research configuration, mode management, and spending limits (SF-020)."""
from __future__ import annotations

from datetime import datetime, time, timezone
import logging
from typing import Any, Callable, Dict, Optional
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from stockforge.db.models import ResearchRun, ResearchSetting, utc_now

logger = logging.getLogger("stockforge.research.settings")

DEFAULT_CRON = "0 8 * * 1"  # Every Monday at 08:00
DEFAULT_TIMEZONE = "Asia/Jakarta"
DEFAULT_MAX_NICHES = 5
DEFAULT_RESEARCH_DAILY_LIMIT_CENTS = 100  # $1.00 USD


class ResearchSettingsError(Exception):
    """Raised for invalid research settings configuration."""
    pass


class ResearchBudgetExceededError(Exception):
    """Raised when research operation would exceed separate research budget limit."""
    pass


class ResearchSettingsManager:
    """Manages persistent research configuration, modes, and spending caps."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self.session_factory = session_factory

    def get_or_create_settings(self, session: Session) -> ResearchSetting:
        """Fetch singleton research settings row (id=1) or initialize defaults."""
        setting = session.get(ResearchSetting, 1)
        if not setting:
            setting = ResearchSetting(
                id=1,
                mode="manual",
                auto_enabled=False,
                cron_expr=DEFAULT_CRON,
                timezone=DEFAULT_TIMEZONE,
                max_niches=DEFAULT_MAX_NICHES,
                research_daily_limit_cents=DEFAULT_RESEARCH_DAILY_LIMIT_CENTS,
                auto_paid_research_approved_by=None,
                auto_paid_research_approved_at=None,
                updated_by=None,
                updated_at=utc_now(),
            )
            session.add(setting)
            session.commit()
            session.refresh(setting)
        return setting

    def set_mode(self, mode: str, updated_by_user_id: int) -> ResearchSetting:
        """Switch between 'manual' and 'auto' research modes."""
        valid_modes = {"manual", "auto"}
        if mode not in valid_modes:
            raise ResearchSettingsError(f"Mode riset tidak valid: '{mode}'. Pilihan: {valid_modes}")

        with self.session_factory() as session:
            setting = self.get_or_create_settings(session)
            setting.mode = mode
            if mode == "manual":
                setting.auto_enabled = False
            setting.updated_by = updated_by_user_id
            setting.updated_at = utc_now()
            session.commit()
            session.refresh(setting)
            logger.info("Mode riset diubah menjadi '%s' oleh user #%s", mode, updated_by_user_id)
            return setting

    def configure_auto_schedule(
        self,
        cron_expr: str,
        timezone_str: str,
        max_niches: int,
        daily_limit_cents: int,
        approved_by_user_id: int,
        auto_enabled: bool = True,
    ) -> ResearchSetting:
        """Configure and explicitly opt-in to scheduled Auto Research."""
        if max_niches < 1 or max_niches > 20:
            raise ResearchSettingsError("max_niches harus bernilai antara 1 dan 20")
        if daily_limit_cents <= 0:
            raise ResearchSettingsError("research_daily_limit_cents harus lebih dari 0")

        now = utc_now()
        with self.session_factory() as session:
            setting = self.get_or_create_settings(session)
            setting.mode = "auto"
            setting.auto_enabled = auto_enabled
            setting.cron_expr = cron_expr.strip()
            setting.timezone = timezone_str.strip()
            setting.max_niches = max_niches
            setting.research_daily_limit_cents = daily_limit_cents
            setting.auto_paid_research_approved_by = approved_by_user_id
            setting.auto_paid_research_approved_at = now
            setting.updated_by = approved_by_user_id
            setting.updated_at = now
            session.commit()
            session.refresh(setting)
            logger.info(
                "Jadwal riset otomatis dikonfigurasi: cron='%s', tz='%s', limit=%d sen oleh user #%s",
                cron_expr,
                timezone_str,
                daily_limit_cents,
                approved_by_user_id,
            )
            return setting

    def check_and_reserve_research_budget(self, session: Session, estimated_cost_cents: int) -> None:
        """Enforce separate research daily spending limit before executing research calls."""
        if estimated_cost_cents <= 0:
            return

        setting = self.get_or_create_settings(session)
        limit_cents = setting.research_daily_limit_cents

        start_of_day = datetime.combine(utc_now().date(), time.min, tzinfo=timezone.utc)
        spent_today = session.scalar(
            select(func.coalesce(func.sum(ResearchRun.actual_cost_cents), 0)).where(
                ResearchRun.started_at >= start_of_day
            )
        ) or 0

        if spent_today + estimated_cost_cents > limit_cents:
            raise ResearchBudgetExceededError(
                f"Batas anggaran harian riset terlampaui! Batas: {limit_cents} sen, "
                f"Terpakai hari ini: {spent_today} sen, Diminta: {estimated_cost_cents} sen."
            )
