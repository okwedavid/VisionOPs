"""The per-session orchestration loop.

One pipeline instance per active session. It owns a source, a model, a tracker,
zone strategies and an event sink, and it is the only place that knows the
order of operations:

    read → rate-limit → infer → NMS → track → zone → event → persist → publish

Failure handling follows one rule: a data-plane problem becomes a **state**, not
an exception that kills the loop.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from .contracts import (
    EvalContext,
    EventRecord,
    EventSink,
    Frame,
    ModelInfo,
    SourceHealth,
    SourceKind,
    SourceState,
    Track,
    VisionModel,
    ZoneSpec,
)
from .event_engine import EventEngine
from .onnx_model import ModelError
from .postprocess import EDGE_EPSILON
from .registry import build_zone_strategy
from .tracker import TrackerConfig

logger = logging.getLogger(__name__)

#: A source with no frame for this long is considered stalled.
STALLED_FRAME_SECONDS = 10.0
#: Consecutive inference failures before the model is declared FAILED.
MAX_CONSECUTIVE_INFERENCE_ERRORS = 3


@dataclass(slots=True)
class PipelineConfig:
    """Per-session knobs. All overridable per camera."""

    target_fps: float = 5.0
    publish_fps: float = 10.0
    tracker: str = "kalman-iou"
    tracker_config: TrackerConfig = field(default_factory=TrackerConfig)
    snapshot_callback: Callable[[EventRecord, Frame], str | None] | None = None


@dataclass(slots=True)
class PipelineStats:
    """Live counters exposed to the UI and to /system/status."""

    frames_processed: int = 0
    frames_dropped: int = 0
    inference_errors: int = 0
    events_emitted: int = 0
    events_suppressed: int = 0
    last_frame_at: float | None = None
    last_inference_ms: float | None = None
    started_at: float = 0.0

    @property
    def frame_age_seconds(self) -> float | None:
        if self.last_frame_at is None:
            return None
        return max(0.0, time.time() - self.last_frame_at)


class VisionPipeline:
    """Frame in → events out, for a single camera session."""

    def __init__(
        self,
        *,
        camera_id: str,
        camera_name: str,
        session_id: str,
        source: Any,
        model: VisionModel,
        sink: EventSink,
        model_info: ModelInfo,
        zones: Sequence[ZoneSpec] = (),
        config: PipelineConfig | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.camera_id = camera_id
        self.camera_name = camera_name
        self.session_id = session_id
        self._source = source
        self._model = model
        self._sink = sink
        self._model_info = model_info
        self._config = config or PipelineConfig()
        self._clock = clock

        self._zones: dict[str, ZoneSpec] = {zone.zone_id: zone for zone in zones}
        self._strategies: dict[str, Any] = {
            kind: build_zone_strategy(kind) for kind in {z.kind for z in zones}
        }
        self._zone_states: dict[str, dict[tuple[str, int], str]] = {
            zone.zone_id: {} for zone in zones
        }
        self._tracker = self._build_tracker()
        self._event_engine = EventEngine()

        self.stats = PipelineStats(started_at=self._clock())
        self.source_health = SourceHealth(kind=getattr(source, "kind", SourceKind.FILE))
        self.model_health: str = "ready"
        self.model_reason: str | None = None
        self.model_detail: str | None = None
        self._min_frame_interval = 1.0 / max(0.1, self._config.target_fps)
        self._last_processed_at = 0.0
        self._last_published_at = 0.0
        self._stopping = False
        self._last_frame: Frame | None = None
        self._last_tracks: tuple[Track, ...] = ()

    @property
    def event_sink(self) -> EventSink:
        """The sink this pipeline publishes events to.

        Public because the session lifecycle (``SOURCE_STARTED`` /
        ``SOURCE_STOPPED``) is emitted through the *same* sink as detections,
        so ordering and back-pressure behave identically.
        """
        return self._sink

    @property
    def last_frame(self) -> Frame | None:
        """The most recently processed frame (used for MJPEG preview)."""
        return self._last_frame

    @property
    def last_tracks(self) -> tuple[Track, ...]:
        """The most recently reported tracks (used for preview annotation)."""
        return self._last_tracks

    def _build_tracker(self) -> Any:
        from .registry import build_tracker

        return build_tracker(self._config.tracker, self._config.tracker_config)

    # -- configuration ------------------------------------------------------

    def set_zones(self, zones: Sequence[ZoneSpec]) -> None:
        """Replace the zone set (called when an operator edits zones live)."""
        self._zones = {zone.zone_id: zone for zone in zones}
        for kind in {z.kind for z in zones}:
            self._strategies.setdefault(kind, build_zone_strategy(kind))
        for zone_id in list(self._zone_states):
            if zone_id not in self._zones:
                del self._zone_states[zone_id]
        for zone in zones:
            self._zone_states.setdefault(zone.zone_id, {})

    @property
    def zones(self) -> list[ZoneSpec]:
        return list(self._zones.values())

    # -- the loop -----------------------------------------------------------

    def step(self) -> bool:
        """Process at most one frame. Returns ``False`` when the source ended."""
        frame = self._source.read()
        now = self._clock()

        if frame is None:
            # An empty read is end-of-stream only when the source says so. A
            # browser tab that has not pushed yet, or a camera that stalled for
            # a moment, must not end the session: treating every None as EOF is
            # what made browser sessions die the instant they started.
            if bool(getattr(self._source, "at_end", False)):
                self.source_health.state = SourceState.FAILED
                self.source_health.reason_code = self.source_health.reason_code or "SOURCE_ENDED"
                self.source_health.detail = self.source_health.detail or "source returned no frame"
                self._mark_source_health(now)
                return False
            # Still waiting: keep whatever diagnosis the source gave us.
            self._mark_source_health(now)
            return True

        self.source_health.frames += 1
        self.source_health.last_frame_at = now
        self.source_health.state = SourceState.RUNNING
        self.source_health.reason_code = None
        self.source_health.detail = None

        # Rate control: drop rather than queue (freshness over completeness).
        if now - self._last_processed_at < self._min_frame_interval:
            self.stats.frames_dropped += 1
            self.source_health.dropped += 1
            self._maybe_publish(now, frame, tracks=(), events=())
            return True

        self._last_processed_at = now
        self.stats.last_frame_at = now
        self.stats.frames_processed += 1

        detections = self._infer(frame)
        tracks = self._track(detections, frame, now)
        events = self._evaluate_zones(tracks, frame, now)

        if events:
            self._persist(events, frame)
        self.stats.events_emitted += len(events)
        self._last_frame = frame
        self._last_tracks = tuple(tracks)
        self._maybe_publish(now, frame, tracks=tracks, events=events)
        return True

    def _infer(self, frame: Frame) -> list:
        try:
            detections = self._model.infer(frame)
        except ModelError as exc:
            self.stats.inference_errors += 1
            self.model_health = "degraded"
            self.model_reason = exc.reason_code
            self.model_detail = exc.detail
            self.source_health.state = SourceState.DEGRADED
            self.source_health.reason_code = exc.reason_code
            self.source_health.detail = exc.detail
            consecutive = getattr(self._model, "note_error", lambda: 1)()
            if consecutive >= MAX_CONSECUTIVE_INFERENCE_ERRORS:
                self.model_health = "failed"
                self.source_health.state = SourceState.FAILED
            logger.warning(
                "inference failed (%s) on session %s: %s",
                exc.reason_code,
                self.session_id,
                exc.detail,
            )
            return []
        except Exception as exc:
            self.stats.inference_errors += 1
            self.model_health = "failed"
            self.model_reason = "MODEL_ERROR"
            self.model_detail = f"unexpected inference failure: {exc}"
            self.source_health.state = SourceState.FAILED
            self.source_health.reason_code = "MODEL_ERROR"
            self.source_health.detail = self.model_detail
            logger.exception("unexpected inference failure on session %s", self.session_id)
            return []
        else:
            self.model_health = "ready"
            self.model_reason = None
            self.model_detail = None
            latency = getattr(self._model, "last_latency_ms", None)
            if latency is not None:
                self.stats.last_inference_ms = latency
            return detections

    def _track(self, detections: Sequence, frame: Frame, now: float) -> list[Track]:
        tracks = self._tracker.update(detections, frame.seq, now)
        # A track whose box touches the frame edge has an unreliable foot point.
        for track in tracks:
            if not track.clip and _touches_edge(track.box):
                object.__setattr__(track, "clip", True)
        return list(tracks)

    def _evaluate_zones(
        self, tracks: Sequence[Track], frame: Frame, now: float
    ) -> list[EventRecord]:
        if not self._zones:
            return []
        ctx = EvalContext(
            timestamp=now,
            frame_seq=frame.seq,
            frame_width=frame.width,
            frame_height=frame.height,
            model_name=self._model_info.name,
            model_sha256=self._model_info.sha256,
            source_kind=str(self.source_health.kind),
        )
        transitions = []
        for zone in self._zones.values():
            strategy = self._strategies.get(zone.kind)
            if strategy is None:
                continue
            transitions.extend(
                strategy.evaluate(tracks, zone, ctx, self._zone_states.setdefault(zone.zone_id, {}))
            )
        if not transitions:
            return []
        events = self._event_engine.handle_transitions(
            transitions,
            ctx,
            camera_id=self.camera_id,
            camera_name=self.camera_name,
            session_id=self.session_id,
            zones=self._zones,
        )
        suppressed = len(transitions) - len(events)
        if suppressed > 0:
            self.stats.events_suppressed += suppressed
        return events

    def _persist(self, events: Sequence[EventRecord], frame: Frame) -> None:
        """Hand events to the sink, attaching a snapshot path when enabled.

        ``EventRecord`` is frozen, so the path is applied by replacement and the
        updated records are what reaches the sink — otherwise the event would be
        stored without the evidence the operator asked for.
        """
        records = list(events)
        if self._config.snapshot_callback is not None:
            updated: list[EventRecord] = []
            for record in records:
                try:
                    path = self._config.snapshot_callback(record, frame)
                except Exception as exc:
                    logger.warning("snapshot failed for event %s: %s", record.type, exc)
                else:
                    if path:
                        record = replace(record, snapshot_path=path)
                updated.append(record)
            records = updated
        self._sink.emit(records)

    # -- state publishing ---------------------------------------------------

    def _maybe_publish(
        self,
        now: float,
        frame: Frame | None,
        *,
        tracks: Sequence[Track],
        events: Sequence[EventRecord],
    ) -> None:
        interval = 1.0 / max(0.5, min(self._config.publish_fps, self._config.target_fps * 2))
        if events or (now - self._last_published_at) >= interval:
            self._last_published_at = now
            self._mark_source_health(now)
            try:
                self._sink.publish_state(self.state_payload(frame, tracks, events))
            except Exception:
                logger.exception("failed to publish pipeline state")

    def _mark_source_health(self, now: float) -> None:
        health = self.source_health
        elapsed = max(1e-6, now - self.stats.started_at)
        health.uptime_seconds = elapsed
        health.fps = round(self.stats.frames_processed / elapsed, 2)
        age = self.stats.frame_age_seconds
        if health.state is SourceState.RUNNING and age is not None and age > STALLED_FRAME_SECONDS:
            health.state = SourceState.DEGRADED
            health.reason_code = "SOURCE_STALLED"
            health.detail = f"no new frames for {age:.0f}s"
        position = getattr(self._source, "position", None)
        duration = getattr(self._source, "duration", None)
        health.position = position() if callable(position) else position
        health.duration = duration() if callable(duration) else duration

    def state_payload(
        self,
        frame: Frame | None,
        tracks: Sequence[Track],
        events: Sequence[EventRecord],
    ) -> dict[str, Any]:
        """The live-state object broadcast over the WebSocket."""
        in_zone: dict[str, int] = {}
        for track in tracks:
            if not track.is_actionable:
                continue
            for zone in self._zones.values():
                if not zone.enabled or not zone.accepts_class(track.label):
                    continue
                from .geometry import point_in_polygon

                if point_in_polygon(track.box.foot_point, zone.points):
                    in_zone[zone.zone_id] = in_zone.get(zone.zone_id, 0) + 1

        age = self.stats.frame_age_seconds
        return {
            "type": "session.state",
            "session_id": self.session_id,
            "camera_id": self.camera_id,
            "camera_name": self.camera_name,
            "seq": frame.seq if frame else None,
            "server_time": self._clock(),
            "source": self.source_health.to_dict(),
            "ai": {
                "state": self.model_health,
                "model": self._model_info.name,
                "sha256": self._model_info.sha256,
                "provider": self._model_info.provider,
                "reason_code": self.model_reason,
                "detail": self.model_detail,
                "latency_ms": round(self.stats.last_inference_ms, 2)
                if self.stats.last_inference_ms
                else None,
            },
            "stats": {
                "active_tracks": len([t for t in tracks if t.is_actionable]),
                "in_zone": in_zone,
                "processed_fps": self.source_health.fps,
                "frame_age_ms": round(age * 1000.0) if age is not None else None,
                "frames_processed": self.stats.frames_processed,
                "frames_dropped": self.stats.frames_dropped,
                "inference_errors": self.stats.inference_errors,
                "events_emitted": self.stats.events_emitted,
                "events_suppressed": self.stats.events_suppressed,
            },
            "tracks": [
                {
                    "id": track.track_id,
                    "label": track.label,
                    "confidence": track.confidence,
                    "box": track.box.as_list(),
                    "age": track.age_frames,
                    "clip": track.clip,
                }
                for track in tracks
            ],
            "zones": [
                {
                    "id": zone.zone_id,
                    "name": zone.name,
                    "kind": zone.kind,
                    "polygon": [[round(x, 6), round(y, 6)] for x, y in zone.points],
                    "color": getattr(zone, "color", "#f97316"),
                    "severity": str(zone.severity_value()),
                    "enabled": zone.enabled,
                    "class_filter": sorted(zone.class_filter),
                    "cooldown_seconds": zone.cooldown_seconds,
                    "min_dwell_seconds": zone.min_dwell_seconds,
                    "occupancy": in_zone.get(zone.zone_id, 0),
                }
                for zone in self._zones.values()
            ],
            "events": [
                {
                    "id": event.id,
                    "type": event.type,
                    "severity": str(event.severity_value()),
                    "occurred_at": event.occurred_at,
                    "zone_id": event.zone_id,
                    "zone_name": event.zone_name,
                    "track_id": event.track_id,
                    "metadata": event.metadata,
                }
                for event in events
            ],
        }

    def close(self) -> None:
        self._stopping = True
        try:
            self._sink.close()
        except Exception:
            logger.exception("error closing event sink for session %s", self.session_id)

    def record_source_failure(self, reason_code: str, detail: str) -> None:
        """Called by the session runner when a source open/read fails fatally."""
        self.source_health.state = SourceState.FAILED
        self.source_health.reason_code = reason_code
        self.source_health.detail = detail


def _touches_edge(box: Any) -> bool:
    return any(
        abs(value) < EDGE_EPSILON or abs(value - 1.0) < EDGE_EPSILON
        for value in (box.x1, box.y1, box.x2, box.y2)
    )


__all__ = [
    "MAX_CONSECUTIVE_INFERENCE_ERRORS",
    "STALLED_FRAME_SECONDS",
    "PipelineConfig",
    "PipelineStats",
    "VisionPipeline",
]
