"""Session lifecycle: one worker thread per active camera.

The runner owns the loop that calls :meth:`VisionPipeline.step`, applies source
retry with exponential backoff, and tears everything down cleanly. Session
capacity is a hard cap, not a suggestion.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ..core.errors import CapacityExceeded, NotFound, ValidationError
from ..core.ids import new_id
from ..core.logging import log_context
from ..ingest.base import SourceOpenError, VideoSource, encode_jpeg
from ..ingest.factory import build_source
from ..observability.metrics import (
    METRIC_FRAME_AGE,
    METRIC_FRAMES,
    METRIC_FRAMES_DROPPED,
    METRIC_PIPELINE_FPS,
    METRIC_SESSION_UPTIME,
    METRIC_SESSIONS_ACTIVE,
    METRIC_SOURCE_ERRORS,
    METRIC_SOURCE_RETRIES,
    METRIC_SOURCE_STATE,
    registry,
)
from ..realtime.hub import RealtimeHub, session_topic
from ..vision.contracts import EventRecord, ModelInfo, SourceState
from ..vision.onnx_model import ModelError
from ..vision.pipeline import PipelineConfig, VisionPipeline
from ..vision.render import annotate
from ..vision.tracker import TrackerConfig

logger = logging.getLogger(__name__)

#: How long the worker waits for a frame before re-checking the stop flag.
_IDLE_SLEEP = 0.05

#: Reason codes that mean the session ended because something gave up, so the
#: lifecycle event written on exit is ``SOURCE_FAILED`` rather than
#: ``SOURCE_STOPPED``. A stopped camera and a broken camera must not look alike
#: in the event log an operator reads the morning after.
PERMANENT_FAILURES = frozenset(
    {
        "PIPELINE_ERROR",
        "SESSION_ERROR",
        "SOURCE_ENDED",
        "SOURCE_FAILED",
        "FILE_NOT_FOUND",
        "FILE_DECODE_ERROR",
        "MEDIA_ROOT_VIOLATION",
        "MEDIA_ROOT_NOT_CONFIGURED",
        "DEVICE_OPEN_FAILED",
    }
)


@dataclass(slots=True)
class SessionState:
    """Everything the API and UI need to know about a running session."""

    session_id: str
    camera_id: str
    camera_name: str
    state: str = "starting"
    started_at: float = 0.0
    target_fps: float = 5.0
    source_kind: str = ""
    source_id: str = ""
    reason_code: str | None = None
    detail: str | None = None
    frames_processed: int = 0
    frames_dropped: int = 0
    events_emitted: int = 0
    events_suppressed: int = 0
    active_tracks: int = 0
    in_zone: dict[str, int] = field(default_factory=dict)
    processed_fps: float = 0.0
    frame_age_ms: float | None = None
    last_inference_ms: float | None = None
    inference_errors: int = 0
    ai_state: str = "ready"
    ai_reason: str | None = None
    ai_detail: str | None = None
    latest_frame_jpeg: bytes | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "camera_id": self.camera_id,
            "camera_name": self.camera_name,
            "state": self.state,
            "started_at": self.started_at,
            "uptime_seconds": round(max(0.0, time.time() - self.started_at), 1)
            if self.started_at
            else 0.0,
            "target_fps": self.target_fps,
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "reason_code": self.reason_code,
            "detail": self.detail,
            "frames_processed": self.frames_processed,
            "frames_dropped": self.frames_dropped,
            "events_emitted": self.events_emitted,
            "events_suppressed": self.events_suppressed,
            "active_tracks": self.active_tracks,
            "in_zone": self.in_zone,
            "processed_fps": self.processed_fps,
            "frame_age_ms": self.frame_age_ms,
            "inference_ms": self.last_inference_ms,
            "inference_errors": self.inference_errors,
            "ai": {
                "state": self.ai_state,
                "reason_code": self.ai_reason,
                "detail": self.ai_detail,
            },
            "source": {
                "state": "running" if self.state == "running" else self.state,
                "kind": self.source_kind,
                "fps": self.processed_fps,
                "reason_code": self.reason_code,
                "detail": self.detail,
            },
        }


class _EventRecorder:
    """The :class:`EventSink` implementation: persist first, then publish.

    This is the only place the pipeline learns where events go, which is what
    makes ``EventSink`` the right seam for future notifiers and webhooks.
    """

    def __init__(
        self,
        *,
        principal: Any,
        event_service: Any,
        hub: RealtimeHub,
    ) -> None:
        self._session: SessionRunner | None = None
        self._principal = principal
        self._events = event_service
        self._hub = hub

    def emit(self, events: Any) -> None:
        # ``EventRecord`` is frozen, so an id discovered at write time is applied
        # by replacement. The database is authoritative for the id it stored.
        records: list[EventRecord] = [
            record if record.id is not None else replace(record, id=new_id()) for record in events
        ]
        rows = self._events.record(self._principal, records)
        for index, record in enumerate(records):
            if index < len(rows) and record.id != rows[index].id:
                records[index] = replace(record, id=rows[index].id)
        if records and self._session is not None:
            self._session.broadcast_events(records)

    def publish_state(self, state: dict[str, Any]) -> None:
        if self._session is not None:
            self._session.ingest_state(state)

    def close(self) -> None:
        return None


class SessionRunner:
    """Owns one pipeline thread for one camera."""

    def __init__(
        self,
        *,
        session_id: str,
        camera_id: str,
        camera_name: str,
        source: VideoSource,
        pipeline: VisionPipeline,
        hub: RealtimeHub,
        state: SessionState,
    ) -> None:
        self.session_id = session_id
        self.camera_id = camera_id
        self.camera_name = camera_name
        self.source = source
        self.pipeline = pipeline
        self._hub = hub
        self.state = state
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._latest_jpeg: bytes | None = None

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name=f"visionops-session-{self.session_id[:8]}", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        try:
            self.source.close()
        except Exception:
            logger.debug("error closing source for session %s", self.session_id)
        self.pipeline.close()
        registry.drop_label_value("session", self.session_id)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- the loop -----------------------------------------------------------

    def _run(self) -> None:
        with log_context(session_id=self.session_id):
            self.state.started_at = time.time()
            self.state.state = "starting"
            attempts = 0
            backoff = self._backoff_base
            while not self._stop.is_set():
                try:
                    if not self._source_opened:
                        self.source.open()
                        self._source_opened = True
                        attempts = 0
                        self.state.state = "running"
                        self.state.reason_code = None
                        self.state.detail = None
                        registry.counter(
                            "visionops_source_frames_total", 0, kind=self.state.source_kind
                        )
                    alive = self._step()
                    if not alive:
                        raise SourceOpenError(
                            self.source.health.reason_code or "SOURCE_ENDED",
                            self.source.health.detail or "the source returned no more frames",
                        )
                except SourceOpenError as exc:
                    attempts += 1
                    registry.counter(
                        METRIC_SOURCE_ERRORS, 1, kind=self.state.source_kind, reason=exc.reason_code
                    )
                    self.state.state = "degraded" if attempts <= self._max_retries else "failed"
                    self.state.reason_code = exc.reason_code
                    self.state.detail = exc.detail
                    self.source.health.state = (
                        SourceState.DEGRADED
                        if self.state.state == "degraded"
                        else SourceState.FAILED
                    )
                    if attempts > self._max_retries:
                        logger.error(
                            "session %s failed permanently: %s", self.session_id, exc.detail
                        )
                        self._stop.set()
                        break
                    registry.counter(METRIC_SOURCE_RETRIES, 1, kind=self.state.source_kind)
                    delay = min(backoff * (2 ** (attempts - 1)), 30.0)
                    logger.warning(
                        "session %s source error (%s); retry %d/%d in %.1fs",
                        self.session_id,
                        exc.reason_code,
                        attempts,
                        self._max_retries,
                        delay,
                    )
                    if self._sleep_or_stop(delay):
                        break
                    self._reopen()
                except ModelError as exc:
                    # The pipeline already turned this into a state; a model
                    # failure is not a source problem, so we keep looping.
                    self.state.reason_code = exc.reason_code
                    self.state.detail = exc.detail
                    self.state.ai_state = "degraded"
                    if self._sleep_or_stop(1.0):
                        break
                except Exception as exc:
                    # Anything unforeseen is a *diagnosis*, not a reason to lose
                    # the session. Without this arm the worker thread dies,
                    # ``state.state`` stays "starting" forever, the manager keeps
                    # counting a corpse against capacity, and no lifecycle event
                    # is ever written.
                    logger.exception(
                        "unexpected error in session %s (%s)", self.session_id, type(exc).__name__
                    )
                    self.state.state = "degraded"
                    self.state.reason_code = "SESSION_ERROR"
                    self.state.detail = "an internal error occurred; see the server logs"
                    registry.counter(
                        METRIC_SOURCE_ERRORS,
                        1,
                        kind=self.state.source_kind,
                        reason="SESSION_ERROR",
                    )
                    if self._sleep_or_stop(1.0):
                        break
            self.state.state = "stopped"
            failed = self.state.reason_code in PERMANENT_FAILURES
            self._emit_source_event(
                "SOURCE_FAILED" if failed else "SOURCE_STOPPED",
                {
                    "duration_seconds": round(time.time() - self.state.started_at, 1),
                    "reason_code": self.state.reason_code,
                },
            )
            registry.gauge(METRIC_SOURCE_STATE, 0, kind=self.state.source_kind, state="stopped")

    def _step(self) -> bool:
        """One pipeline iteration. Returns ``False`` when the source ended."""
        try:
            alive = self.pipeline.step()
        except Exception:
            logger.exception("pipeline step failed for session %s", self.session_id)
            self.state.state = "degraded"
            self.state.reason_code = "PIPELINE_ERROR"
            self.state.detail = "an internal pipeline error occurred; see the server logs"
            self._sleep_or_stop(0.5)
            return True
        if not alive:
            return False
        self._capture_preview()
        return True

    def _capture_preview(self) -> None:
        """Keep one annotated JPEG for the MJPEG preview endpoint (best-effort)."""
        frame = self.pipeline.last_frame
        if frame is None:
            return
        try:
            annotated = annotate(frame.image, self.pipeline.zones, self.pipeline.last_tracks)
            self._latest_jpeg = encode_jpeg(annotated, quality=65)
        except Exception:
            logger.debug("preview encode failed for session %s", self.session_id, exc_info=True)

    def _sleep_or_stop(self, seconds: float) -> bool:
        return self._stop.wait(seconds)

    def _reopen(self) -> None:
        self._source_opened = False
        self.source.reopen()

    # -- state --------------------------------------------------------------

    def ingest_state(self, state: dict[str, Any]) -> None:
        stats = state.get("stats", {})
        self.state.frames_processed = int(stats.get("frames_processed", 0))
        self.state.frames_dropped = int(stats.get("frames_dropped", 0))
        self.state.events_emitted = int(stats.get("events_emitted", 0))
        self.state.events_suppressed = int(stats.get("events_suppressed", 0))
        self.state.active_tracks = int(stats.get("active_tracks", 0))
        self.state.in_zone = dict(stats.get("in_zone", {}))
        self.state.processed_fps = float(stats.get("processed_fps", 0.0))
        self.state.frame_age_ms = stats.get("frame_age_ms")
        self.state.inference_errors = int(stats.get("inference_errors", 0))
        ai = state.get("ai", {})
        self.state.ai_state = str(ai.get("state", "ready"))
        self.state.ai_reason = ai.get("reason_code")
        self.state.ai_detail = ai.get("detail")
        self.state.last_inference_ms = ai.get("latency_ms")
        source = state.get("source", {})
        if source.get("state") in ("running", "degraded", "failed"):
            self.state.state = str(source["state"])
        self.state.reason_code = source.get("reason_code")
        self.state.detail = source.get("detail")

        session = self.session_id
        registry.counter(METRIC_FRAMES, self.state.frames_processed, session=session)
        registry.counter(
            METRIC_FRAMES_DROPPED, self.state.frames_dropped, session=session, reason="rate_limit"
        )
        registry.gauge(METRIC_PIPELINE_FPS, self.state.processed_fps, session=session)
        if self.state.frame_age_ms is not None:
            registry.gauge(METRIC_FRAME_AGE, self.state.frame_age_ms, session=session)
        registry.gauge("visionops_active_tracks", self.state.active_tracks, session=session)
        registry.gauge(
            METRIC_SESSION_UPTIME, max(0.0, time.time() - self.state.started_at), session=session
        )
        kind = self.state.source_kind
        for value in ("running", "degraded", "failed", "stopped"):
            registry.gauge(
                METRIC_SOURCE_STATE, 1 if value == self.state.state else 0, kind=kind, state=value
            )

        self._hub.publish(session_topic(self.session_id), state)

    def broadcast_events(self, records: list[EventRecord]) -> None:
        self._hub.publish(
            session_topic(self.session_id),
            {
                "type": "session.event",
                "session_id": self.session_id,
                "server_time": time.time(),
                "events": [
                    {
                        "id": record.id,
                        "type": record.type,
                        "severity": str(record.severity_value()),
                        "occurred_at": record.occurred_at,
                        "camera_id": record.camera_id,
                        "camera_name": record.camera_name,
                        "zone_id": record.zone_id,
                        "zone_name": record.zone_name,
                        "track_id": record.track_id,
                        "confidence": record.confidence,
                        "metadata": record.metadata,
                        "snapshot_path": record.snapshot_path,
                    }
                    for record in records
                ],
            },
        )

    def _emit_source_event(self, event_type: str, metadata: dict[str, Any]) -> None:
        """Emit a SOURCE_* lifecycle event through the session's event sink.

        Anything that goes wrong here is logged and swallowed: a session that
        cannot record why it started or stopped must still shut down cleanly.
        """
        try:
            sink = self.pipeline.event_sink
            if sink is None:
                return
            from ..vision.event_engine import EVENT_REGISTRY, validate_event_metadata

            validate_event_metadata(event_type, metadata)
            record = EventRecord(
                type=event_type,
                severity=EVENT_REGISTRY[event_type].severity,
                occurred_at=time.time(),
                camera_id=self.camera_id,
                camera_name=self.camera_name,
                session_id=self.session_id,
                metadata=metadata,
                id=new_id(),
                source_kind=self.state.source_kind,
            )
            sink.emit([record])
        except Exception:
            logger.debug(
                "could not emit %s for session %s", event_type, self.session_id, exc_info=True
            )

    @property
    def latest_jpeg(self) -> bytes | None:
        return self._latest_jpeg

    def snapshot_state(self) -> dict[str, Any]:
        """Current state in the *broadcast* shape.

        A subscriber that connects mid-session gets this as its first message,
        so it must be identical to what :meth:`VisionPipeline.state_payload`
        produces. Sending :meth:`SessionState.to_dict` instead would look right
        and be silently dropped by the client, which only reads
        ``type == "session.state"``.
        """
        stats = self.state.to_dict()
        return {
            "type": "session.state",
            "session_id": self.state.session_id,
            "camera_id": self.state.camera_id,
            "camera_name": self.state.camera_name,
            "seq": None,
            "server_time": time.time(),
            "source": stats["source"],
            "ai": {
                "state": self.state.ai_state,
                "reason_code": self.state.ai_reason,
                "detail": self.state.ai_detail,
            },
            "stats": stats,
            "tracks": [],
            "events": [],
            "zones": [],
        }

    # Set by the manager.
    _source_opened: bool = False
    _max_retries: int = 5
    _backoff_base: float = 2.0


class SessionManager:
    """Registry and capacity gate for active sessions."""

    def __init__(
        self,
        *,
        max_sessions: int,
        hub: RealtimeHub,
        event_service: Any,
        settings: Any,
        model_info: ModelInfo | None,
        model: Any,
        snapshot_enabled: bool = False,
        source_backoff_seconds: float = 2.0,
        max_retries: int = 5,
    ) -> None:
        self._max = max(1, max_sessions)
        self._hub = hub
        self._events = event_service
        self._settings = settings
        self._model_info = model_info
        self._model = model
        self._snapshot_enabled = snapshot_enabled
        self._backoff = source_backoff_seconds
        self._max_retries = max_retries
        self._sessions: dict[str, SessionRunner] = {}
        self._by_camera: dict[str, str] = {}
        # Reentrant: ``start`` holds the lock while calling ``for_camera`` and
        # ``_reap``, both of which take it themselves.
        self._lock = threading.RLock()

    # -- accessors ----------------------------------------------------------

    @property
    def capacity(self) -> int:
        return self._max

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def get(self, session_id: str) -> SessionRunner:
        with self._lock:
            runner = self._sessions.get(session_id)
        if runner is None:
            raise NotFound("session", session_id)
        return runner

    def for_camera(self, camera_id: str) -> SessionRunner | None:
        with self._lock:
            session_id = self._by_camera.get(camera_id)
            return self._sessions.get(session_id) if session_id else None

    def get_or_none(self, session_id: str) -> SessionRunner | None:
        with self._lock:
            return self._sessions.get(session_id)

    def states(self) -> list[SessionState]:
        """A snapshot of every session's state.

        Copied under the lock: an HTTP thread iterating this while a session
        thread starts or stops would otherwise raise "dictionary changed size
        during iteration" straight into a 500.
        """
        with self._lock:
            return [runner.state for runner in list(self._sessions.values())]

    def _reap(self) -> int:
        """Forget sessions whose worker thread has exited. Caller holds the lock.

        A session that ended is still a row in the registry until something
        removes it. Left alone, ``max_sessions`` of them turns every later start
        into a permanent ``503 capacity_exceeded`` even though nothing is
        running.
        """
        dead = [session_id for session_id, runner in self._sessions.items() if not runner.is_alive()]
        for session_id in dead:
            runner = self._sessions.pop(session_id, None)
            if runner is None:  # pragma: no cover - defensive
                continue
            if self._by_camera.get(runner.camera_id) == session_id:
                self._by_camera.pop(runner.camera_id, None)
            registry.drop_label_value("session", session_id)
            logger.info("session %s ended and was reaped", session_id)
        return len(dead)

    # -- lifecycle ----------------------------------------------------------

    def start(
        self,
        *,
        principal: Any,
        camera: Any,
        source_row: Any,
        target_fps: float,
        zone_specs: list[Any],
    ) -> tuple[SessionRunner, bool]:
        """Start a session for a camera. Returns ``(runner, created)``.

        Idempotent: starting an already-running camera returns the existing
        session instead of failing, which keeps the UI simple.
        """
        with self._lock:
            self._reap()
            existing = self.for_camera(camera.id)
            if existing is not None and existing.is_alive():
                return existing, False
            if len(self._sessions) >= self._max:
                raise CapacityExceeded(
                    f"{self._max} session(s) are already running (VISIONOPS_MAX_SESSIONS). "
                    "Stop one to start another."
                )

            if self._model is None or self._model_info is None:
                raise ValidationError(
                    "the detection model is not loaded, so no session can start. "
                    "Check System status for the reason.",
                    [{"field": "model", "message": "model unavailable"}],
                )

            session_id = new_id()
            source = build_source(
                kind=source_row.kind,
                target=source_row.target,
                source_id=source_row.id,
                options=_source_options(source_row),
                secret_box=getattr(self, "_secret_box", None),
                media_root=self._settings.media_root,
                open_timeout_ms=self._settings.source_open_timeout_ms,
                session_id=session_id,
                target_fps=target_fps,
            )
            recorder = _EventRecorder(
                principal=principal,
                event_service=self._events,
                hub=self._hub,
            )
            pipeline = VisionPipeline(
                camera_id=camera.id,
                camera_name=camera.name,
                session_id=session_id,
                source=source,
                model=self._model,
                sink=recorder,
                model_info=self._model_info,
                zones=zone_specs,
                config=PipelineConfig(
                    target_fps=target_fps,
                    publish_fps=self._settings.publish_fps,
                    tracker=self._settings.tracker,
                    tracker_config=TrackerConfig(
                        match_iou=self._settings.tracker_match_iou,
                        max_age=self._settings.tracker_max_age,
                        min_hits=self._settings.tracker_min_hits,
                    ),
                    snapshot_callback=self._snapshot_callback() if self._snapshot_enabled else None,
                ),
            )
            state = SessionState(
                session_id=session_id,
                camera_id=camera.id,
                camera_name=camera.name,
                target_fps=target_fps,
                source_kind=source_row.kind,
                source_id=source_row.id,
            )
            runner = SessionRunner(
                session_id=session_id,
                camera_id=camera.id,
                camera_name=camera.name,
                source=source,
                pipeline=pipeline,
                hub=self._hub,
                state=state,
            )
            runner._max_retries = self._max_retries
            runner._backoff_base = self._backoff
            recorder._session = runner  # bind the sink now that the runner exists

            self._sessions[session_id] = runner
            self._by_camera[camera.id] = session_id
            runner.start()
            registry.gauge(METRIC_SESSIONS_ACTIVE, len(self._sessions))
            logger.info(
                "session %s started for camera %s (source=%s kind=%s fps=%.1f)",
                session_id,
                camera.name,
                source_row.name,
                source_row.kind,
                target_fps,
            )
            self._emit_started(runner, principal, source_row, target_fps)
            return runner, True

    def _emit_started(
        self, runner: SessionRunner, principal: Any, source_row: Any, target_fps: float
    ) -> None:
        runner._emit_source_event(
            "SOURCE_STARTED",
            {
                "source_kind": source_row.kind,
                "source_name": source_row.name,
                "target_fps": target_fps,
                "camera_name": runner.camera_name,
            },
        )

    def stop(self, camera_id: str) -> None:
        with self._lock:
            session_id = self._by_camera.get(camera_id)
            runner = self._sessions.pop(session_id, None) if session_id else None
            if session_id:
                self._by_camera.pop(camera_id, None)
        if runner is not None:
            runner.stop()
            registry.gauge(METRIC_SESSIONS_ACTIVE, self.active_count)
            logger.info("session %s stopped", runner.session_id)

    def stop_all(self) -> None:
        with self._lock:
            runners = list(self._sessions.values())
            self._sessions.clear()
            self._by_camera.clear()
        for runner in runners:
            runner.stop(timeout=2.0)
        registry.gauge(METRIC_SESSIONS_ACTIVE, 0)

    def refresh_zones(self, camera_id: str, zone_specs: list[Any]) -> None:
        """Apply edited zones to a running session without restarting it."""
        runner = self.for_camera(camera_id)
        if runner is not None:
            runner.pipeline.set_zones(zone_specs)

    def set_secret_box(self, secret_box: Any) -> None:
        self._secret_box = secret_box

    def _snapshot_callback(self) -> Callable[[EventRecord, Any], str | None]:
        from .snapshot import SnapshotWriter

        writer = SnapshotWriter(root=Path(self._settings.snapshot_dir), camera_id_label="unknown")
        return writer.write

    _secret_box: Any = None


def _source_options(source_row: Any) -> dict[str, Any]:
    options = dict(source_row.options or {})
    if source_row.credential_blob:
        options["credential_blob"] = source_row.credential_blob
    return options


__all__ = ["SessionManager", "SessionRunner", "SessionState"]
