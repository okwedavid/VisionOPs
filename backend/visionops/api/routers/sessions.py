"""Session control and the MJPEG preview stream."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import StreamingResponse

from ...core.errors import NotFound, ValidationError
from ...core.logging import current_request_id
from ...ingest.base import encode_jpeg
from ...ingest.browser import BrowserFrameSource
from ...schemas.dto import SessionOut, SessionStart
from ...vision.render import annotate
from ..deps import ContainerDep, PrincipalDep, client_ip

router = APIRouter(tags=["sessions"])

MJPEG_BOUNDARY = "visionopsframe"


@router.post(
    "/cameras/{camera_id}/session",
    response_model=SessionOut,
    status_code=201,
    summary="Start processing a camera",
)
def start_session(
    camera_id: str,
    request: Request,
    response: Response,
    container: ContainerDep,
    principal: PrincipalDep,
    body: SessionStart | None = None,
) -> SessionOut:
    """Start (or join) the processing session for a camera.

    Idempotent: an already-running camera returns the existing session with
    ``200`` instead of a conflict. A ``browser`` source also receives the
    WebSocket URL the page must connect to.
    """
    payload = body or SessionStart()
    camera = container.cameras.get_camera(principal, camera_id)
    source_row = _resolve_source(container, principal, camera, payload.source_id)

    zone_specs = container.zones.specs_for(principal, camera_id)
    if payload.zones:
        wanted = set(payload.zones)
        zone_specs = [spec for spec in zone_specs if spec.zone_id in wanted]

    target_fps = float(payload.target_fps or camera.target_fps or container.settings.target_fps)
    runner, created = container.sessions.start(
        principal=principal,
        camera=camera,
        source_row=source_row,
        target_fps=target_fps,
        zone_specs=zone_specs,
    )
    # A re-start is idempotent: the same session comes back with 200, not a
    # conflict and not a second "created". The declared 201 is the default;
    # FastAPI cannot vary it per outcome, so the response object sets it.
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    if created:
        container.audit.record(
            principal.organization_id,
            action="session.start",
            actor_type=principal.actor_type,
            actor_id=principal.actor_id,
            target_type="camera",
            target_id=camera_id,
            ip=client_ip(request, container.settings.trusted_proxies),
            request_id=current_request_id(),
            detail={"session_id": runner.session_id, "source_kind": source_row.kind},
        )
    return _session_out(container, runner, include_ingest_url=True)


@router.get(
    "/cameras/{camera_id}/session", response_model=SessionOut, summary="Current session state"
)
def get_session(camera_id: str, container: ContainerDep, principal: PrincipalDep) -> SessionOut:
    runner = container.sessions.for_camera(camera_id)
    if runner is None:
        raise NotFound("session", camera_id)
    return _session_out(container, runner)


@router.delete("/cameras/{camera_id}/session", status_code=204, summary="Stop processing a camera")
def stop_session(
    camera_id: str, request: Request, container: ContainerDep, principal: PrincipalDep
) -> Response:
    container.cameras.get_camera(principal, camera_id)
    container.sessions.stop(camera_id)
    container.audit.record(
        principal.organization_id,
        action="session.stop",
        actor_type=principal.actor_type,
        actor_id=principal.actor_id,
        target_type="camera",
        target_id=camera_id,
        ip=client_ip(request, container.settings.trusted_proxies),
        request_id=current_request_id(),
    )
    return Response(status_code=204)


@router.get(
    "/cameras/{camera_id}/preview.mjpg",
    summary="Annotated MJPEG preview",
    response_class=StreamingResponse,
)
async def preview(
    camera_id: str, container: ContainerDep, principal: PrincipalDep
) -> StreamingResponse:
    """Live annotated stills for server-side sources.

    The browser source renders its own local preview, so this endpoint is for
    ``device``/``file``/``rtsp`` sources — and for ``curl``/``ffplay`` debugging.
    """
    camera = container.cameras.get_camera(principal, camera_id)
    runner = container.sessions.for_camera(camera_id)
    if runner is None:
        raise NotFound("session", camera_id)
    del camera
    settings = container.settings

    async def frames() -> AsyncIterator[bytes]:
        interval = 1.0 / max(0.5, settings.preview_fps)
        deadline = time.monotonic() + 300.0  # bounded stream; clients reconnect
        while time.monotonic() < deadline:
            payload = await asyncio.to_thread(_render_preview, runner, settings.preview_width)
            if payload is None:
                await asyncio.sleep(0.2)
                continue
            yield (
                (
                    f"--{MJPEG_BOUNDARY}\r\n"
                    f"Content-Type: image/jpeg\r\n"
                    f"Content-Length: {len(payload)}\r\n\r\n"
                ).encode()
                + payload
                + b"\r\n"
            )
            await asyncio.sleep(interval)

    return StreamingResponse(
        frames(),
        media_type=f"multipart/x-mixed-replace; boundary={MJPEG_BOUNDARY}",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


def _render_preview(runner: Any, width: int) -> bytes | None:
    frame = runner.pipeline.last_frame
    if frame is None:
        return None
    try:
        annotated = annotate(
            frame.image, runner.pipeline.zones, runner.pipeline.last_tracks, max_width=width
        )
        return encode_jpeg(annotated, quality=65)
    except Exception:
        return None


def _resolve_source(container: Any, principal: Any, camera: Any, source_id: str | None) -> Any:
    sources = container.cameras.list_sources(principal, camera.id)
    if not sources:
        raise ValidationError(
            "this camera has no video source. Add one (browser webcam, device, file or RTSP) "
            "before starting a session.",
            [{"field": "source_id", "message": "no source configured"}],
        )
    if source_id:
        for source in sources:
            if source.id == source_id:
                return source
        raise NotFound("source", source_id)
    return container.cameras.primary_source(camera)


def _session_out(container: Any, runner: Any, *, include_ingest_url: bool = False) -> SessionOut:
    payload = runner.state.to_dict()
    payload.pop("latest_frame_jpeg", None)
    ingest_url = None
    if include_ingest_url and isinstance(runner.source, BrowserFrameSource):
        ingest_url = f"/api/v1/stream/ingest?session_id={runner.session_id}"
    return SessionOut(**payload, ingest_ws_url=ingest_url)


__all__ = ["MJPEG_BOUNDARY", "router"]
