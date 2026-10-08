"""First-run bootstrap: the organisation, its first API key, and migrations.

Kept separate from :mod:`visionops.main` so the CLI can run it without starting
the web application. Idempotent: running it twice is safe.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from .config import Settings
from .core.ids import new_id
from .core.security import generate_api_key, hash_secret, secure_write
from .db.engine import build_engine, build_session_factory
from .db.migrations import run_migrations
from .models import ApiKey, Organization

logger = logging.getLogger(__name__)

DEFAULT_ORGANIZATION_NAME = "VisionOps"
DEFAULT_ORGANIZATION_SLUG = "default"
DEFAULT_TIMEZONE = "UTC"


def ensure_bootstrap(settings: Settings) -> tuple[str, str]:
    """Ensure the schema, one organisation and at least one usable API key.

    Returns ``(organization_id, organization_name)``. The bootstrap key from the
    environment is honoured **only** when no keys exist yet, so a stale env var
    cannot silently re-grant access after rotation.
    """
    settings.ensure_directories()
    if settings.auto_migrate:
        run_migrations(settings.database_url)

    engine = build_engine(settings.database_url)
    session_factory = build_session_factory(engine)
    try:
        with session_factory() as session:
            organization = session.execute(
                select(Organization).where(Organization.slug == DEFAULT_ORGANIZATION_SLUG)
            ).scalar_one_or_none()
            if organization is None:
                organization = Organization(
                    id=new_id(),
                    name=DEFAULT_ORGANIZATION_NAME,
                    slug=DEFAULT_ORGANIZATION_SLUG,
                    timezone=DEFAULT_TIMEZONE,
                    retention_days=settings.retention_days,
                    snapshots_enabled=False,
                )
                session.add(organization)
                session.commit()
                logger.info("created organisation %s", organization.name)

            key_count = int(
                session.execute(
                    select(func.count()).select_from(ApiKey).where(ApiKey.revoked.is_(False))
                ).scalar_one()
            )
            if key_count == 0:
                _create_bootstrap_key(session_factory, session, organization.id, settings)
            return organization.id, organization.name
    finally:
        engine.dispose()


def _create_bootstrap_key(
    session_factory: Any,
    session: Any,
    organization_id: str,
    settings: Settings,
) -> None:
    """Create the first key, using ``VISIONOPS_API_KEY`` when it looks valid."""
    candidate = (settings.api_key or "").strip()
    from .core.security import parse_api_key

    record = None
    if candidate and parse_api_key(candidate) is not None:
        key_id, secret = parse_api_key(candidate)  # type: ignore[misc]
        from .core.security import generate_api_key as _unused  # noqa: F401

        salt = _derive_salt(candidate)
        record = ApiKey(
            id=new_id(),
            organization_id=organization_id,
            name="bootstrap",
            key_prefix=f"vo_{key_id}",
            key_hash=hash_secret(secret, salt),
            key_salt=salt,
            permissions=["*"],
        )
        logger.info("registered the bootstrap API key from the environment")
    else:
        generated = generate_api_key()
        record = ApiKey(
            id=new_id(),
            organization_id=organization_id,
            name="bootstrap",
            key_prefix=generated.key_prefix,
            key_hash=hash_secret(generated.secret, generated.salt),
            key_salt=generated.salt,
            permissions=["*"],
        )
        _write_key_to_env_file(generated.full_key, settings)
        logger.warning(
            "generated a new bootstrap API key; it is written to .env with 0600 permissions "
            "and cannot be retrieved later"
        )
    session.add(record)
    session.commit()


def _derive_salt(candidate: str) -> str:
    """A stable per-key salt derived from the key material itself.

    Deterministic so a restart with the same ``VISIONOPS_API_KEY`` does not
    invalidate the stored hash. Still a fast hash (ADR-014): the secret is 256
    bits of randomness, so there is nothing to brute-force.
    """
    import hashlib

    return hashlib.sha256(f"visionops-salt:{candidate}".encode()).hexdigest()[:32]


def _write_key_to_env_file(key: str, settings: Settings) -> None:
    """Append the generated key to ``.env`` with owner-only permissions."""
    env_path = Path(".env")
    line = f"VISIONOPS_API_KEY={key}"
    try:
        if env_path.exists():
            content = env_path.read_text(encoding="utf-8")
            if "VISIONOPS_API_KEY=" in content:
                return
            env_path.write_text(content.rstrip("\n") + f"\n{line}\n", encoding="utf-8")
        else:
            env_path.write_text(line + "\n", encoding="utf-8")
        secure_write(env_path, env_path.read_text(encoding="utf-8"))
    except OSError as exc:  # pragma: no cover - depends on the filesystem
        logger.error("could not persist the generated API key: %s", exc)
        logger.error("create a key manually with: visionops key add")


__all__ = [
    "DEFAULT_ORGANIZATION_NAME",
    "DEFAULT_ORGANIZATION_SLUG",
    "ensure_bootstrap",
]
