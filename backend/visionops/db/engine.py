"""Engine, session factory and dialect setup (ADR-005).

SQLite is the default and a first-class deployment: WAL mode plus a busy
timeout means a small deployment needs no database server at all, while the
same models and queries run on PostgreSQL unchanged.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .base import Base

logger = logging.getLogger(__name__)

#: How long SQLite waits for a lock before raising (``database is locked``).
SQLITE_BUSY_TIMEOUT_MS = 5000


def _sqlite_path(url: str) -> Path | None:
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        return None
    raw = url[len(prefix) :]
    if raw in ("", ":memory:"):
        return None
    return Path(raw)


def build_engine(database_url: str, *, echo: bool = False) -> Engine:
    """Create an engine with the settings each dialect needs."""
    connect_args: dict[str, object] = {}
    if database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        # We manage transactions explicitly per operation; a long-lived read
        # connection would otherwise see a stale snapshot.
        connect_args["timeout"] = SQLITE_BUSY_TIMEOUT_MS / 1000.0
        path = _sqlite_path(database_url)
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(
        database_url,
        echo=echo,
        future=True,
        connect_args=connect_args,
        pool_pre_ping=True,
    )

    if database_url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _configure_sqlite(dbapi_connection: object, _record: object) -> None:
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            # foreign_keys is off by default in SQLite; the schema depends on it.
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
            cursor.close()

    return engine


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Transactional scope: commit on success, roll back on any exception."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_all(engine: Engine) -> None:
    """Create the schema directly. Production uses Alembic; this is for tests."""
    # Importing the models registers them on ``Base.metadata``.
    from .. import models  # noqa: F401  (side-effect import)

    Base.metadata.create_all(engine)


def database_dialect(engine: Engine) -> str:
    return engine.dialect.name


__all__ = [
    "SQLITE_BUSY_TIMEOUT_MS",
    "Base",
    "build_engine",
    "build_session_factory",
    "create_all",
    "database_dialect",
    "session_scope",
]
