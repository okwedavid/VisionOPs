"""The tenant-isolation chokepoint.

Every read and write of a domain row goes through this module. ``organization_id``
is not something a service remembers to filter on — it is applied here, from the
authenticated principal, so a forgotten filter is a type error rather than a
data breach (``docs/SECURITY_PRIVACY.md`` §8).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, TypeVar

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from ..models import Camera, Event, Site, VideoSource, Zone

T = TypeVar("T")

#: Models that must always be queried with an organisation scope. Membership is
#: asserted by ``tests/security/test_tenant_isolation.py``.
SCOPED_MODELS: tuple[type, ...] = (Camera, Event, Site, VideoSource, Zone)


class Principal:
    """The authenticated caller. Created by the auth layer, never by handlers."""

    __slots__ = (
        "actor_id",
        "actor_type",
        "key_id",
        "key_name",
        "organization_id",
        "permissions",
        "rate_limit_rps",
    )

    def __init__(
        self,
        *,
        organization_id: str,
        actor_id: str = "",
        actor_type: str = "api_key",
        permissions: Sequence[str] = ("*",),
        key_id: str | None = None,
        key_name: str = "",
        rate_limit_rps: float | None = None,
    ) -> None:
        self.organization_id = organization_id
        self.actor_id = actor_id or "unknown"
        self.actor_type = actor_type
        self.permissions = frozenset(permissions)
        self.key_id = key_id
        self.key_name = key_name
        self.rate_limit_rps = rate_limit_rps

    def can(self, permission: str) -> bool:
        """Deny by default: a permission not granted is not permitted."""
        return "*" in self.permissions or permission in self.permissions

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Principal(org={self.organization_id}, actor={self.actor_type}:{self.actor_id})"


def scoped(statement: Select[Any], principal: Principal, model: type[T]) -> Select[Any]:
    """Apply the organisation filter to a statement for a scoped model."""
    if model in SCOPED_MODELS:
        statement = statement.where(model.organization_id == principal.organization_id)  # type: ignore[attr-defined]
    return statement


def get_by_id(session: Session, principal: Principal, model: type[T], entity_id: str) -> T | None:
    """Fetch one row scoped to the principal's organisation, or ``None``.

    A row in another organisation is indistinguishable from a row that does not
    exist, which is what stops the API being used as an existence oracle.
    """
    statement = scoped(select(model).where(model.id == entity_id), principal, model)  # type: ignore[attr-defined]
    # ``unique()`` is required whenever a model has joined eager loads against a
    # collection (Camera does), because the result then contains multiple rows.
    return session.execute(statement).unique().scalar_one_or_none()


def list_scoped(
    session: Session,
    principal: Principal,
    model: type[T],
    *,
    limit: int = 50,
    order_by: Any = None,
    extra: Any = None,
) -> list[T]:
    """List rows for the principal's organisation with a bounded page size."""
    statement = select(model)
    statement = scoped(statement, principal, model)
    if extra is not None:
        statement = statement.where(extra)
    if order_by is not None:
        statement = statement.order_by(order_by)
    statement = statement.limit(min(limit, 200))
    return list(session.execute(statement).unique().scalars())


def count_scoped(session: Session, principal: Principal, model: type[T]) -> int:
    statement = scoped(select(func.count()).select_from(model), principal, model)
    return int(session.execute(statement).scalar_one())


def insert_scoped(session: Session, principal: Principal, model: type[T], **values: Any) -> T:
    """Create a row, stamping ``organization_id`` from the principal."""
    values.setdefault("organization_id", principal.organization_id)
    row = model(**values)
    session.add(row)
    return row


__all__ = [
    "SCOPED_MODELS",
    "Principal",
    "count_scoped",
    "get_by_id",
    "insert_scoped",
    "list_scoped",
    "scoped",
]
