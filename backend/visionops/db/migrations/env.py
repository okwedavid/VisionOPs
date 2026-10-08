"""Alembic environment: reads the target URL from the VisionOps settings.

The URL is not taken from ``alembic.ini`` (there is no such file) so that
configuration lives in exactly one place: the environment / ``.env``.
"""

from __future__ import annotations

import contextlib
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make the application importable when Alembic is invoked from the repo root.
_ROOT = Path(__file__).resolve().parents[4]
if str(_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(_ROOT / "backend"))

from visionops.config import get_settings  # noqa: E402
from visionops.db.base import Base  # noqa: E402

config = context.config
if config.config_file_name is not None:  # pragma: no cover - optional ini
    # A broken/unreadable ini must not abort migrations; Alembic's own
    # ``-x``/``-n`` flags and the env vars are the supported configuration path.
    with contextlib.suppress(Exception):
        fileConfig(config.config_file_name)

settings = get_settings()
# Prefer the URL the caller configured (visionops.cli / visionops.main pass it
# explicitly). Falling back to the process settings keeps `alembic upgrade` usable
# from the command line, but it must never override an explicit choice — doing so
# silently migrated the wrong database.
_configured_url = config.get_main_option("sqlalchemy.url")
if not _configured_url or _configured_url.startswith("driver://"):
    _configured_url = settings.database_url
config.set_main_option("sqlalchemy.url", _configured_url.replace("%", "%%"))
database_url = _configured_url

target_metadata = Base.metadata


def _import_models() -> None:
    from visionops import models  # noqa: F401  (registers tables on the metadata)


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting (useful for review)."""
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=database_url.startswith("sqlite"),
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect and apply migrations."""
    _import_models()
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=database_url.startswith("sqlite"),
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
