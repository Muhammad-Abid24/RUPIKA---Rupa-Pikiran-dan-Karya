"""Database engine, session management, and SQLite pragmas (SF-002)."""
from __future__ import annotations

import logging
from typing import Generator, Optional, Tuple
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from stockforge.config import get_settings

logger = logging.getLogger("stockforge.db")


def create_db_engine(db_url: Optional[str] = None, echo: bool = False) -> Engine:
    """Create SQLAlchemy engine configured with WAL, foreign keys, and busy timeout."""
    url = db_url or get_settings().database_url

    engine = create_engine(
        url,
        echo=echo,
        future=True,
    )

    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def configure_sqlite_connection(dbapi_connection, connection_record) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys = ON;")
                cursor.execute("PRAGMA journal_mode = WAL;")
                cursor.execute("PRAGMA busy_timeout = 5000;")
                cursor.execute("PRAGMA synchronous = NORMAL;")
            finally:
                cursor.close()

    return engine


def check_sqlite_integrity(engine: Engine) -> Tuple[bool, str]:
    """Execute PRAGMA integrity_check on SQLite database."""
    with engine.connect() as conn:
        result = conn.execute(text("PRAGMA integrity_check;")).fetchall()
        messages = [row[0] for row in result]
        is_ok = len(messages) == 1 and messages[0] == "ok"
        return is_ok, "; ".join(messages)


def get_session_factory(engine: Optional[Engine] = None) -> sessionmaker[Session]:
    """Construct configured sessionmaker bound to engine."""
    eng = engine or create_db_engine()
    return sessionmaker(bind=eng, autoflush=False, autocommit=False, expire_on_commit=False)

