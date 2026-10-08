"""Sites, cameras and video sources."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response, status

from ...core.errors import NotFound
from ...core.logging import current_request_id
from ...models import Organization
from ...schemas.dto import (
    CameraCreate,
    CameraOut,
    CameraPatch,
    OrganizationOut,
    SiteCreate,
    SiteOut,
    SitePatch,
    SourceCreate,
    SourceOut,
)
from ..deps import ContainerDep, PrincipalDep, client_ip

router = APIRouter(tags=["resources"])


# ---------------------------------------------------------------------------
# Organisation (self)
# ---------------------------------------------------------------------------


@router.get("/organizations/self", response_model=OrganizationOut, summary="Current organisation")
def organization(container: ContainerDep, principal: PrincipalDep) -> OrganizationOut:
    """The caller's own organisation. There is no cross-organisation access."""
    with container.session_factory() as session:
        row = session.get(Organization, principal.organization_id)
    if row is None:  # pragma: no cover - the bootstrap org always exists
        raise NotFound("organization", principal.organization_id)
    return OrganizationOut(
        id=row.id,
        name=row.name,
        slug=row.slug,
        timezone=row.timezone,
        retention_days=row.retention_days,
        snapshots_enabled=row.snapshots_enabled,
        min_dwell_seconds=row.min_dwell_seconds,
        event_cooldown_seconds=row.event_cooldown_seconds,
        settings=row.settings,
        created_at=row.created_at,
    )


# ---------------------------------------------------------------------------
# Sites
# ---------------------------------------------------------------------------


@router.get("/sites", response_model=list[SiteOut], summary="List sites")
def list_sites(container: ContainerDep, principal: PrincipalDep) -> list[SiteOut]:
    return [_site_out(site) for site in container.cameras.list_sites(principal)]


@router.post("/sites", response_model=SiteOut, status_code=201, summary="Create a site")
def create_site(
    body: SiteCreate, request: Request, container: ContainerDep, principal: PrincipalDep
) -> SiteOut:
    site = container.cameras.create_site(principal, body.model_dump())
    container.audit.record(
        principal.organization_id,
        action="site.create",
        actor_type=principal.actor_type,
        actor_id=principal.actor_id,
        target_type="site",
        target_id=site.id,
        ip=client_ip(request, container.settings.trusted_proxies),
        request_id=current_request_id(),
        detail={"name": site.name},
    )
    return _site_out(site)


@router.get("/sites/{site_id}", response_model=SiteOut, summary="Get a site")
def get_site(site_id: str, container: ContainerDep, principal: PrincipalDep) -> SiteOut:
    return _site_out(container.cameras.get_site(principal, site_id))


@router.patch("/sites/{site_id}", response_model=SiteOut, summary="Update a site")
def update_site(
    site_id: str,
    body: SitePatch,
    request: Request,
    container: ContainerDep,
    principal: PrincipalDep,
) -> SiteOut:
    site = container.cameras.update_site(principal, site_id, body.model_dump(exclude_unset=True))
    container.audit.record(
        principal.organization_id,
        action="site.update",
        actor_type=principal.actor_type,
        actor_id=principal.actor_id,
        target_type="site",
        target_id=site_id,
        ip=client_ip(request, container.settings.trusted_proxies),
        request_id=current_request_id(),
    )
    return _site_out(site)


@router.delete("/sites/{site_id}", status_code=204, summary="Delete a site")
def delete_site(
    site_id: str, request: Request, container: ContainerDep, principal: PrincipalDep
) -> Response:
    container.cameras.delete_site(principal, site_id)
    container.audit.record(
        principal.organization_id,
        action="site.delete",
        actor_type=principal.actor_type,
        actor_id=principal.actor_id,
        target_type="site",
        target_id=site_id,
        ip=client_ip(request, container.settings.trusted_proxies),
        request_id=current_request_id(),
    )
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Cameras
# ---------------------------------------------------------------------------


@router.get("/cameras", response_model=list[CameraOut], summary="List cameras")
def list_cameras(
    container: ContainerDep,
    principal: PrincipalDep,
    site_id: str | None = None,
    camera_status: str | None = None,
    q: str | None = None,
) -> list[CameraOut]:
    rows = container.cameras.list_cameras(
        principal, site_id=site_id, status=camera_status, search=q
    )
    return [_camera_out(row) for row in rows]


@router.post("/cameras", response_model=CameraOut, status_code=201, summary="Create a camera")
def create_camera(
    body: CameraCreate, request: Request, container: ContainerDep, principal: PrincipalDep
) -> CameraOut:
    camera = container.cameras.create_camera(principal, body.model_dump())
    container.audit.record(
        principal.organization_id,
        action="camera.create",
        actor_type=principal.actor_type,
        actor_id=principal.actor_id,
        target_type="camera",
        target_id=camera.id,
        ip=client_ip(request, container.settings.trusted_proxies),
        request_id=current_request_id(),
        detail={"name": camera.name, "site_id": camera.site_id},
    )
    return _camera_out(camera)


@router.get("/cameras/{camera_id}", response_model=CameraOut, summary="Get a camera")
def get_camera(camera_id: str, container: ContainerDep, principal: PrincipalDep) -> CameraOut:
    return _camera_out(container.cameras.get_camera(principal, camera_id))


@router.patch("/cameras/{camera_id}", response_model=CameraOut, summary="Update a camera")
def update_camera(
    camera_id: str,
    body: CameraPatch,
    request: Request,
    container: ContainerDep,
    principal: PrincipalDep,
) -> CameraOut:
    camera = container.cameras.update_camera(
        principal, camera_id, body.model_dump(exclude_unset=True)
    )
    container.audit.record(
        principal.organization_id,
        action="camera.update",
        actor_type=principal.actor_type,
        actor_id=principal.actor_id,
        target_type="camera",
        target_id=camera_id,
        ip=client_ip(request, container.settings.trusted_proxies),
        request_id=current_request_id(),
    )
    return _camera_out(camera)


@router.delete("/cameras/{camera_id}", status_code=204, summary="Delete a camera")
def delete_camera(
    camera_id: str, request: Request, container: ContainerDep, principal: PrincipalDep
) -> Response:
    container.cameras.delete_camera(principal, camera_id)
    container.audit.record(
        principal.organization_id,
        action="camera.delete",
        actor_type=principal.actor_type,
        actor_id=principal.actor_id,
        target_type="camera",
        target_id=camera_id,
        ip=client_ip(request, container.settings.trusted_proxies),
        request_id=current_request_id(),
    )
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


@router.get("/cameras/{camera_id}/sources", response_model=list[SourceOut], summary="List sources")
def list_sources(
    camera_id: str, container: ContainerDep, principal: PrincipalDep
) -> list[SourceOut]:
    container.cameras.get_camera(principal, camera_id)
    return [_source_out(row) for row in container.cameras.list_sources(principal, camera_id)]


@router.post(
    "/cameras/{camera_id}/sources",
    response_model=SourceOut,
    status_code=201,
    summary="Add a source",
)
def create_source(
    camera_id: str,
    body: SourceCreate,
    request: Request,
    container: ContainerDep,
    principal: PrincipalDep,
) -> SourceOut:
    """Credentials are write-only: stored encrypted, never returned."""
    payload = body.model_dump()
    source = container.cameras.add_source(principal, camera_id, payload)
    container.audit.record(
        principal.organization_id,
        action="source.create",
        actor_type=principal.actor_type,
        actor_id=principal.actor_id,
        target_type="source",
        target_id=source.id,
        ip=client_ip(request, container.settings.trusted_proxies),
        request_id=current_request_id(),
        detail={"kind": source.kind, "name": source.name, "camera_id": camera_id},
    )
    return _source_out(source)


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def _site_out(row: Any) -> SiteOut:
    return SiteOut(
        id=row.id,
        name=row.name,
        timezone=row.timezone,
        address=row.address,
        description=row.description,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _camera_out(row: Any) -> CameraOut:
    return CameraOut(
        id=row.id,
        name=row.name,
        description=row.description,
        site_id=row.site_id,
        site_name=getattr(row.site, "name", None),
        status=row.status,
        target_fps=row.target_fps,
        settings=row.settings or {},
        last_seen_at=row.last_seen_at,
        zone_count=len([z for z in row.zones if z.deleted_at is None]),
        source_count=len(row.sources),
        created_at=row.created_at,
    )


def _source_out(row: Any) -> SourceOut:
    options = {k: v for k, v in (row.options or {}).items() if k != "credential_blob"}
    return SourceOut(
        id=row.id,
        camera_id=row.camera_id,
        name=row.name,
        kind=row.kind,
        target=row.target,
        is_primary=row.is_primary,
        options=options,
        credentials={"configured": bool(row.credential_blob)},
        created_at=row.created_at,
    )


__all__ = ["router", "status"]
