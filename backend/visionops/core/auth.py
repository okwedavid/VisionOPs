"""Authentication: API keys today, OIDC later behind the same interface (ADR-014).

The resolver is deliberately narrow. Handlers ask for a :class:`Principal` and
check permissions; they never know whether the caller authenticated with a key,
a session cookie or (Phase 7) an edge agent token.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

from fastapi import Request, WebSocket
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.scoped import Principal
from ..models import ApiKey
from .errors import Unauthorized
from .security import fingerprint, parse_api_key, verify_secret

logger = logging.getLogger(__name__)

#: ``last_used_at`` is touched at most this often, to keep a hot key from
#: turning every request into a write.
LAST_USED_THROTTLE_SECONDS = 60.0


class AuthProvider(Protocol):
    """Anything that can turn credentials into a principal."""

    async def resolve(self, credentials: str, session: Session) -> Principal | None: ...


def extract_credentials(request: Request | WebSocket) -> str | None:
    """Read the API key from ``Authorization: Bearer`` or ``X-API-Key``."""
    header = request.headers.get("authorization")
    if header:
        scheme, _, value = header.partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            return value.strip()
    api_key = request.headers.get("x-api-key")
    return api_key.strip() if api_key else None


class ApiKeyProvider:
    """Resolve ``vo_<id>_<secret>`` keys against the ``api_keys`` table."""

    def __init__(
        self,
        *,
        last_used_throttle: float = LAST_USED_THROTTLE_SECONDS,
        session_factory: Any = None,
    ) -> None:
        self._throttle = last_used_throttle
        # Only needed to attribute rejected credentials; ``resolve`` is handed
        # its session by the caller, but an audit lookup happens after the
        # request session has already been rolled back.
        self._session_factory = session_factory

    async def resolve(self, credentials: str, session: Session) -> Principal | None:
        parsed = parse_api_key(credentials)
        if parsed is None:
            return None
        key_id, secret = parsed
        statement = select(ApiKey).where(ApiKey.key_prefix == f"vo_{key_id}")
        record = session.execute(statement).scalar_one_or_none()
        if record is None or record.revoked:
            return None

        now = datetime.now(UTC)
        if record.expires_at is not None:
            expires = record.expires_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            if expires <= now:
                return None

        if not verify_secret(secret, record.key_salt, record.key_hash):
            return None

        self._touch(session, record, now)
        return Principal(
            organization_id=record.organization_id,
            actor_id=record.id,
            actor_type="api_key",
            permissions=_permissions(record.permissions),
            key_id=record.id,
            key_name=record.name,
            rate_limit_rps=record.rate_limit_rps,
        )

    def organization_for(self, credentials: str) -> str | None:
        """Which organisation a *presented* key prefix belongs to, if any.

        Used to attribute a rejected attempt in the audit trail. It answers only
        "is this prefix one of ours?", never "is this secret valid?" — a
        revoked or expired key still resolves here, which is exactly when an
        operator most wants to see the attempt recorded. Returns ``None`` for a
        prefix that matches no row, because there is no tenant to file it under.
        """
        parsed = parse_api_key(credentials)
        if parsed is None:
            return None
        key_id, _secret = parsed
        statement = select(ApiKey.organization_id).where(ApiKey.key_prefix == f"vo_{key_id}")
        try:
            with self._session_factory() as session:
                return session.execute(statement).scalar_one_or_none()
        except Exception:
            logger.debug("could not attribute a credential prefix", exc_info=True)
            return None

    def _touch(self, session: Session, record: ApiKey, now: datetime) -> None:
        last = record.last_used_at
        if last is not None:
            if last.tzinfo is None:
                last = last.replace(tzinfo=UTC)
            if (now - last).total_seconds() < self._throttle:
                return
        record.last_used_at = now
        session.commit()


def _permissions(stored: Sequence[str] | None) -> Sequence[str]:
    """The grants on a key row, deny-by-default.

    An absent or empty list grants *nothing*. It must never widen to ``"*"``:
    a key created with no scopes would otherwise be a full-privilege key, and
    ``/apikeys`` lets an operator mint such a key deliberately. The bootstrap
    key is given ``["*"]`` explicitly by ``bootstrap.ensure_bootstrap``.
    """
    return tuple(stored or ())


def rate_limit_key(credentials: str) -> str:
    """Stable per-key bucket id (never the key itself)."""
    return fingerprint(credentials)


async def resolve_principal(
    request: Request,
    session: Session,
    provider: ApiKeyProvider,
) -> Principal:
    """FastAPI dependency body: resolve or reject with a uniform 401."""
    credentials = extract_credentials(request)
    if not credentials:
        raise Unauthorized()
    principal = await provider.resolve(credentials, session)
    if principal is None:
        raise Unauthorized()
    return principal


__all__ = [
    "LAST_USED_THROTTLE_SECONDS",
    "ApiKeyProvider",
    "AuthProvider",
    "extract_credentials",
    "rate_limit_key",
    "resolve_principal",
]
