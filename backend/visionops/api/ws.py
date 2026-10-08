"""WebSocket endpoints: live pipeline state out, browser frames in.

Two sockets, two directions, deliberately separate:

* ``/stream/sessions/{session_id}`` — server → client live state (JSON)
* ``/stream/ingest?session_id=…``     — client → server JPEG frames (binary)

Keeping them apart means a browser that loses its state socket can re-sync over
REST without interrupting frame delivery.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from ..core.logging import current_request_id, log_context
from ..ingest.browser import BrowserFrameSource
from ..realtime.hub import session_topic
from .deps import UNMAPPED, required_permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/stream", tags=["realtime"])
CLOSE_UNAUTHORIZED = 4401
CLOSE_NOT_FOUND = 4404
CLOSE_BAD_REQUEST = 4400
CLOSE_TOO_LARGE = 4409
CLOSE_FORBIDDEN = 4403
STALE_AFTER_SECONDS = 3.0

#: Returned by :func:`_authorise` for a *known* key that lacks the grant. A
#: sentinel rather than ``None``, because ``None`` already means "no valid
#: credential" and the two must not collapse into one 4401 — a 4403 tells an
#: operator the key works but is under-privileged, which is a different bug.
FORBIDDEN = object()


def _stream_path(websocket: WebSocket) -> str:
    """The API path a socket maps to, so one mapping authorises every route."""
    root = websocket.scope.get("root_path", "")
    return f"{root}{websocket.url.path}"


async def _authorise(websocket: WebSocket, container: Any) -> Any:
    """Authenticate *and* authorise a WebSocket using the same rules as HTTP.

    The browser API cannot set headers on a WebSocket handshake, so the key may
    arrive as a query parameter — it is never logged and never stored.

    Authenticating is not enough: a key minted with no grants could otherwise
    subscribe to any session's live state and push frames into a running camera,
    because route dependencies are not evaluated for WebSocket routes. The
    grant is read from the same :data:`~visionops.api.deps.ROUTE_PERMISSIONS`
    table HTTP uses, so a new socket route is denied until someone maps it.
    """
    from ..core.auth import extract_credentials, rate_limit_key
    from ..core.errors import Unauthorized
    from ..db.scoped import Principal as ResolvedPrincipal

    credentials = extract_credentials(websocket) or websocket.query_params.get("key")
    if not credentials:
        return None
    try:
        with container.session_factory() as session:
            principal = await container.auth.resolve(credentials, session)
    except Unauthorized:
        return None
    except Exception:
        logger.debug("websocket authentication raised", exc_info=True)
        return None
    if principal is None:
        # Failed keys get the same throttling as HTTP, so a socket is not a way
        # around the auth-failure limit.
        fingerprint = rate_limit_key(credentials)
        if not container.auth_limiter.allow(fingerprint):
            logger.warning("websocket authentication is rate limited: key=%s", fingerprint)
        return None
    permission = required_permission(_stream_path(websocket), "GET")
    if permission == UNMAPPED:
        # Not a real permission, so it can never be satisfied: a socket route
        # with no entry in the table is refused by default, exactly as the HTTP
        # path refuses it — and a wildcard key does not get to sweep it up.
        logger.warning("refusing a websocket: %s has no permission mapping", websocket.url.path)
        return FORBIDDEN
    if permission is None:
        return principal
    if not isinstance(principal, ResolvedPrincipal) or not principal.can(permission):
        logger.warning(
            "refusing a websocket: path=%s requires %r", websocket.url.path, permission
        )
        return FORBIDDEN
    return principal


@router.websocket("/sessions/{session_id}")
async def live_state(websocket: WebSocket, session_id: str) -> None:
    """Server → client live pipeline state for one session."""
    container = getattr(websocket.app.state, "container", None)
    if container is None:  # pragma: no cover - broken startup
        await websocket.close(code=CLOSE_BAD_REQUEST)
        return
    principal = await _authorise(websocket, container)
    if principal is None:
        await websocket.close(code=CLOSE_UNAUTHORIZED)
        return
    if principal is FORBIDDEN:
        await websocket.close(code=CLOSE_FORBIDDEN)
        return
    if container.sessions.get_or_none(session_id) is None:
        await websocket.close(code=CLOSE_NOT_FOUND)
        return

    await websocket.accept()
    with log_context(session_id=session_id, request_id=current_request_id()):

        async def send(payload: dict[str, Any]) -> None:
            await websocket.send_text(json.dumps(payload, default=str))

        subscription = await container.hub.subscribe(session_topic(session_id), send)
        try:
            # Send the current state immediately so the overlay is never blank.
            # It must be the *same* shape the broadcasts use, or the client
            # ignores it (it only reads ``session.state``) and the overlay waits
            # for the next publish anyway — which is exactly what this was meant
            # to avoid.
            runner = container.sessions.get(session_id)
            await send(runner.snapshot_state())
            while True:
                message = await websocket.receive_text()
                await _handle_client_message(message, send, container, session_id)
        except WebSocketDisconnect:
            pass
        except Exception:
            logger.debug("live-state socket closed unexpectedly", exc_info=True)
        finally:
            await container.hub.unsubscribe(subscription)


@router.websocket("/ingest")
async def ingest_frames(
    websocket: WebSocket,
    session_id: str = Query(..., min_length=8),
    target_fps: float = Query(5.0, gt=0, le=30),
    width: int = Query(0, ge=0, le=16384),
    height: int = Query(0, ge=0, le=16384),
) -> None:
    """Client → server JPEG frames for a browser-webcam session."""
    container = getattr(websocket.app.state, "container", None)
    if container is None:  # pragma: no cover - broken startup
        await websocket.close(code=CLOSE_BAD_REQUEST)
        return
    principal = await _authorise(websocket, container)
    if principal is None:
        await websocket.close(code=CLOSE_UNAUTHORIZED)
        return
    if principal is FORBIDDEN:
        await websocket.close(code=CLOSE_FORBIDDEN)
        return

    runner = container.sessions.get_or_none(session_id)
    if runner is None or not isinstance(runner.source, BrowserFrameSource):
        await websocket.close(code=CLOSE_NOT_FOUND)
        return
    source: BrowserFrameSource = runner.source
    source.target_fps = target_fps

    await websocket.accept()
    with log_context(session_id=session_id, request_id=current_request_id()):
        await websocket.send_text(
            json.dumps(
                {
                    "type": "ingest.hello",
                    "session_id": session_id,
                    "target_fps": target_fps,
                    "max_frame_bytes": container.settings.ws_max_frame_bytes,
                }
            )
        )
        source.attach_client(width=width, height=height)
        try:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                payload = message.get("bytes")
                if payload:
                    if len(payload) > container.settings.ws_max_frame_bytes:
                        await websocket.close(code=CLOSE_TOO_LARGE)
                        break
                    source.push_frame(payload, source_timestamp=time.time())
                    continue
                text = message.get("text")
                if text:
                    _handle_ingest_control(source, text)
        except WebSocketDisconnect:
            pass
        except Exception:
            logger.debug("ingest socket closed unexpectedly", exc_info=True)
        finally:
            source.detach_client()


def _handle_ingest_control(source: BrowserFrameSource, text: str) -> None:
    try:
        message = json.loads(text)
    except ValueError:
        return
    if not isinstance(message, dict):
        return
    if message.get("type") == "set_target_fps":
        value = message.get("value")
        if isinstance(value, (int, float)):
            source.target_fps = max(0.5, min(30.0, float(value)))


async def _handle_client_message(text: str, send: Any, container: Any, session_id: str) -> None:
    try:
        message = json.loads(text)
    except ValueError:
        return
    if not isinstance(message, dict):
        return
    kind = message.get("type")
    if kind == "ping":
        await send({"type": "pong", "server_time": time.time()})
    elif kind == "get_state":
        try:
            await send(container.sessions.get(session_id).snapshot_state())
        except Exception:
            await send({"type": "session.error", "detail": "session is not available"})


__all__ = ["CLOSE_NOT_FOUND", "CLOSE_UNAUTHORIZED", "STALE_AFTER_SECONDS", "router"]
