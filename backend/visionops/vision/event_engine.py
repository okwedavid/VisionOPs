"""Event engine: gating, typed metadata validation, event construction.

Every event that leaves VisionOps passes through here, so every event carries a
known type, a validated metadata shape and a stable severity
(``docs/EVENT_ENGINE.md``).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .contracts import (
    BBox,
    EvalContext,
    EventRecord,
    Severity,
    SourceKind,
    ZoneTransition,
)

#: Bucket width (seconds) for the dedup key. Events for the same zone/type/track
#: inside one bucket collapse into a single event.
DEDUP_BUCKET_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class EventTypeSpec:
    """The contract for one event type."""

    name: str
    severity: Severity
    description: str
    since_version: str = "0.1.0"
    #: Metadata keys that must be present and numeric (0..1 confidence-like).
    required_confidence_keys: tuple[str, ...] = ()


def _zone_metadata(
    transition: ZoneTransition,
    ctx: EvalContext,
    *,
    camera_name: str,
) -> dict[str, Any]:
    box: BBox = transition.box
    metadata: dict[str, Any] = {
        "camera_name": camera_name,
        "zone_name": transition.zone_name,
        "box": box.as_list(),
        "foot_point": [round(transition.foot_point[0], 6), round(transition.foot_point[1], 6)],
        "class": transition.label,
        "confidence": round(transition.confidence, 4),
        "track_age_frames": transition.frame_seq,
        "frame_width": ctx.frame_width,
        "frame_height": ctx.frame_height,
        "model": ctx.model_name,
        "model_sha256": ctx.model_sha256,
        "source_kind": ctx.source_kind,
    }
    if transition.dwell_seconds is not None:
        metadata["dwell_seconds"] = round(transition.dwell_seconds, 3)
    return metadata


#: The event type catalogue. Status tags match ``docs/EVENT_ENGINE.md`` §3.
EVENT_REGISTRY: dict[str, EventTypeSpec] = {
    spec.name: spec
    for spec in (
        EventTypeSpec(
            name="ZONE_ENTERED",
            severity=Severity.WARNING,
            description="A tracked object entered a configured zone.",
        ),
        EventTypeSpec(
            name="ZONE_EXITED",
            severity=Severity.INFO,
            description="A tracked object left a configured zone.",
        ),
        EventTypeSpec(
            name="SOURCE_STARTED",
            severity=Severity.INFO,
            description="A processing session began ingesting frames.",
        ),
        EventTypeSpec(
            name="SOURCE_STOPPED",
            severity=Severity.INFO,
            description="A processing session stopped cleanly.",
        ),
        EventTypeSpec(
            name="SOURCE_FAILED",
            severity=Severity.ERROR,
            description="A frame source gave up after exhausting retries.",
        ),
        EventTypeSpec(
            name="MODEL_LOADED",
            severity=Severity.INFO,
            description="The detection model loaded and warmed up successfully.",
        ),
        EventTypeSpec(
            name="MODEL_FAILED",
            severity=Severity.ERROR,
            description="The detection model could not be loaded or repeatedly failed.",
        ),
    )
}


def is_known_event_type(name: str) -> bool:
    return name in EVENT_REGISTRY


def validate_event_metadata(name: str, metadata: dict[str, Any]) -> None:
    """Reject metadata that does not match the type's contract.

    Guards the write boundary so no future producer (webhook receiver, agent
    tool, replay command) can smuggle an unvalidatable payload into the store.
    """
    spec = EVENT_REGISTRY.get(name)
    if spec is None:
        raise ValueError(f"unknown event type: {name!r}")
    for key in spec.required_confidence_keys:
        value = metadata.get(key)
        if value is not None and not isinstance(value, (int, float)):
            raise TypeError(f"event {name}: metadata[{key!r}] must be numeric, got {type(value)!r}")


@dataclass(slots=True)
class EventEngine:
    """Applies the anti-storm gates and builds :class:`EventRecord` values.

    Three independent gates, each solving a different failure mode
    (``docs/EVENT_ENGINE.md`` §4):

    * dwell hysteresis — inside the zone strategy, per ``(zone, track)``
    * cooldown — here, per ``(zone, type)``; a boundary flapping storm
    * dedup key — here, per ``(zone, type, track)`` within a time bucket
    """

    id_factory: Any = None  # callable() -> str; injected for deterministic tests
    _last_emit: dict[tuple[str, str], float] = field(default_factory=dict, init=False)
    _seen_keys: set[tuple[str, str, int, int]] = field(default_factory=set, init=False)

    def reset(self) -> None:
        self._last_emit.clear()
        self._seen_keys.clear()

    # -- transitions --------------------------------------------------------

    def handle_transitions(
        self,
        transitions: Sequence[ZoneTransition],
        ctx: EvalContext,
        *,
        camera_id: str,
        camera_name: str,
        session_id: str,
        zones: dict[str, Any],
    ) -> list[EventRecord]:
        """Convert confirmed transitions into gated event records."""
        events: list[EventRecord] = []
        for transition in transitions:
            zone = zones.get(transition.zone_id)
            if zone is None or not zone.enabled:
                continue
            if not zone.accepts_class(transition.label):
                continue

            event_type = "ZONE_ENTERED" if transition.kind == "entered" else "ZONE_EXITED"
            if not is_known_event_type(event_type):
                continue

            # The cooldown comes from the zone configuration, per (zone, type):
            # an entry must never silence an exit.
            cooldown = max(0.0, float(getattr(zone, "cooldown_seconds", 0.0)))
            if not self._cooldown_ok(
                transition.zone_id, event_type, transition.timestamp, cooldown
            ):
                continue
            if not self._dedup_ok(transition, event_type, transition.timestamp):
                continue

            spec = EVENT_REGISTRY[event_type]
            metadata = _zone_metadata(transition, ctx, camera_name=camera_name)
            validate_event_metadata(event_type, metadata)

            events.append(
                EventRecord(
                    type=event_type,
                    severity=(
                        zone.severity_value() if event_type == "ZONE_ENTERED" else spec.severity
                    ),
                    occurred_at=transition.timestamp,
                    camera_id=camera_id,
                    camera_name=camera_name,
                    zone_id=transition.zone_id,
                    zone_name=transition.zone_name,
                    track_id=transition.track_id,
                    session_id=session_id,
                    confidence=transition.confidence,
                    metadata=metadata,
                )
            )
        return events

    def _cooldown_ok(self, zone_id: str, event_type: str, now: float, cooldown: float) -> bool:
        key = (zone_id, event_type)
        last = self._last_emit.get(key)
        if cooldown > 0.0 and last is not None and (now - last) < cooldown:
            return False
        self._last_emit[key] = now
        return True

    def _dedup_ok(self, transition: ZoneTransition, event_type: str, now: float) -> bool:
        bucket = int(now // DEDUP_BUCKET_SECONDS)
        key = (transition.zone_id, event_type, transition.track_id, bucket)
        if key in self._seen_keys:
            return False
        self._seen_keys.add(key)
        # Bound memory: one bucket of history is all that can ever match.
        if len(self._seen_keys) > 4096:
            self._seen_keys = {k for k in self._seen_keys if k[3] >= bucket - 1}
        return True

    # -- system events ------------------------------------------------------

    def source_event(
        self,
        event_type: str,
        *,
        camera_id: str,
        camera_name: str,
        session_id: str,
        metadata: dict[str, Any] | None = None,
        timestamp: float | None = None,
        source_kind: SourceKind | str = "",
    ) -> EventRecord:
        spec = EVENT_REGISTRY[event_type]
        payload = dict(metadata or {})
        validate_event_metadata(event_type, payload)
        return EventRecord(
            type=event_type,
            severity=spec.severity,
            occurred_at=timestamp if timestamp is not None else math.floor(0) or 0.0,
            camera_id=camera_id,
            camera_name=camera_name,
            session_id=session_id,
            metadata=payload,
            source_kind=str(source_kind),
        )

    def make_zone_event(
        self,
        event_type: str,
        *,
        occurred_at: float,
        camera_id: str,
        camera_name: str,
        session_id: str,
        ctx: EvalContext,
        box: BBox,
        foot_point: tuple[float, float],
        track_id: int,
        label: str,
        confidence: float,
        zone_id: str,
        zone_name: str,
        dwell_seconds: float | None = None,
    ) -> EventRecord:
        """Build a zone event directly (used by tests and the replay path)."""
        metadata: dict[str, Any] = {
            "camera_name": camera_name,
            "zone_name": zone_name,
            "box": box.as_list(),
            "foot_point": [round(foot_point[0], 6), round(foot_point[1], 6)],
            "class": label,
            "confidence": round(confidence, 4),
            "track_age_frames": ctx.frame_seq,
            "frame_width": ctx.frame_width,
            "frame_height": ctx.frame_height,
            "model": ctx.model_name,
            "model_sha256": ctx.model_sha256,
            "source_kind": ctx.source_kind,
        }
        if dwell_seconds is not None:
            metadata["dwell_seconds"] = round(dwell_seconds, 3)
        validate_event_metadata(event_type, metadata)
        spec = EVENT_REGISTRY[event_type]
        return EventRecord(
            type=event_type,
            severity=spec.severity,
            occurred_at=occurred_at,
            camera_id=camera_id,
            camera_name=camera_name,
            zone_id=zone_id,
            zone_name=zone_name,
            track_id=track_id,
            session_id=session_id,
            confidence=confidence,
            metadata=metadata,
        )


__all__ = [
    "DEDUP_BUCKET_SECONDS",
    "EVENT_REGISTRY",
    "EventEngine",
    "EventTypeSpec",
    "is_known_event_type",
    "validate_event_metadata",
]
