"""Event persistence, querying, aggregation and export.

The event log is the system of record. Everything else — the dashboard, reports,
future alerts and the AI assistant — is a projection of it.
"""

from __future__ import annotations

import builtins
import csv
import io
import json
import logging
import time
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Float, Select, and_, cast, func, or_, select

from ..core.errors import NotFound, ValidationError
from ..core.ids import new_id
from ..db.base import utcnow
from ..db.scoped import Principal
from ..models import Event
from ..observability.metrics import METRIC_EVENT_WRITE_LATENCY, registry
from ..vision.contracts import EventRecord

logger = logging.getLogger(__name__)

#: Widest window the on-demand summary will compute in one query.
MAX_SUMMARY_DAYS = 31
#: Refuse to build a CSV larger than this in memory; the export streams instead.
MAX_EXPORT_ROWS = 500_000
EXPORT_COLUMNS: tuple[str, ...] = (
    "id",
    "occurred_at",
    "received_at",
    "type",
    "severity",
    "camera_name",
    "zone_name",
    "track_id",
    "confidence",
    "metadata",
)


def _as_utc(value: datetime) -> datetime:
    """SQLite round-trips naive datetimes; everything in memory is UTC-aware."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class EventService:
    """Use-cases over the event log."""

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    # -- writing ------------------------------------------------------------

    def record(
        self,
        principal: Principal,
        records: Sequence[EventRecord],
        *,
        retry_attempts: int = 3,
    ) -> list[Event]:
        """Persist events. Durable *before* broadcast, by contract.

        Returns the rows that were written; a row that fails after
        ``retry_attempts`` is counted as dropped and logged loudly, because a
        silently missing event is worse than a visible gap.
        """
        if not records:
            return []
        rows: list[Event] = []
        started = time.perf_counter()
        for record in records:
            rows.append(
                Event(
                    id=record.id or new_id(),
                    schema_version=record.schema_version,
                    organization_id=principal.organization_id,
                    type=record.type,
                    severity=str(record.severity_value()),
                    occurred_at=_as_utc(datetime.fromtimestamp(record.occurred_at, UTC)),
                    received_at=utcnow(),
                    camera_id=record.camera_id,
                    camera_name=record.camera_name or "",
                    zone_id=record.zone_id,
                    zone_name=record.zone_name,
                    track_id=record.track_id,
                    session_id=record.session_id,
                    confidence=record.confidence,
                    metadata_=record.metadata,
                    snapshot_path=record.snapshot_path,
                )
            )

        last_error: Exception | None = None
        for attempt in range(1, retry_attempts + 1):
            try:
                with self._session_factory() as session:
                    session.add_all(rows)
                    session.commit()
                registry.observe_ms(
                    METRIC_EVENT_WRITE_LATENCY, (time.perf_counter() - started) * 1000.0
                )
                for row in rows:
                    registry.counter(
                        "visionops_events_total", 1, type=row.type, severity=row.severity
                    )
                return rows
            except Exception as exc:
                last_error = exc
                logger.warning("event write attempt %d/%d failed: %s", attempt, retry_attempts, exc)
                if attempt < retry_attempts:
                    time.sleep(0.05 * attempt)

        registry.counter("visionops_events_dropped_total", len(rows), reason="db_error")
        logger.error(
            "dropped %d events after %d attempts: %s", len(rows), retry_attempts, last_error
        )
        return []

    # -- reading ------------------------------------------------------------

    def _base_query(self, principal: Principal) -> Select[Any]:
        return select(Event).where(Event.organization_id == principal.organization_id)

    def list(
        self,
        principal: Principal,
        *,
        limit: int = 50,
        camera_id: str | None = None,
        zone_id: str | None = None,
        event_type: str | None = None,
        severity: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        search: str | None = None,
        before: datetime | None = None,
        before_id: str | None = None,
    ) -> tuple[list[Event], bool]:
        """Newest-first page with a stable ``(occurred_at, id)`` cursor.

        The cursor is *exclusive in both components*: an event sharing the cursor
        timestamp is skipped unless its id sorts before the cursor id. Using
        ``<=`` on the timestamp alone would repeat the boundary item on every
        page, which is the classic cursor-pagination bug.
        """
        statement = self._base_query(principal)
        if camera_id:
            statement = statement.where(Event.camera_id == camera_id)
        if zone_id:
            statement = statement.where(Event.zone_id == zone_id)
        if event_type:
            statement = statement.where(Event.type == event_type)
        if severity:
            statement = statement.where(Event.severity == severity)
        if since:
            statement = statement.where(Event.occurred_at >= _as_utc(since))
        if until:
            statement = statement.where(Event.occurred_at <= _as_utc(until))
        if before:
            boundary = _as_utc(before)
            if before_id:
                statement = statement.where(
                    or_(
                        Event.occurred_at < boundary,
                        and_(Event.occurred_at == boundary, Event.id < before_id),
                    )
                )
            else:
                statement = statement.where(Event.occurred_at < boundary)
        if search:
            pattern = f"%{search.strip().lower()}%"
            statement = statement.where(
                func.lower(Event.type).like(pattern)
                | func.lower(Event.camera_name).like(pattern)
                | func.lower(Event.zone_name).like(pattern)
            )
        statement = statement.order_by(Event.occurred_at.desc(), Event.id.desc()).limit(limit + 1)

        with self._session_factory() as session:
            rows = builtins.list(session.execute(statement).scalars())
        has_more = len(rows) > limit
        return rows[:limit], has_more

    def get(self, principal: Principal, event_id: str) -> Event:
        with self._session_factory() as session:
            row = session.execute(
                self._base_query(principal).where(Event.id == event_id)
            ).scalar_one_or_none()
        if row is None:
            raise NotFound("event", event_id)
        return row

    def cursor_for(self, event: Event) -> str:
        """Opaque pagination cursor: ``<iso timestamp>:<id>``."""
        return f"{_as_utc(event.occurred_at).isoformat().replace('+00:00', 'Z')}:{event.id}"

    @staticmethod
    def parse_cursor(cursor: str) -> tuple[datetime, str]:
        """Split a cursor back into ``(occurred_at, id)``.

        ``rsplit`` rather than ``partition``: an ISO-8601 timestamp contains
        colons, so splitting on the *first* one would tear the date in half.
        Ids are UUIDs and never contain a colon.
        """
        timestamp, separator, event_id = cursor.rpartition(":")
        if not separator or not timestamp or not event_id:
            raise ValidationError("malformed pagination cursor")
        try:
            return datetime.fromisoformat(timestamp.replace("Z", "+00:00")), event_id
        except ValueError as exc:
            raise ValidationError("malformed pagination cursor timestamp") from exc

    # -- aggregation --------------------------------------------------------

    def summary(
        self,
        principal: Principal,
        *,
        since: datetime,
        until: datetime,
        camera_id: str | None = None,
        zone_id: str | None = None,
    ) -> dict[str, Any]:
        """Counts, dwell averages and an hourly series over a bounded window."""
        span_days = (_as_utc(until) - _as_utc(since)).total_seconds() / 86400.0
        if span_days > MAX_SUMMARY_DAYS:
            raise ValidationError(
                f"summary window is limited to {MAX_SUMMARY_DAYS} days; "
                "use GET /events/export for wider ranges"
            )
        if span_days < 0:
            raise ValidationError("'from' must be before 'to'")

        window = [
            Event.occurred_at >= _as_utc(since),
            Event.occurred_at <= _as_utc(until),
            Event.organization_id == principal.organization_id,
        ]
        if camera_id:
            window.append(Event.camera_id == camera_id)
        if zone_id:
            window.append(Event.zone_id == zone_id)

        with self._session_factory() as session:
            totals = session.execute(
                select(Event.type, func.count()).where(and_(*window)).group_by(Event.type)
            ).all()
            by_severity = session.execute(
                select(Event.severity, func.count()).where(and_(*window)).group_by(Event.severity)
            ).all()
            by_camera = session.execute(
                select(Event.camera_id, Event.camera_name, Event.type, func.count())
                .where(and_(*window))
                .group_by(Event.camera_id, Event.camera_name, Event.type)
            ).all()
            by_zone = session.execute(
                select(Event.zone_id, Event.zone_name, func.count())
                .where(and_(*window), Event.zone_id.is_not(None))
                .group_by(Event.zone_id, Event.zone_name)
            ).all()
            hourly = session.execute(
                select(_hour_bucket(), func.count()).where(and_(*window)).group_by(_hour_bucket())
            ).all()
            dwell = session.execute(
                select(func.avg(_dwell_seconds_expression(session.get_bind().dialect.name))).where(
                    and_(*window), Event.type == "ZONE_EXITED"
                )
            ).scalar_one_or_none()

        camera_rows: dict[str, dict[str, Any]] = {}
        for cam_id, cam_name, event_type, count in by_camera:
            entry = camera_rows.setdefault(
                cam_id or "",
                {
                    "camera_id": cam_id,
                    "camera_name": cam_name,
                    "count": 0,
                    "entries": 0,
                    "exits": 0,
                },
            )
            entry["count"] += count
            if event_type == "ZONE_ENTERED":
                entry["entries"] += count
            elif event_type == "ZONE_EXITED":
                entry["exits"] += count

        return {
            "window": {
                "from": _as_utc(since).isoformat().replace("+00:00", "Z"),
                "to": _as_utc(until).isoformat().replace("+00:00", "Z"),
            },
            "totals": {
                "events": sum(count for _type, count in totals),
                "by_severity": dict(by_severity),
            },
            "by_type": [{"type": event_type, "count": count} for event_type, count in totals],
            "by_camera": sorted(camera_rows.values(), key=lambda item: -item["count"]),
            "by_zone": [
                {"zone_id": zid, "zone_name": zname, "count": count}
                for zid, zname, count in by_zone
            ],
            "hourly": [
                {"bucket": bucket, "count": count}
                for bucket, count in sorted(hourly, key=lambda row: row[0])
            ],
            "avg_dwell_seconds": round(float(dwell), 2) if dwell is not None else None,
        }

    # -- export -------------------------------------------------------------

    def export_rows(
        self,
        principal: Principal,
        *,
        since: datetime,
        until: datetime,
        camera_id: str | None = None,
        zone_id: str | None = None,
    ) -> Iterator[Event]:
        """Stream matching events oldest-first for export."""
        statement = self._base_query(principal).where(
            Event.occurred_at >= _as_utc(since), Event.occurred_at <= _as_utc(until)
        )
        if camera_id:
            statement = statement.where(Event.camera_id == camera_id)
        if zone_id:
            statement = statement.where(Event.zone_id == zone_id)
        statement = statement.order_by(Event.occurred_at.asc(), Event.id.asc())
        with self._session_factory() as session:
            yield from session.execute(statement).scalars()

    def export_csv(self, principal: Principal, **kwargs: Any) -> str:
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=list(EXPORT_COLUMNS), extrasaction="ignore")
        writer.writeheader()
        for index, event in enumerate(self.export_rows(principal, **kwargs)):
            if index >= MAX_EXPORT_ROWS:
                logger.warning("csv export truncated at %d rows", MAX_EXPORT_ROWS)
                break
            writer.writerow(_export_row(event))
        return buffer.getvalue()

    def export_json(self, principal: Principal, **kwargs: Any) -> str:
        rows: list[dict[str, Any]] = []
        for index, event in enumerate(self.export_rows(principal, **kwargs)):
            if index >= MAX_EXPORT_ROWS:
                break
            rows.append(_export_row(event))
        return json.dumps({"items": rows, "count": len(rows)}, default=str)

    def prune(self, organization_id: str, *, before: datetime) -> tuple[int, builtins.list[str]]:
        """Delete events older than ``before``; return the count and snapshot paths."""
        with self._session_factory() as session:
            rows = list(
                session.execute(
                    select(Event.id, Event.snapshot_path).where(
                        Event.organization_id == organization_id, Event.occurred_at < before
                    )
                ).all()
            )
            for event_id, _snapshot in rows:
                session.delete(session.get(Event, event_id))
            session.commit()
        return len(rows), [snapshot for _id, snapshot in rows if snapshot]

    def count(self, principal: Principal) -> int:
        with self._session_factory() as session:
            return int(
                session.execute(
                    select(func.count())
                    .select_from(Event)
                    .where(Event.organization_id == principal.organization_id)
                ).scalar_one()
            )


def _export_row(event: Event) -> dict[str, Any]:
    return {
        "id": event.id,
        "occurred_at": _as_utc(event.occurred_at).isoformat().replace("+00:00", "Z"),
        "received_at": _as_utc(event.received_at).isoformat().replace("+00:00", "Z"),
        "type": event.type,
        "severity": event.severity,
        "camera_name": event.camera_name,
        "zone_name": event.zone_name or "",
        "track_id": event.track_id if event.track_id is not None else "",
        "confidence": event.confidence if event.confidence is not None else "",
        "metadata": json.dumps(event.metadata_, default=str),
    }


def _hour_bucket() -> Any:
    """Portable hour truncation for SQLite and Postgres."""
    from sqlalchemy import String, cast

    return func.substr(cast(Event.occurred_at, String), 1, 13)


def _dwell_seconds_expression(dialect_name: str) -> Any:
    """Read ``metadata.dwell_seconds`` as a float, per dialect.

    SQLite has no JSON cast, so it needs ``json_extract``; Postgres can cast the
    JSON path directly. This is the one place in the codebase that has to know
    the dialect, and it is quarantined here on purpose.
    """
    if dialect_name == "sqlite":
        return func.json_extract(Event.metadata_, "$.dwell_seconds")
    return cast(Event.metadata_["dwell_seconds"].as_string(), Float)


__all__ = ["EXPORT_COLUMNS", "MAX_SUMMARY_DAYS", "EventService"]
