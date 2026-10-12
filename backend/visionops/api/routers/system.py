"""Health, readiness, system status, metrics, model info and API keys."""

from __future__ import annotations

import time
from datetime import UTC
from typing import Any

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import PlainTextResponse

from ...core.ids import new_id
from ...core.logging import current_request_id
from ...core.security import generate_api_key, hash_secret
from ...models import ApiKey
from ...schemas.dto import ApiKeyCreate, ApiKeyCreated, ApiKeyOut, HealthOut, ReadyOut
from ..deps import ContainerDep, PrincipalDep, client_ip

router = APIRouter(tags=["system"])

PUBLIC_PATHS = frozenset({"/healthz", "/readyz", "/api/v1/healthz", "/api/v1/readyz"})


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now(UTC).isoformat()


@router.get("/healthz", response_model=HealthOut, summary="Liveness")
def healthz(container: ContainerDep) -> HealthOut:
    """Liveness: the process is running. Never touches the database."""
    return HealthOut(
        status="ok",
        version=container.settings.version,
        uptime_seconds=round(time.time() - container.started_at, 1),
    )


@router.get("/readyz", response_model=ReadyOut, summary="Readiness")
def readyz(container: ContainerDep, response: Response) -> ReadyOut:
    """Readiness: the API can serve requests.

    A missing model makes this ``degraded`` rather than unready — configuration,
    history and export still work. ``503`` is reserved for a *required*
    dependency (the database) being down.
    """
    from ...services.system_service import Check

    verdict, checks = container.system.readiness()
    # The API answering at all is itself a required, healthy component; it makes
    # the readiness document self-describing for the UI's diagnostics panel.
    payload: dict[str, Any] = {"api": Check("ok", None, "API responding", required=True)}
    payload.update(checks)
    if verdict == "unavailable":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadyOut(
        status=verdict,
        timestamp=_now_iso(),
        checks={name: check.to_dict() for name, check in payload.items()},
    )


@router.get("/system/status", summary="Full system status")
def system_status(container: ContainerDep, _principal: PrincipalDep) -> dict[str, Any]:
    """The human-facing health aggregate that the System page renders."""
    return container.system.status()


@router.get(
    "/system/metrics",
    response_class=PlainTextResponse,
    summary="Prometheus metrics",
)
def metrics(container: ContainerDep, _principal: PrincipalDep) -> Response:
    """Prometheus text exposition. No dependency, no telemetry egress."""
    from ...observability.metrics import (
        METRIC_AUTH_FAILURES,
        METRIC_EVENTS,
        METRIC_EVENTS_DROPPED,
        METRIC_EVENTS_SUPPRESSED,
        METRIC_INFERENCE_ERRORS,
        METRIC_MEMORY,
        METRIC_RATE_LIMITED,
        METRIC_SESSIONS_ACTIVE,
        METRIC_SOURCE_ERRORS,
        METRIC_UPTIME,
        cpu_seconds,
        registry,
        resident_memory_bytes,
    )

    memory = resident_memory_bytes()
    if memory is not None:
        registry.gauge(METRIC_MEMORY, memory)
    registry.gauge(METRIC_UPTIME, time.time() - container.started_at)
    registry.gauge("visionops_process_cpu_seconds", cpu_seconds())
    registry.gauge(METRIC_SESSIONS_ACTIVE, container.sessions.active_count)
    registry.gauge("visionops_model_state", 1, state=container.model_state())
    # Counters an operator graphs daily are published at zero from startup, so a
    # dashboard or alert rule does not break before the first event of the day.
    for name in (
        METRIC_EVENTS,
        METRIC_EVENTS_SUPPRESSED,
        METRIC_EVENTS_DROPPED,
        METRIC_INFERENCE_ERRORS,
        METRIC_SOURCE_ERRORS,
        METRIC_RATE_LIMITED,
        METRIC_AUTH_FAILURES,
    ):
        if not registry.value(name):
            registry.counter(name, 0)
    return Response(
        content=registry.render(), media_type="text/plain; version=0.0.4; charset=utf-8"
    )


@router.get("/models/current", summary="Loaded model")
def current_model(container: ContainerDep, _principal: PrincipalDep) -> dict[str, Any]:
    """What the AI is running: model, checksum, thresholds and class filter."""
    if container.model_info is None:
        return {
            "state": container.model_state(),
            "error": container.model_error,
            "config": {
                "confidence_threshold": container.settings.confidence_threshold,
                "nms_iou_threshold": container.settings.nms_iou_threshold,
                "class_filter": sorted(container.settings.class_set),
                "target_fps": container.settings.target_fps,
                "model_path": str(container.settings.model_path),
            },
        }
    return {
        "state": container.model_state(),
        **container.model_info.to_dict(),
        "classes": list(container.model_info.classes[:200]),
        "config": {
            "confidence_threshold": container.settings.confidence_threshold,
            "nms_iou_threshold": container.settings.nms_iou_threshold,
            "class_filter": sorted(container.settings.class_set),
            "target_fps": container.settings.target_fps,
            "tracker": container.settings.tracker,
            "tracker_max_age": container.settings.tracker_max_age,
            "tracker_min_hits": container.settings.tracker_min_hits,
            "provider": container.settings.ort_provider,
        },
    }


@router.get("/events/schema", summary="Event type catalogue")
def event_schema(_principal: PrincipalDep) -> dict[str, Any]:
    """Discover the event contract programmatically (see docs/EVENT_ENGINE.md §7)."""
    from ...vision.event_engine import EVENT_REGISTRY

    return {
        "types": [
            {
                "name": spec.name,
                "severity": str(spec.severity),
                "description": spec.description,
                "since_version": spec.since_version,
            }
            for spec in EVENT_REGISTRY.values()
        ],
        "contract": {
            "schema_version": 1,
            "rules": [
                "consumers must ignore unknown fields",
                "consumers must skip unknown type values",
                "coordinates are normalised to [0, 1]",
            ],
        },
    }


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------

keys_router = APIRouter(prefix="/apikeys", tags=["api-keys"])


@keys_router.get("", response_model=list[ApiKeyOut], summary="List API keys")
def list_keys(container: ContainerDep, principal: PrincipalDep) -> list[ApiKeyOut]:
    """Metadata only. The secret is never retrievable."""
    from sqlalchemy import select

    with container.session_factory() as session:
        rows = session.execute(
            select(ApiKey).where(ApiKey.organization_id == principal.organization_id)
        ).scalars()
        return [_key_out(row) for row in rows]


@keys_router.post("", response_model=ApiKeyCreated, status_code=201, summary="Create an API key")
def create_key(
    body: ApiKeyCreate,
    request: Request,
    container: ContainerDep,
    principal: PrincipalDep,
) -> ApiKeyCreated:
    """The only time the secret is returned. Store it immediately."""
    generated = generate_api_key()
    with container.session_factory() as session:
        record = ApiKey(
            id=new_id(),
            organization_id=principal.organization_id,
            name=body.name,
            key_prefix=generated.key_prefix,
            key_hash=hash_secret(generated.secret, generated.salt),
            key_salt=generated.salt,
            permissions=list(body.permissions or ["*"]),
            rate_limit_rps=body.rate_limit_rps,
        )
        session.add(record)
        session.commit()
        key_id = record.id
    container.audit.record(
        principal.organization_id,
        action="apikey.create",
        actor_type=principal.actor_type,
        actor_id=principal.actor_id,
        target_type="api_key",
        target_id=key_id,
        ip=client_ip(request, container.settings.trusted_proxies),
        request_id=current_request_id(),
        detail={"name": body.name, "prefix": generated.key_prefix},
    )
    return ApiKeyCreated(
        id=key_id,
        name=body.name,
        key=generated.full_key,
        key_prefix=generated.key_prefix,
    )


@keys_router.delete("/{key_id}", status_code=204, summary="Revoke an API key")
def revoke_key(
    key_id: str, request: Request, container: ContainerDep, principal: PrincipalDep
) -> Response:
    from sqlalchemy import select

    from ...core.errors import NotFound
    from ...db.base import utcnow

    with container.session_factory() as session:
        record = session.execute(
            select(ApiKey).where(
                ApiKey.id == key_id, ApiKey.organization_id == principal.organization_id
            )
        ).scalar_one_or_none()
        if record is None:
            raise NotFound("api key", key_id)
        record.revoked = True
        record.revoked_at = utcnow()
        session.commit()
    container.audit.record(
        principal.organization_id,
        action="apikey.revoke",
        actor_type=principal.actor_type,
        actor_id=principal.actor_id,
        target_type="api_key",
        target_id=key_id,
        ip=client_ip(request, container.settings.trusted_proxies),
        request_id=current_request_id(),
    )
    return Response(status_code=204)


def _key_out(row: ApiKey) -> ApiKeyOut:
    return ApiKeyOut(
        id=row.id,
        name=row.name,
        key_prefix=row.key_prefix,
        permissions=list(row.permissions or []),
        rate_limit_rps=row.rate_limit_rps,
        last_used_at=row.last_used_at,
        expires_at=row.expires_at,
        revoked=row.revoked,
        created_at=row.created_at,
    )


__all__ = ["PUBLIC_PATHS", "keys_router", "router"]
