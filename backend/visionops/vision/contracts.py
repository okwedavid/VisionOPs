"""The vision engine's entire vocabulary: value types and extension interfaces.

Nothing in :mod:`visionops.vision` may import a web framework, an ORM, or the
network. That constraint is what makes the pipeline deterministic, unit-testable
and embeddable in an edge agent (``docs/REPOSITORY_STRUCTURE.md`` §2.1).

All geometry crossing this boundary is **normalised** to ``[0, 1]`` with the
origin at the top-left of the source frame (ADR-019).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

import numpy as np

# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class SourceState(StrEnum):
    """Lifecycle of a frame source."""

    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    DEGRADED = "degraded"
    FAILED = "failed"


class TrackState(StrEnum):
    """Lifecycle of a single track."""

    ACTIVE = "active"
    LOST = "lost"
    REMOVED = "removed"


class TransitionKind(StrEnum):
    """A confirmed zone boundary crossing."""

    ENTERED = "entered"
    EXITED = "exited"


class Severity(StrEnum):
    """Event severity. Ordered least → most urgent."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"
    ERROR = "error"


class SourceKind(StrEnum):
    """Supported frame sources."""

    BROWSER = "browser"
    DEVICE = "device"
    FILE = "file"
    RTSP = "rtsp"


# ---------------------------------------------------------------------------
# Geometry value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BBox:
    """Axis-aligned bounding box in normalised coordinates ``(x1, y1, x2, y2)``.

    ``x1 <= x2`` and ``y1 <= y2`` always hold; values are clamped to ``[0, 1]``.
    """

    x1: float
    y1: float
    x2: float
    y2: float

    def __post_init__(self) -> None:
        if self.x2 < self.x1 or self.y2 < self.y1:
            raise ValueError(f"malformed bbox: ({self.x1}, {self.y1}, {self.x2}, {self.y2})")

    @classmethod
    def from_xyxy(cls, x1: float, y1: float, x2: float, y2: float) -> BBox:
        """Build a box from possibly unordered coordinates, clamped to the frame."""
        lo_x, hi_x = (x1, x2) if x1 <= x2 else (x2, x1)
        lo_y, hi_y = (y1, y2) if y1 <= y2 else (y2, y1)
        clamp = lambda v: min(1.0, max(0.0, v))  # noqa: E731
        return cls(clamp(lo_x), clamp(lo_y), clamp(hi_x), clamp(hi_y))

    @classmethod
    def from_cxcywh(cls, cx: float, cy: float, w: float, h: float) -> BBox:
        """Build a box from centre/width/height, clamped to the frame."""
        return cls.from_xyxy(cx - w / 2.0, cy - h / 2.0, cx + w / 2.0, cy + h / 2.0)

    @property
    def width(self) -> float:
        return self.x2 - self.x1

    @property
    def height(self) -> float:
        return self.y2 - self.y1

    @property
    def area(self) -> float:
        return max(0.0, self.width) * max(0.0, self.height)

    @property
    def center(self) -> tuple[float, float]:
        return ((self.x1 + self.x2) / 2.0, (self.y1 + self.y2) / 2.0)

    @property
    def foot_point(self) -> tuple[float, float]:
        """Ground-contact estimate: bottom-centre of the box.

        Zone membership is decided on this point, not the box centre — a
        person's box spans head to feet and only the bottom edge touches the
        floor (``docs/VISION_ENGINE.md`` §6.1).
        """
        return ((self.x1 + self.x2) / 2.0, self.y2)

    @property
    def is_degenerate(self) -> bool:
        return self.width <= 1e-6 or self.height <= 1e-6

    def as_list(self) -> list[float]:
        return [round(self.x1, 6), round(self.y1, 6), round(self.x2, 6), round(self.y2, 6)]


# ---------------------------------------------------------------------------
# Frame and detection
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Frame:
    """One decoded frame plus its provenance."""

    image: np.ndarray  # BGR uint8, HxWx3, at the source's native resolution
    seq: int
    timestamp: float  # unix seconds
    width: int
    height: int
    source_timestamp: float | None = None  # client-reported capture time (browser source)

    def __post_init__(self) -> None:
        if self.image.ndim != 3 or self.image.shape[2] != 3:
            raise ValueError(f"frame image must be HxWx3 BGR, got shape {self.image.shape}")
        if self.image.shape[0] != self.height or self.image.shape[1] != self.width:
            raise ValueError(
                f"frame shape {self.image.shape[:2]} does not match declared "
                f"size ({self.width}x{self.height})"
            )


@dataclass(frozen=True, slots=True)
class Detection:
    """A single model detection, in normalised frame coordinates."""

    class_id: int
    label: str
    confidence: float
    box: BBox
    frame_seq: int = 0
    #: True when the detection touches (or was clipped by) a frame edge, which
    #: makes its foot point unreliable — such tracks cannot cause zone events.
    clip: bool = False


@dataclass(frozen=True, slots=True)
class Track:
    """A tracked object. ``track_id`` is ephemeral and session-scoped."""

    track_id: int
    label: str
    confidence: float
    box: BBox
    velocity: tuple[float, float]
    state: TrackState
    first_seen_seq: int
    last_seen_seq: int
    age_frames: int
    hits: int
    clip: bool
    frame_seq: int = 0

    @property
    def is_actionable(self) -> bool:
        """Whether this track may produce a zone transition."""
        return self.state is TrackState.ACTIVE and not self.clip


# ---------------------------------------------------------------------------
# Zones and transitions
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ZoneSpec:
    """A configured zone. A rectangle is simply a four-point polygon."""

    zone_id: str
    name: str
    kind: str = "polygon"
    points: tuple[tuple[float, float], ...] = ()
    severity: Severity | str = Severity.WARNING
    class_filter: frozenset[str] = frozenset()
    cooldown_seconds: float = 10.0
    min_dwell_seconds: float = 0.5
    enabled: bool = True
    direction: tuple[float, float] | None = None  # reserved for kind="line"

    def severity_value(self) -> Severity:
        return Severity(self.severity) if not isinstance(self.severity, Severity) else self.severity

    def accepts_class(self, label: str) -> bool:
        return not self.class_filter or label in self.class_filter


@dataclass(frozen=True, slots=True)
class ZoneTransition:
    """A confirmed boundary crossing, before event-level gating."""

    kind: TransitionKind
    zone_id: str
    zone_name: str
    track_id: int
    label: str
    confidence: float
    box: BBox
    foot_point: tuple[float, float]
    dwell_seconds: float | None
    severity: Severity
    frame_seq: int = 0
    timestamp: float = 0.0


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EventRecord:
    """A durable, structured event. The only product VisionOps persists."""

    type: str
    severity: Severity | str
    occurred_at: float  # unix seconds
    camera_id: str
    camera_name: str = ""
    zone_id: str | None = None
    zone_name: str | None = None
    track_id: int | None = None
    session_id: str | None = None
    confidence: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: int = 1
    id: str | None = None  # assigned by the sink if not pre-generated
    source_kind: str = ""
    frame_width: int | None = None
    frame_height: int | None = None
    snapshot_path: str | None = None  # relative to the snapshot root; opt-in only

    def severity_value(self) -> Severity:
        return Severity(self.severity) if not isinstance(self.severity, Severity) else self.severity


# ---------------------------------------------------------------------------
# Health and model metadata
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SourceHealth:
    """Machine- and human-readable health of a frame source."""

    state: SourceState = SourceState.STOPPED
    reason_code: str | None = None
    detail: str | None = None
    kind: SourceKind | str = SourceKind.FILE
    fps: float = 0.0
    frames: int = 0
    dropped: int = 0
    retries: int = 0
    uptime_seconds: float = 0.0
    position: float | None = None
    duration: float | None = None
    last_frame_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": str(self.state),
            "reason_code": self.reason_code,
            "detail": self.detail,
            "kind": str(self.kind),
            "fps": round(self.fps, 2),
            "frames": self.frames,
            "dropped": self.dropped,
            "retries": self.retries,
            "uptime_seconds": round(self.uptime_seconds, 1),
            "position": self.position,
            "duration": self.duration,
        }


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """Provenance of the loaded detection model."""

    name: str
    path: str
    sha256: str
    provider: str
    input_width: int
    input_height: int
    classes: tuple[str, ...]
    version: str = "unknown"
    warmup_ms: float | None = None
    fallback_provider: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "sha256": self.sha256,
            "provider": self.provider,
            "fallback_provider": self.fallback_provider,
            "input_width": self.input_width,
            "input_height": self.input_height,
            "classes_count": len(self.classes),
            "version": self.version,
            "warmup_ms": round(self.warmup_ms, 1) if self.warmup_ms is not None else None,
        }


class ModelState(StrEnum):
    """Lifecycle of the detection model."""

    UNCONFIGURED = "unconfigured"
    DOWNLOADING = "downloading"
    LOADING = "loading"
    READY = "ready"
    DEGRADED = "degraded"
    FAILED = "failed"
    DISABLED = "disabled"


# ---------------------------------------------------------------------------
# Extension interfaces (ADR-015)
# ---------------------------------------------------------------------------


@runtime_checkable
class VisionModel(Protocol):
    """Anything that turns a frame into detections.

    Implementations must be safe to share across sessions (or document that
    they are not); the shipped ONNX adapter is.
    """

    @property
    def info(self) -> ModelInfo: ...

    def warmup(self, iterations: int = 1) -> float:
        """Run throwaway inferences; return the measured warmup duration in ms."""
        ...

    def infer(self, frame: Frame) -> list[Detection]: ...

    def close(self) -> None: ...


@runtime_checkable
class Tracker(Protocol):
    """Assigns stable, ephemeral IDs to detections across frames."""

    def update(
        self, detections: Sequence[Detection], frame_seq: int, timestamp: float
    ) -> list[Track]: ...

    def reset(self) -> None: ...

    @property
    def active_count(self) -> int: ...


@dataclass(frozen=True, slots=True)
class EvalContext:
    """Everything a zone strategy may consult besides geometry."""

    timestamp: float
    frame_seq: int
    frame_width: int | None = None
    frame_height: int | None = None
    model_name: str = ""
    model_sha256: str = ""
    source_kind: str = ""


@runtime_checkable
class ZoneStrategy(Protocol):
    """Turns tracks + a zone spec into confirmed transitions."""

    kind: str

    def evaluate(
        self,
        tracks: Sequence[Track],
        zone: ZoneSpec,
        ctx: EvalContext,
        state: dict[tuple[str, int], str],
    ) -> list[ZoneTransition]:
        """Evaluate transitions and mutate ``state`` (keyed by ``(zone_id, track_id)``).

        ``state`` holds the last confirmed/pending membership per track so the
        strategy can implement hysteresis across frames.
        """
        ...


@runtime_checkable
class EventSink(Protocol):
    """Where events and live state go. The pipeline never knows the concrete type."""

    def emit(self, events: Sequence[EventRecord]) -> None: ...

    def publish_state(self, state: dict[str, Any]) -> None: ...

    def close(self) -> None: ...


__all__ = [
    "BBox",
    "Detection",
    "EvalContext",
    "EventRecord",
    "EventSink",
    "Frame",
    "ModelInfo",
    "ModelState",
    "Severity",
    "SourceHealth",
    "SourceKind",
    "SourceState",
    "Track",
    "TrackState",
    "Tracker",
    "TransitionKind",
    "VisionModel",
    "ZoneSpec",
    "ZoneStrategy",
    "ZoneTransition",
]
