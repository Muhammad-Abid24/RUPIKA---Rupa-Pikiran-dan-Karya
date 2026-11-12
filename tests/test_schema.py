"""Acceptance tests for SF-002 Database schema and migrations."""
import os
from pathlib import Path
from alembic.config import Config
from alembic import command
import pytest
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import IntegrityError

from stockforge.db.models import (
    Asset,
    BudgetLedger,
    Concept,
    Job,
    JobItem,
    Niche,
    TelegramOutbox,
    TelegramUser,
)
from stockforge.db.session import check_sqlite_integrity, create_db_engine, get_session_factory


@pytest.fixture
def temp_db_path(tmp_path: Path) -> Path:
    """Fixture providing a temporary SQLite database file path."""
    db_file = tmp_path / "test_stockforge.db"
    return db_file


@pytest.fixture
def alembic_cfg(temp_db_path: Path) -> Config:
    """Construct Alembic Config targeting the temporary SQLite DB."""
    project_root = Path(__file__).parent.parent
    ini_path = project_root / "alembic.ini"
    cfg = Config(str(ini_path))
    cfg.set_main_option("script_location", str(project_root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{temp_db_path}")
    return cfg


def test_alembic_upgrade_and_downgrade(temp_db_path: Path, alembic_cfg: Config):
    """Verify clean upgrade to head, verify all tables, downgrade to base, and upgrade again."""
    # 1. Upgrade to head
    command.upgrade(alembic_cfg, "head")

    engine = create_db_engine(f"sqlite:///{temp_db_path}")
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())

    expected_tables = {
        "telegram_users",
        "ai_providers",
        "ai_models",
        "model_routes",
        "model_route_candidates",
        "provider_health",
        "niches",
        "concepts",
        "jobs",
        "assets",
        "job_items",
        "job_attempts",
        "job_stage_timings",
        "qc_results",
        "asset_metadata",
        "reviews",
        "ai_usage",
        "budget_ledger",
        "export_batches",
        "export_items",
        "submissions",
        "job_events",
        "telegram_outbox",
        "telegram_progress_messages",
        "research_settings",
        "research_runs",
        "research_candidates",
        "niche_selections",
        "production_plans",
        "alembic_version",
    }
    assert expected_tables.issubset(tables)

    # 2. Downgrade to base
    command.downgrade(alembic_cfg, "base")
    inspector = inspect(engine)
    remaining_tables = set(inspector.get_table_names())
    # All domain tables must be dropped
    assert "jobs" not in remaining_tables
    assert "telegram_users" not in remaining_tables

    # 3. Re-upgrade to head
    command.upgrade(alembic_cfg, "head")
    inspector = inspect(engine)
    recreated_tables = set(inspector.get_table_names())
    assert expected_tables.issubset(recreated_tables)


def test_sqlite_pragmas_and_integrity(temp_db_path: Path, alembic_cfg: Config):
    """Verify SQLite WAL mode, foreign keys, busy timeout, and integrity check."""
    command.upgrade(alembic_cfg, "head")
    engine = create_db_engine(f"sqlite:///{temp_db_path}")

    with engine.connect() as conn:
        fk_val = conn.execute(text("PRAGMA foreign_keys;")).scalar()
        assert fk_val == 1

        journal_mode = conn.execute(text("PRAGMA journal_mode;")).scalar()
        assert str(journal_mode).lower() == "wal"

        busy_timeout = conn.execute(text("PRAGMA busy_timeout;")).scalar()
        assert busy_timeout == 5000

    is_ok, msg = check_sqlite_integrity(engine)
    assert is_ok is True
    assert msg == "ok"


def test_foreign_key_enforcement(temp_db_path: Path, alembic_cfg: Config):
    """Verify SQLite foreign key enforcement raises IntegrityError on invalid parent keys."""
    command.upgrade(alembic_cfg, "head")
    engine = create_db_engine(f"sqlite:///{temp_db_path}")
    SessionFactory = get_session_factory(engine)

    # Attempt to insert Concept pointing to non-existent niche_id 999
    with pytest.raises(IntegrityError):
        with SessionFactory() as session:
            bad_concept = Concept(
                niche_id=999,
                title="Invalid Concept",
                prompt="Some prompt",
                fingerprint="fingerprint_bad_001",
            )
            session.add(bad_concept)
            session.commit()

    # Attempt to insert JobItem pointing to non-existent job_id 999
    with pytest.raises(IntegrityError):
        with SessionFactory() as session:
            bad_item = JobItem(
                job_id=999,
                item_index=1,
                stage="prompt",
                status="pending",
            )
            session.add(bad_item)
            session.commit()


def test_uniqueness_constraints(temp_db_path: Path, alembic_cfg: Config):
    """Verify unique constraints on operation_id, event_key, and (job_id, item_index)."""
    command.upgrade(alembic_cfg, "head")
    engine = create_db_engine(f"sqlite:///{temp_db_path}")
    SessionFactory = get_session_factory(engine)

    # 1. Duplicate telegram_outbox event_key
    with pytest.raises(IntegrityError):
        with SessionFactory() as session:
            ev1 = TelegramOutbox(chat_id=123, event_key="evt_unique_1", payload_json="{}")
            ev2 = TelegramOutbox(chat_id=123, event_key="evt_unique_1", payload_json="{}")
            session.add(ev1)
            session.add(ev2)
            session.commit()

    # 2. Duplicate jobs operation_id
    with pytest.raises(IntegrityError):
        with SessionFactory() as session:
            j1 = Job(type="image_batch", operation_id="op_unique_1")
            j2 = Job(type="image_batch", operation_id="op_unique_1")
            session.add(j1)
            session.add(j2)
            session.commit()

    # 3. Duplicate (job_id, item_index) in job_items
    with pytest.raises(IntegrityError):
        with SessionFactory() as session:
            valid_job = Job(type="image_batch", operation_id="op_unique_2")
            session.add(valid_job)
            session.flush()

            it1 = JobItem(job_id=valid_job.id, item_index=1, stage="prompt", status="pending")
            it2 = JobItem(job_id=valid_job.id, item_index=1, stage="prompt", status="pending")
            session.add(it1)
            session.add(it2)
            session.commit()


def test_currency_precision_in_ledger(temp_db_path: Path, alembic_cfg: Config):
    """Verify financial balances store exact integer cents without float drift."""
    command.upgrade(alembic_cfg, "head")
    engine = create_db_engine(f"sqlite:///{temp_db_path}")
    SessionFactory = get_session_factory(engine)

    with SessionFactory() as session:
        # $3.00 reserve = 300 cents
        # $1.49 actual = 149 cents
        # $1.51 release = 151 cents
        session.add(BudgetLedger(kind="reserve", amount_cents=300))
        session.add(BudgetLedger(kind="settle", amount_cents=-149))
        session.add(BudgetLedger(kind="release", amount_cents=-151))
        session.commit()

        # Sum ledger
        total_cents = session.scalar(select(func.sum(BudgetLedger.amount_cents)))
        assert total_cents == 0


def test_concurrency_and_wal_readers(temp_db_path: Path, alembic_cfg: Config):
    """Verify SQLite WAL allows non-blocking reads while a write transaction is open."""
    command.upgrade(alembic_cfg, "head")
    engine1 = create_db_engine(f"sqlite:///{temp_db_path}")
    engine2 = create_db_engine(f"sqlite:///{temp_db_path}")

    SessionFactory1 = get_session_factory(engine1)
    SessionFactory2 = get_session_factory(engine2)

    # Initial record
    with SessionFactory1() as s1:
        u = TelegramUser(telegram_id=9999, role="admin")
        s1.add(u)
        s1.commit()

    # Open transaction in s1 and add a new row without committing yet
    s1 = SessionFactory1()
    s1.add(TelegramUser(telegram_id=8888, role="reviewer"))
    s1.flush()

    # Concurrent reader s2 can read existing committed data without blocking
    with SessionFactory2() as s2:
        count = s2.execute(select(text("COUNT(*) FROM telegram_users"))).scalar()
        assert count == 1  # Only committed record visible

    # Now commit writer
    s1.commit()
    s1.close()

    # Verify reader sees both records
    with SessionFactory2() as s2:
        count = s2.execute(select(text("COUNT(*) FROM telegram_users"))).scalar()
        assert count == 2
