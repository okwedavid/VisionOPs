"""Alembic migration environment (ADR-004).

Migrations are the only sanctioned way to change the schema. Every revision must
have a working ``downgrade()`` while its major version is supported, and CI runs
upgrade → downgrade → upgrade on every change.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

logger = logging.getLogger(__name__)

#: This directory *is* the Alembic script location (it holds ``env.py`` and
#: ``versions/``), so it must not be suffixed again.
MIGRATIONS_DIR = Path(__file__).resolve().parent


def alembic_config(database_url: str) -> Config:
    """Build an Alembic config bound to this package's migration scripts."""
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", database_url)
    config.set_main_option("version_path_separator", "os")
    config.attributes["configure_logger"] = False
    return config


def run_migrations(database_url: str, revision: str = "head") -> None:
    """Apply migrations up to ``revision``."""
    config = alembic_config(database_url)
    try:
        command.upgrade(config, revision)
    except Exception as exc:
        logger.error("migration to %s failed: %s", revision, exc)
        raise


def current_revision(database_url: str) -> str | None:
    try:
        script = ScriptDirectory.from_config(alembic_config(database_url))
        return script.get_current_head()
    except Exception:
        return None


def downgrade_to(database_url: str, revision: str) -> None:
    command.downgrade(alembic_config(database_url), revision)


def stamp_head(database_url: str) -> None:
    command.stamp(alembic_config(database_url), "head")


def _unused(*_args: Any) -> None:  # pragma: no cover
    return None


__all__ = [
    "MIGRATIONS_DIR",
    "alembic_config",
    "current_revision",
    "downgrade_to",
    "run_migrations",
    "stamp_head",
]
