"""Zones: the V0.1 core configuration surface."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request, Response, status

from ...core.logging import current_request_id
from ...schemas.dto import ZoneCreate, ZoneOut, ZonePatch
from ..deps import ContainerDep, PrincipalDep, client_ip

router = APIRouter(prefix="/zones", tags=["zones"])

logger = logging.getLogger("visionops.zones")


@router.get("", response_model=list[ZoneOut], summary="List zones")
def list_zones(
    container: ContainerDep, principal: PrincipalDep, camera_id: str | None = None
) -> list[ZoneOut]:
    """Zones for one camera, or across the organisation."""
    return [_zone_out(row) for row in container.zones.list(principal, camera_id)]


@router.post("", response_model=ZoneOut, status_code=201, summary="Create a zone")
def create_zone(
    body: ZoneCreate, request: Request, container: ContainerDep, principal: PrincipalDep
) -> ZoneOut:
    """Create a restricted zone.

    ``points`` are **normalised** coordinates in ``[0, 1]`` with the origin at the
    top-left of the video frame, so a zone survives a resolution change.
    """
    zone = container.zones.create(principal, body.model_dump())
    _refresh(container, principal, zone.camera_id)
    container.audit.record(
        principal.organization_id,
        action="zone.create",
        actor_type=principal.actor_type,
        actor_id=principal.actor_id,
        target_type="zone",
        target_id=zone.id,
        ip=client_ip(request, container.settings.trusted_proxies),
        request_id=current_request_id(),
        detail={
            "camera_id": zone.camera_id,
            "name": zone.name,
            "kind": zone.kind,
            "severity": zone.severity,
            "point_count": len(zone.points),
        },
    )
    return _zone_out(zone)


@router.get("/{zone_id}", response_model=ZoneOut, summary="Get a zone")
def get_zone(zone_id: str, container: ContainerDep, principal: PrincipalDep) -> ZoneOut:
    return _zone_out(container.zones.get(principal, zone_id))


@router.patch("/{zone_id}", response_model=ZoneOut, summary="Update a zone")
def update_zone(
    zone_id: str,
    body: ZonePatch,
    request: Request,
    container: ContainerDep,
    principal: PrincipalDep,
) -> ZoneOut:
    """Edit a zone. A running session picks the change up without a restart."""
    zone = container.zones.update(principal, zone_id, body.model_dump(exclude_unset=True))
    _refresh(container, principal, zone.camera_id)
    container.audit.record(
        principal.organization_id,
        action="zone.update",
        actor_type=principal.actor_type,
        actor_id=principal.actor_id,
        target_type="zone",
        target_id=zone_id,
        ip=client_ip(request, container.settings.trusted_proxies),
        request_id=current_request_id(),
        detail={"changed": sorted(body.model_dump(exclude_unset=True))},
    )
    return _zone_out(zone)


@router.delete("/{zone_id}", status_code=204, summary="Delete a zone")
def delete_zone(
    zone_id: str, request: Request, container: ContainerDep, principal: PrincipalDep
) -> Response:
    """Stops future events. Historical events keep their zone name."""
    zone = container.zones.get(principal, zone_id)
    container.zones.delete(principal, zone_id)
    _refresh(container, principal, zone.camera_id)
    container.audit.record(
        principal.organization_id,
        action="zone.delete",
        actor_type=principal.actor_type,
        actor_id=principal.actor_id,
        target_type="zone",
        target_id=zone_id,
        ip=client_ip(request, container.settings.trusted_proxies),
        request_id=current_request_id(),
    )
    return Response(status_code=204)


@router.get("/{zone_id}/events", summary="Events for a zone")
def zone_events(
    zone_id: str,
    container: ContainerDep,
    principal: PrincipalDep,
    limit: int = 50,
    before: datetime | None = None,
) -> Any:
    """Convenience filter over the event log."""
    container.zones.get(principal, zone_id)
    return _event_page(container, principal, zone_id=zone_id, limit=limit, before=before)


def _event_page(
    container: Any,
    principal: Any,
    *,
    zone_id: str | None = None,
    camera_id: str | None = None,
    limit: int = 50,
    before: datetime | None = None,
) -> Any:
    from .events import event_page

    return event_page(
        container,
        principal,
        camera_id=camera_id,
        zone_id=zone_id,
        limit=limit,
        before=before,
    )


def _refresh(container: Any, principal: Any, camera_id: str) -> None:
    """Push the current zone set into a running session (no restart needed)."""
    try:
        container.sessions.refresh_zones(camera_id, container.zones.specs_for(principal, camera_id))
    except Exception:
        logger.debug("live zone refresh failed for camera %s", camera_id, exc_info=True)


def _zone_out(row: Any) -> ZoneOut:
    return ZoneOut(
        id=row.id,
        camera_id=row.camera_id,
        name=row.name,
        description=row.description,
        kind=row.kind,
        points=[list(point) for point in row.points],
        color=row.color,
        severity=row.severity,
        class_filter=list(row.class_filter or []),
        cooldown_seconds=row.cooldown_seconds,
        min_dwell_seconds=row.min_dwell_seconds,
        enabled=row.enabled,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


__all__ = ["router", "status"]
