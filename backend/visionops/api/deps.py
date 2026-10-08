"""The application container and FastAPI dependencies.

Explicit construction — no service locator, no module-level singletons that make
tests order-dependent. The container is created once at startup and stashed on
``app.state``; dependencies read it from the request.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Request
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings
from ..core.auth import ApiKeyProvider, extract_credentials, rate_limit_key
from ..core.errors import Forbidden, RateLimited, Unauthorized
from ..core.logging import current_request_id
from ..core.ratelimit import RateLimiter
from ..core.security import SecretBox, parse_api_key
from ..db.engine import build_engine, build_session_factory
from ..db.scoped import Principal
from ..observability.metrics import METRIC_AUTH_FAILURES, METRIC_RATE_LIMITED, registry
from ..realtime.hub import RealtimeHub
from ..services.audit_service import AuditService
from ..services.event_service import EventService
from ..services.resource_service import CameraService, ZoneService
from ..services.session_service import SessionManager
from ..services.system_service import SystemService
from ..vision.contracts import ModelInfo
from ..vision.onnx_model import DetectorConfig, ModelError, OnnxYoloModel


@dataclass(slots=True)
class Container:
    """Everything the API needs, constructed once."""

    settings: Settings
    engine: Any
    session_factory: sessionmaker[Session]
    hub: RealtimeHub
    auth: ApiKeyProvider
    rate_limiter: RateLimiter
    auth_limiter: RateLimiter
    audit: AuditService
    events: EventService
    cameras: CameraService
    zones: ZoneService
    sessions: SessionManager
    system: SystemService
    secret_box: SecretBox | None
    model: OnnxYoloModel | None
    model_info: ModelInfo | None
    model_error: str | None
    started_at: float
    organization_id: str
    organization_name: str
    snapshots_enabled: bool
    retention_days: int

    def model_state(self) -> str:
        if self.model is None:
            return "failed" if self.model_error else "unconfigured"
        return str(self.model.state)


def build_model(settings: Settings) -> tuple[OnnxYoloModel | None, ModelInfo | None, str | None]:
    """Load the detection model, returning ``(model, info, error)``.

    A missing or broken model must not stop the API: the dashboard, history and
    export still work, and System Status reports the failure honestly.
    """
    config = DetectorConfig(
        model_path=settings.model_path,
        provider=settings.ort_provider,
        threads=settings.ort_threads,
        input_width=settings.input_width,
        input_height=settings.input_height,
        confidence_threshold=settings.confidence_threshold,
        nms_iou_threshold=settings.nms_iou_threshold,
        class_filter=settings.class_set,
        expected_sha256=settings.model_sha256,
        use_area_interpolation=settings.inter_area,
    )
    model = OnnxYoloModel(config)
    try:
        info = model.load()
        model.warmup()
    except ModelError as exc:
        return None, None, f"{exc.reason_code}: {exc.detail}"
    except Exception as exc:
        return None, None, f"MODEL_ERROR: {exc}"
    return model, info, None


def build_container(
    settings: Settings, *, organization_id: str, organization_name: str
) -> Container:
    """Construct the whole application graph."""
    started_at = time.time()
    settings.ensure_directories()

    engine = build_engine(settings.database_url)
    session_factory = build_session_factory(engine)

    secret_box: SecretBox | None = None
    if settings.decoded_master_key is not None:
        secret_box = SecretBox(settings.decoded_master_key)
    secret_state = "enabled" if secret_box is not None else "not-configured"

    hub = RealtimeHub()
    audit = AuditService(session_factory)
    events = EventService(session_factory)
    cameras = CameraService(
        session_factory, secret_box=secret_box, media_root=settings.media_root
    )
    zones = ZoneService(session_factory)
    model, model_info, model_error = build_model(settings)

    # Read organisation defaults so a single-tenant deployment inherits them.
    from ..models import Organization

    with session_factory() as session:
        organization = session.get(Organization, organization_id)
        snapshots_enabled = bool(organization.snapshots_enabled) if organization else False
        retention_days = (
            int(organization.retention_days) if organization else settings.retention_days
        )

    sessions = SessionManager(
        max_sessions=settings.max_sessions,
        hub=hub,
        event_service=events,
        settings=settings,
        model_info=model_info,
        model=model,
        snapshot_enabled=snapshots_enabled,
        source_backoff_seconds=settings.source_backoff_seconds,
        max_retries=settings.source_max_retries,
    )
    sessions.set_secret_box(secret_box)

    system = SystemService(
        settings=settings,
        session_manager=sessions,
        version="0.1.0",
        started_at=started_at,
        encryption_state=secret_state,
        snapshots_enabled=snapshots_enabled,
        model_error=model_error,
    )

    return Container(
        settings=settings,
        engine=engine,
        session_factory=session_factory,
        hub=hub,
        auth=ApiKeyProvider(session_factory=session_factory),
        rate_limiter=RateLimiter(settings.rate_limit_rps),
        # Repeated *failed* keys are throttled harder than normal traffic, but an
        # operator with a typo must still get a clear 401 rather than a 429 on
        # their second attempt — hence a burst equal to the configured allowance.
        auth_limiter=RateLimiter(
            max(0.05, settings.auth_failure_limit / 60.0), burst=float(settings.auth_failure_limit)
        ),
        audit=audit,
        events=events,
        cameras=cameras,
        zones=zones,
        sessions=sessions,
        system=system,
        secret_box=secret_box,
        model=model,
        model_info=model_info,
        model_error=model_error,
        started_at=started_at,
        organization_id=organization_id,
        organization_name=organization_name,
        snapshots_enabled=snapshots_enabled,
        retention_days=retention_days,
    )


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------


def get_container(request: Request) -> Container:
    container = getattr(request.app.state, "container", None)
    if container is None:  # pragma: no cover - only during a broken startup
        raise Unauthorized("the application is not ready")
    return container


def get_session(request: Request) -> Any:
    container = get_container(request)
    with container.session_factory() as session:
        yield session


def get_settings_dep(request: Request) -> Settings:
    return get_container(request).settings


logger = logging.getLogger("visionops.api.deps")

ContainerDep = Annotated[Container, Depends(get_container)]
SessionDep = Annotated[Any, Depends(get_session)]


#: ``(path prefix, read permission, write permission)``, longest prefix first.
#:
#: Enforcement lives here rather than in each handler so the mapping can be
#: read, audited and tested in one place. Permission strings are those of
#: ``docs/PRD.md`` §26.
ROUTE_PERMISSIONS: tuple[tuple[str, str, str], ...] = (
    ("/api/v1/events/export", "event.export", "event.export"),
    ("/api/v1/events/schema", "event.read", "event.read"),
    ("/api/v1/apikeys", "apikeys.manage", "apikeys.manage"),
    ("/api/v1/cameras", "camera.read", "camera.write"),
    ("/api/v1/sites", "camera.read", "camera.write"),
    ("/api/v1/organizations", "org.manage", "org.manage"),
    ("/api/v1/zones", "zone.read", "zone.write"),
    ("/api/v1/events", "event.read", "event.read"),
    ("/api/v1/models", "model.manage", "model.manage"),
    ("/api/v1/audit", "audit.read", "audit.read"),
    ("/api/v1/system", "org.manage", "org.manage"),
    ("/api/v1/stream", "session.start", "session.start"),
)

#: Reachable without a key. Liveness and readiness must answer before a key
#: exists, otherwise a fresh install cannot be probed.
PUBLIC_PATHS = frozenset(
    {
        "/",
        "/api/v1/healthz",
        "/api/v1/readyz",
        "/api/v1/docs",
        "/api/v1/redoc",
        "/api/v1/openapi.json",
        "/docs/oauth2-redirect",
    }
)

#: HTTP methods that mutate state and therefore need the *write* permission.
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Returned by :func:`required_permission` for an API route with no mapping.
#: It is never a real permission, so it can never be satisfied — the request is
#: refused. A new endpoint is therefore denied until someone decides what it
#: needs, which is the safe direction to fail in.
UNMAPPED = "\x00unmapped"


def is_public_path(path: str) -> bool:
    """Whether ``path`` is reachable without an API key.

    Anything outside ``/api`` is the single-page app itself, which must render
    so the operator can be shown the key prompt.
    """
    return path in PUBLIC_PATHS or not path.startswith("/api/")


def required_permission(path: str, method: str) -> str | None:
    """The permission ``method path`` needs.

    Returns ``None`` for public paths and :data:`UNMAPPED` for an API route
    with no entry in :data:`ROUTE_PERMISSIONS`.
    """
    if is_public_path(path):
        return None
    for prefix, read_permission, write_permission in ROUTE_PERMISSIONS:
        if path == prefix or path.startswith(prefix + "/"):
            return write_permission if method in _WRITE_METHODS else read_permission
    return UNMAPPED


async def get_principal(
    request: Request, container: ContainerDep, session: SessionDep
) -> Principal:
    """Authenticate, rate-limit and audit the caller.

    Order matters: the rate limit is checked before any database work, and the
    attributable failure paths are audited so credential probing is visible.
    """
    credentials = extract_credentials(request)
    if not credentials:
        # No credential means no key to attribute the attempt to, so this is
        # throttled per source address instead of per key: an unauthenticated
        # flood (or a spray of distinct made-up keys) has to hit a ceiling
        # somewhere, and a per-key bucket cannot see it.
        source = client_ip(request, container.settings.trusted_proxies) or "unknown"
        if not container.auth_limiter.allow(f"ip:{source}"):
            registry.counter(METRIC_RATE_LIMITED, 1, endpoint="anonymous")
            raise RateLimited(container.auth_limiter.retry_after(f"ip:{source}"))
        raise Unauthorized()
    fingerprint = rate_limit_key(credentials)
    if not container.rate_limiter.allow(fingerprint):
        registry.counter(METRIC_RATE_LIMITED, 1, endpoint=request.url.path)
        raise RateLimited(container.rate_limiter.retry_after(fingerprint))

    principal = await container.auth.resolve(credentials, session)
    if principal is None:
        registry.counter(METRIC_AUTH_FAILURES, 1, reason="invalid_key")
        source = client_ip(request, container.settings.trusted_proxies) or "unknown"
        # Even if the credential is bogus, a spray of different keys from the
        # same source is an unauthenticated flood: throttle per source address.
        if not container.auth_limiter.allow(f"ip:{source}"):
            registry.counter(METRIC_RATE_LIMITED, 1, endpoint="anonymous")
            raise RateLimited(container.auth_limiter.retry_after(f"ip:{source}"))
        if not container.auth_limiter.allow(fingerprint):
            registry.counter(METRIC_RATE_LIMITED, 1, endpoint=request.url.path)
            raise RateLimited(container.auth_limiter.retry_after(fingerprint))
        _audit_rejection(
            container,
            request,
            credentials,
            action="auth.key_rejected",
            detail={"reason": "invalid_key"},
        )
        raise Unauthorized()

    permission = required_permission(request.url.path, request.method)
    if permission == UNMAPPED:
        registry.counter(METRIC_AUTH_FAILURES, 1, reason="unmapped_route")
        logger.error(
            "refusing %s %s: no permission mapping (add it to ROUTE_PERMISSIONS)",
            request.method,
            request.url.path,
        )
        raise Forbidden("this endpoint has no permission mapping and is refused by default")
    if permission is not None and not principal.can(permission):
        registry.counter(METRIC_AUTH_FAILURES, 1, reason="insufficient_permission")
        _audit_rejection(
            container,
            request,
            credentials,
            action="auth.permission_denied",
            detail={"reason": "insufficient_permission", "required": permission},
        )
        # 403, not 401: the credential is valid, the grant is not.
        raise Forbidden(f"this API key lacks the {permission!r} permission")
    return principal


def _audit_rejection(
    container: Any,
    request: Request,
    credentials: str,
    *,
    action: str,
    detail: dict[str, Any],
) -> None:
    """Record a rejected credential, when it can be attributed to an organisation.

    ``audit_logs.organization_id`` is not nullable, so an attempt with a key
    prefix that matches no row cannot be written at all — there is no tenant to
    file it under. Those are counted and logged instead; every attempt against a
    *known* key (including a revoked one) is audited.
    """
    organization_id = None
    resolver = getattr(container.auth, "organization_for", None)
    if resolver is not None:
        try:
            organization_id = resolver(credentials)
        except Exception:  # pragma: no cover - a lookup failure must not mask the 401
            logger.debug("could not attribute a rejected credential", exc_info=True)
    if organization_id is None:
        logger.warning(
            "rejected an unrecognised credential: action=%s path=%s method=%s",
            action,
            request.url.path,
            request.method,
        )
        return
    parsed = parse_api_key(credentials)
    # ``vo_<id>`` is the public half of a key: it is listed by ``GET /apikeys``
    # and is what an operator needs to find the offending credential. The
    # secret half never leaves this function.
    key_prefix = f"vo_{parsed[0]}" if parsed else "unknown"
    container.audit.record(
        organization_id,
        action=action,
        actor_type="api_key",
        actor_id=key_prefix,
        target_type="request",
        target_id=request.url.path[:64],
        outcome="denied",
        ip=client_ip(request, container.settings.trusted_proxies),
        user_agent=request.headers.get("user-agent"),
        request_id=current_request_id(),
        detail={**detail, "method": request.method},
    )


PrincipalDep = Annotated[Principal, Depends(get_principal)]


def require(permission: str) -> Callable[..., Awaitable[Principal]]:
    """Build a dependency that enforces a permission (deny by default)."""

    async def _dependency(principal: PrincipalDep) -> Principal:
        if not principal.can(permission):
            raise Unauthorized()
        return principal

    return _dependency


def client_ip(request: Request, trusted_proxies: list[str]) -> str | None:
    """Resolve the client IP, honouring proxy headers only for trusted proxies."""
    direct = request.client.host if request.client else None
    if not trusted_proxies:
        return direct
    forwarded = request.headers.get("x-forwarded-for")
    if not forwarded:
        return direct
    if direct not in trusted_proxies:
        return direct
    return forwarded.split(",")[0].strip() or direct


__all__ = [
    "Container",
    "ContainerDep",
    "PrincipalDep",
    "SessionDep",
    "build_container",
    "build_model",
    "client_ip",
    "get_container",
    "get_principal",
    "get_session",
    "require",
]
