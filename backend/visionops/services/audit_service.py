"""Append-only audit logging.

There is deliberately no update or delete path: the table is a security trail,
and a trail that can be edited is not a trail. ``detail`` is sanitised before it
is written so a secret can never reach it.
"""

from __future__ import annotations

import logging
from typing import Any

from ..core.ids import new_id
from ..db.base import utcnow
from ..models import AuditLog

logger = logging.getLogger(__name__)

#: Keys never written into an audit detail, at any nesting depth.
FORBIDDEN_DETAIL_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "secret",
        "token",
        "api_key",
        "credential",
        "credential_blob",
        "credentials",
        "master_key",
        "authorization",
    }
)

_MAX_DETAIL_DEPTH = 4


def sanitize(value: Any, *, depth: int = 0) -> Any:
    """Recursively strip secret-looking keys and truncate long strings."""
    if depth > _MAX_DETAIL_DEPTH:
        return "…"
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            if str(key).lower() in FORBIDDEN_DETAIL_KEYS:
                continue
            clean[str(key)] = sanitize(item, depth=depth + 1)
        return clean
    if isinstance(value, (list, tuple)):
        return [sanitize(item, depth=depth + 1) for item in value[:50]]
    if isinstance(value, str):
        return value[:500]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:500]


class AuditService:
    """Writes audit rows. Constructed with a session factory per call site."""

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    def record(
        self,
        organization_id: str,
        *,
        action: str,
        actor_type: str = "system",
        actor_id: str = "system",
        target_type: str | None = None,
        target_id: str | None = None,
        outcome: str = "success",
        ip: str | None = None,
        user_agent: str | None = None,
        request_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Append one audit row.

        Audit failures must never break the request they describe, so exceptions
        are logged and swallowed — with a loud warning, because a silently
        missing audit row is itself an incident.
        """
        row = AuditLog(
            id=new_id(),
            organization_id=organization_id,
            actor_type=actor_type,
            actor_id=str(actor_id)[:64],
            action=action[:64],
            target_type=target_type,
            target_id=str(target_id)[:64] if target_id else None,
            outcome=outcome,
            ip=ip[:64] if ip else None,
            user_agent=user_agent[:256] if user_agent else None,
            request_id=request_id[:64] if request_id else None,
            detail=sanitize(detail or {}),
            created_at=utcnow(),
        )
        try:
            with self._session_factory() as session:
                session.add(row)
                session.commit()
        except Exception:
            logger.warning(
                "failed to write audit row action=%s target=%s", action, target_id, exc_info=True
            )


__all__ = ["FORBIDDEN_DETAIL_KEYS", "AuditService", "sanitize"]
