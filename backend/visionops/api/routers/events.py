"""Events: the durable record. List, detail, summary, schema and export."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Query, Response

from ...models import Event
from ...schemas.dto import EventOut, EventPage
from ..deps import ContainerDep, PrincipalDep

router = APIRouter(prefix="/events", tags=["events"])


@router.get("", response_model=EventPage, summary="List events")
def list_events(
    container: ContainerDep,
    principal: PrincipalDep,
    limit: int = Query(default=50, ge=1, le=200),
    camera_id: str | None = None,
    zone_id: str | None = None,
    type: str | None = None,
    severity: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    q: str | None = None,
    after: str | None = None,
) -> EventPage:
    """Newest-first, cursor-paginated, filterable.

    ``after`` is an opaque cursor from a previous page; use it instead of
    ``until`` when paging, so the page cannot shift while new events arrive.
    """
    before: datetime | None = until
    before_id: str | None = None
    if after:
        before, before_id = container.events.parse_cursor(after)
    rows, has_more = container.events.list(
        principal,
        limit=limit,
        camera_id=camera_id,
        zone_id=zone_id,
        event_type=type,
        severity=severity,
        since=since,
        until=until,
        search=q,
        before=before,
        before_id=before_id,
    )
    return EventPage(
        items=[_event_out(row) for row in rows],
        next_cursor=container.events.cursor_for(rows[-1]) if (has_more and rows) else None,
        has_more=has_more,
    )


@router.get("/summary", summary="Aggregates over a window")
def summary(
    container: ContainerDep,
    principal: PrincipalDep,
    since: datetime | None = None,
    until: datetime | None = None,
    camera_id: str | None = None,
    zone_id: str | None = None,
) -> dict[str, Any]:
    """Counts, dwell average and an hourly series. Defaults to the last 24 h."""
    now = datetime.now(UTC)
    window_since = since or now.replace(hour=0, minute=0, second=0, microsecond=0)
    window_until = until or now
    return container.events.summary(
        principal,
        since=window_since,
        until=window_until,
        camera_id=camera_id,
        zone_id=zone_id,
    )


@router.get("/export", summary="Export events")
def export_events(
    container: ContainerDep,
    principal: PrincipalDep,
    since: datetime | None = None,
    until: datetime | None = None,
    camera_id: str | None = None,
    zone_id: str | None = None,
    format: Literal["csv", "json"] = "csv",
) -> Response:
    """Stream a full export for a window (CSV or JSON).

    Streams rather than buffering so a year of events does not exhaust memory.
    """
    now = datetime.now(UTC)
    window_since = since or now.replace(hour=0, minute=0, second=0, microsecond=0)
    window_until = until or now
    kwargs: dict[str, Any] = {
        "since": window_since,
        "until": window_until,
        "camera_id": camera_id,
        "zone_id": zone_id,
    }
    if format == "json":
        body = container.events.export_json(principal, **kwargs)
        return Response(
            content=body,
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="visionops-events.json"'},
        )
    body = container.events.export_csv(principal, **kwargs)
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="visionops-events.csv"'},
    )


@router.get("/{event_id}", response_model=EventOut, summary="Get an event")
def get_event(event_id: str, container: ContainerDep, principal: PrincipalDep) -> EventOut:
    return _event_out(container.events.get(principal, event_id))


@router.get("/{event_id}/snapshot", summary="Event snapshot image")
def event_snapshot(event_id: str, container: ContainerDep, principal: PrincipalDep) -> Response:
    """Serve the stored snapshot, if snapshots are enabled for this deployment."""
    from ...core.errors import NotFound
    from ...services.snapshot import SnapshotWriter

    event = container.events.get(principal, event_id)
    if not event.snapshot_path:
        raise NotFound("snapshot", event_id)
    writer = SnapshotWriter(root=container.settings.snapshot_dir)
    try:
        path = writer.resolve(event.snapshot_path)
    except ValueError as exc:
        raise NotFound("snapshot", event_id) from exc
    if not path.exists():
        raise NotFound("snapshot", event_id)
    return Response(content=path.read_bytes(), media_type="image/jpeg")


def event_page(
    container: Any,
    principal: Any,
    *,
    camera_id: str | None = None,
    zone_id: str | None = None,
    limit: int = 50,
    before: datetime | None = None,
    before_id: str | None = None,
) -> EventPage:
    """Shared paginator used by the zone-scoped convenience endpoint."""
    rows, has_more = container.events.list(
        principal,
        limit=limit,
        camera_id=camera_id,
        zone_id=zone_id,
        before=before,
        before_id=before_id,
    )
    return EventPage(
        items=[_event_out(row) for row in rows],
        next_cursor=container.events.cursor_for(rows[-1]) if (has_more and rows) else None,
        has_more=has_more,
    )


def _event_out(row: Event) -> EventOut:
    return EventOut(
        id=row.id,
        schema_version=row.schema_version,
        type=row.type,
        severity=row.severity,  # type: ignore[arg-type]
        occurred_at=row.occurred_at,
        received_at=row.received_at,
        organization_id=row.organization_id,
        camera_id=row.camera_id,
        camera_name=row.camera_name,
        zone_id=row.zone_id,
        zone_name=row.zone_name,
        track_id=row.track_id,
        session_id=row.session_id,
        confidence=row.confidence,
        metadata=row.metadata_ or {},
        snapshot_path=row.snapshot_path,
    )


__all__ = ["event_page", "router"]
