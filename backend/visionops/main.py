"""Application factory, lifespan and middleware."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .api.deps import build_container
from .api.problems import from_app_error, install_exception_handlers, problem_response
from .api.routers import events as events_router
from .api.routers import resources as resources_router
from .api.routers import sessions as sessions_router
from .api.routers import system as system_router
from .api.routers import zones as zones_router
from .api.ws import router as ws_router
from .bootstrap import ensure_bootstrap
from .config import Settings, get_settings
from .core.errors import PayloadTooLarge
from .core.ids import new_id
from .core.logging import configure_logging, log_context, request_id_var
from .observability.metrics import METRIC_HTTP_LATENCY, METRIC_HTTP_REQUESTS, registry

logger = logging.getLogger("visionops")

API_PREFIX = "/api/v1"
DESCRIPTION = """
**VisionOps** turns cameras into operational intelligence.

V0.1 delivers *restricted-zone intelligence*: ingest a camera, detect and track
people, draw a zone, and get a timestamped, persisted `ZONE_ENTERED` /
`ZONE_EXITED` event with full history.

All geometry (`points`, `box`) is **normalised** to `[0, 1]` so it survives a
resolution change. No biometrics are collected: analytics are over objects,
anonymous ephemeral track IDs, zones, counts and timestamps.

Authenticate with `Authorization: Bearer vo_<id>_<secret>` or `X-API-Key`.
`/healthz` and `/readyz` are public so probes work without credentials.
"""

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "geolocation=(), microphone=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application. Explicit factory: no import-time side effects."""
    resolved = settings or get_settings()
    configure_logging(level=resolved.log_level, fmt=resolved.log_format, log_file=resolved.log_file)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        organization_id, organization_name = ensure_bootstrap(resolved)
        container = build_container(
            resolved, organization_id=organization_id, organization_name=organization_name
        )
        app.state.container = container
        if container.model_error:
            logger.error("detection model unavailable: %s", container.model_error)
        else:
            logger.info(
                "model ready: %s (%s, %d classes)",
                container.model_info.name if container.model_info else "?",
                container.model_info.sha256[:12] if container.model_info else "?",
                len(container.model_info.classes) if container.model_info else 0,
            )
        logger.info(
            "visionops %s ready — env=%s db=%s max_sessions=%d",
            resolved.version,
            resolved.env,
            resolved.database_url.split("://", 1)[0],
            resolved.max_sessions,
        )
        try:
            yield
        finally:
            container.sessions.stop_all()
            await container.hub.shutdown()
            container.engine.dispose()
            logger.info("visionops stopped cleanly")

    app = FastAPI(
        title="VisionOps API",
        version=resolved.version,
        description=DESCRIPTION,
        lifespan=lifespan,
        docs_url=f"{API_PREFIX}/docs",
        redoc_url=f"{API_PREFIX}/redoc",
        openapi_url=f"{API_PREFIX}/openapi.json",
    )

    if resolved.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(resolved.cors_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "X-API-Key", "Content-Type", "Idempotency-Key"],
            max_age=600,
        )

    @app.middleware("http")
    async def observability(request: Request, call_next: Any) -> Response:
        request_id = _request_id(request)
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        path = _template_path(request)
        try:
            with log_context(request_id=request_id):
                response = await call_next(request)
        except Exception:
            registry.counter(
                METRIC_HTTP_REQUESTS, 1, method=request.method, path=path, status="500"
            )
            raise
        finally:
            request_id_var.reset(token)
        elapsed = time.perf_counter() - started
        registry.counter(
            METRIC_HTTP_REQUESTS,
            1,
            method=request.method,
            path=path,
            status=str(response.status_code),
        )
        registry.observe_ms(METRIC_HTTP_LATENCY, elapsed * 1000.0, method=request.method, path=path)
        response.headers["X-Request-Id"] = request_id
        if not resolved.debug:
            for header, value in SECURITY_HEADERS.items():
                response.headers.setdefault(header, value)
        return response

    @app.middleware("http")
    async def enforce_body_limit(request: Request, call_next: Any) -> Response:
        limit = (
            request.app.state.settings_max_body
            if hasattr(request.app.state, "settings_max_body")
            else None
        )
        max_bytes = limit or _max_body_bytes(request)

        def too_large() -> Response:
            return from_app_error(PayloadTooLarge(max_bytes), instance=request.url.path)

        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > max_bytes:
            return too_large()
        if "chunked" in (request.headers.get("transfer-encoding") or "").lower():
            # A chunked body declares no length, so the check above can never
            # fire for one: an anonymous client could stream gigabytes straight
            # through to an endpoint that only parses the first few kilobytes of
            # JSON. Reading it here and cutting at the limit both stops the
            # transfer and refuses it, and caching the result in ``_body`` hands
            # the app what arrived.
            body = b""
            async for chunk in request.stream():
                body += chunk
                if len(body) > max_bytes:
                    return too_large()
            request._body = body
        return await call_next(request)

    install_exception_handlers(app)

    app.include_router(system_router.router, prefix=API_PREFIX)
    app.include_router(system_router.keys_router, prefix=API_PREFIX)
    app.include_router(resources_router.router, prefix=API_PREFIX)
    app.include_router(zones_router.router, prefix=API_PREFIX)
    app.include_router(events_router.router, prefix=API_PREFIX)
    app.include_router(sessions_router.router, prefix=API_PREFIX)
    app.include_router(ws_router, prefix=API_PREFIX)

    @app.get("/", include_in_schema=False)
    def root() -> Response:
        if _spa_dir(resolved).exists():
            return FileResponse(_spa_dir(resolved) / "index.html")
        return JSONResponse(
            {
                "name": "VisionOps",
                "version": resolved.version,
                "docs": f"{API_PREFIX}/docs",
                "health": f"{API_PREFIX}/healthz",
            }
        )

    _mount_spa(app, resolved)
    return app


def _max_body_bytes(request: Request) -> int:
    container = getattr(request.app.state, "container", None)
    if container is not None:
        return int(container.settings.max_body_bytes)
    return 1_048_576


#: An acceptable inbound request id: what we mint ourselves, plus the shapes a
#: tracing proxy sends (W3C ``traceparent`` ids, ULIDs). Anything else — most
#: importantly a value containing a newline — is replaced rather than echoed
#: into logs and the response header, so a caller cannot forge log lines.
REQUEST_ID_PATTERN = re.compile(r"\A[A-Za-z0-9._:-]{1,128}\Z")


def _request_id(request: Request) -> str:
    supplied = request.headers.get("x-request-id", "")
    if REQUEST_ID_PATTERN.match(supplied):
        return supplied
    if supplied:
        # The value itself is never logged: logging the raw header would be the
        # very forgery this is guarding against.
        logger.debug("discarding an unusable X-Request-Id (%d bytes)", len(supplied))
    return new_id()


def _template_path(request: Request) -> str:
    """Collapse ids out of the path so metric labels stay low-cardinality."""
    parts: list[str] = []
    for segment in request.url.path.split("/"):
        parts.append("{id}" if _looks_like_id(segment) else segment)
    return "/".join(parts) or "/"


def _looks_like_id(segment: str) -> bool:
    if len(segment) < 8:
        return False
    return all(char.isdigit() or char in "-_" for char in segment)


def _spa_dir(settings: Settings) -> Path:
    return Path(settings.data_dir).parent / "frontend" / "dist"


def _mount_spa(app: FastAPI, settings: Settings) -> None:
    """Serve the built SPA when it exists, so production is a single origin."""
    dist = _spa_dir(settings)
    if not dist.exists():
        logger.info("frontend build not found at %s — serving the API only", dist)
        return
    assets = dist / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str) -> Response:
        if full_path.startswith(("api/", "healthz", "readyz")):
            return problem_response(
                status=404,
                code="not_found",
                title="Not found",
                detail="The requested resource does not exist.",
                instance=f"/{full_path}",
            )
        candidate = dist / full_path
        if full_path and candidate.is_file() and _within(dist, candidate):
            return FileResponse(candidate)
        return FileResponse(dist / "index.html")

    logger.info("serving the web app from %s", dist)


def _within(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


__all__ = ["API_PREFIX", "create_app"]
