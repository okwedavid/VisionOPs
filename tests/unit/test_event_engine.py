"""Event engine: the anti-storm gates and the structured payload contract.

These tests define what a customer actually receives, so every assertion is on
the emitted event rather than on internal state.
"""

from __future__ import annotations

import pytest

from visionops.vision.contracts import (
    BBox,
    EvalContext,
    EventRecord,
    Severity,
    TransitionKind,
    ZoneSpec,
)
from visionops.vision.event_engine import (
    DEDUP_BUCKET_SECONDS,
    EVENT_REGISTRY,
    EventEngine,
    is_known_event_type,
    validate_event_metadata,
)
from visionops.vision.geometry import rectangle_polygon

ZONE = ZoneSpec(
    zone_id="z1",
    name="restricted",
    points=rectangle_polygon(0.4, 0.2, 0.7, 0.95),
    severity=Severity.WARNING,
    cooldown_seconds=0.0,
    min_dwell_seconds=0.0,
)


def transition(
    kind: TransitionKind = TransitionKind.ENTERED,
    *,
    track_id: int = 1,
    timestamp: float = 0.0,
    zone_id: str = "z1",
    confidence: float = 0.9,
    label: str = "person",
):
    from visionops.vision.contracts import ZoneTransition

    box = BBox(0.45, 0.30, 0.55, 0.80)
    return ZoneTransition(
        kind=kind,
        zone_id=zone_id,
        zone_name="restricted",
        track_id=track_id,
        label=label,
        confidence=confidence,
        box=box,
        foot_point=box.foot_point,
        dwell_seconds=None if kind is TransitionKind.ENTERED else 12.5,
        severity=Severity.WARNING,
        frame_seq=int(timestamp),
        timestamp=timestamp,
    )


def context(timestamp: float = 0.0) -> EvalContext:
    return EvalContext(
        timestamp=timestamp,
        frame_seq=int(timestamp),
        frame_width=1280,
        frame_height=720,
        model_name="yolo11n",
        model_sha256="a" * 64,
        source_kind="file",
    )


_DEFAULT_ZONES = object()


def handle(engine: EventEngine, transitions, *, zones=_DEFAULT_ZONES, timestamp: float = 0.0):
    """Drive the engine. ``zones={}`` is meaningful, hence the sentinel default."""
    resolved = {"z1": ZONE} if zones is _DEFAULT_ZONES else zones
    return engine.handle_transitions(
        list(transitions),
        context(timestamp),
        camera_id="cam-1",
        camera_name="front-door",
        session_id="sess-1",
        zones=resolved,
    )


class TestRegistry:
    def test_all_shipped_types_are_registered(self) -> None:
        expected = {
            "ZONE_ENTERED",
            "ZONE_EXITED",
            "SOURCE_STARTED",
            "SOURCE_STOPPED",
            "SOURCE_FAILED",
            "MODEL_LOADED",
            "MODEL_FAILED",
        }
        assert set(EVENT_REGISTRY) == expected

    def test_every_type_has_a_description_and_severity(self) -> None:
        for name, spec in EVENT_REGISTRY.items():
            assert spec.description, f"{name} has no description"
            assert isinstance(spec.severity, Severity)
            assert spec.since_version

    def test_is_known_event_type(self) -> None:
        assert is_known_event_type("ZONE_ENTERED")
        assert not is_known_event_type("SOMETHING_MADE_UP")

    def test_unknown_types_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown event type"):
            validate_event_metadata("NOT_A_TYPE", {})


class TestTransitionsToEvents:
    def test_entry_becomes_a_zone_entered_event(self) -> None:
        engine = EventEngine()
        events = handle(engine, [transition()])
        assert [e.type for e in events] == ["ZONE_ENTERED"]
        assert events[0].severity is Severity.WARNING
        assert events[0].zone_id == "z1"
        assert events[0].zone_name == "restricted"
        assert events[0].track_id == 1
        assert events[0].camera_id == "cam-1"
        assert events[0].camera_name == "front-door"
        assert events[0].session_id == "sess-1"
        assert events[0].schema_version == 1

    def test_exit_carries_the_dwell_time(self) -> None:
        engine = EventEngine()
        events = handle(engine, [transition(TransitionKind.EXITED)])
        assert [e.type for e in events] == ["ZONE_EXITED"]
        assert events[0].metadata["dwell_seconds"] == pytest.approx(12.5)

    def test_entry_has_no_dwell_time(self) -> None:
        engine = EventEngine()
        events = handle(engine, [transition()])
        assert "dwell_seconds" not in events[0].metadata

    def test_metadata_is_fully_typed_and_normalised(self) -> None:
        engine = EventEngine()
        metadata = handle(engine, [transition()])[0].metadata
        assert metadata["box"] == [0.45, 0.30, 0.55, 0.80]
        assert metadata["foot_point"] == [0.5, 0.8]
        assert metadata["class"] == "person"
        assert metadata["confidence"] == pytest.approx(0.9)
        assert metadata["frame_width"] == 1280
        assert metadata["frame_height"] == 720
        assert metadata["model"] == "yolo11n"
        assert metadata["model_sha256"] == "a" * 64
        assert metadata["source_kind"] == "file"
        assert metadata["zone_name"] == "restricted"
        for value in metadata["box"]:
            assert 0.0 <= value <= 1.0

    def test_confidence_is_rounded_for_stable_json(self) -> None:
        engine = EventEngine()
        events = handle(engine, [transition(confidence=0.87654321)])
        assert events[0].confidence == pytest.approx(0.8765, abs=1e-4)
        assert events[0].metadata["confidence"] == pytest.approx(0.8765, abs=1e-4)

    def test_no_transitions_means_no_events(self) -> None:
        assert handle(EventEngine(), []) == []

    def test_a_zone_no_longer_present_is_ignored(self) -> None:
        """An operator deleting a zone mid-session must not produce a stale event."""
        engine = EventEngine()
        assert handle(engine, [transition()], zones={}) == []

    def test_a_disabled_zone_is_ignored(self) -> None:
        engine = EventEngine()
        disabled = ZoneSpec(zone_id="z1", name="restricted", points=ZONE.points, enabled=False)
        assert handle(engine, [transition()], zones={"z1": disabled}) == []

    def test_the_class_filter_is_applied_at_the_event_stage_too(self) -> None:
        engine = EventEngine()
        forklift_only = ZoneSpec(
            zone_id="z1", name="yard", points=ZONE.points, class_filter=frozenset({"forklift"})
        )
        assert handle(engine, [transition()], zones={"z1": forklift_only}) == []


class TestCooldown:
    def test_a_second_event_of_the_same_type_inside_the_cooldown_is_suppressed(self) -> None:
        engine = EventEngine()
        zone = ZoneSpec(zone_id="z1", name="restricted", points=ZONE.points, cooldown_seconds=10.0)
        zones = {"z1": zone}
        assert len(handle(engine, [transition(timestamp=0.0)], zones=zones)) == 1
        assert handle(engine, [transition(timestamp=1.0)], zones=zones) == []
        assert handle(engine, [transition(timestamp=9.9)], zones=zones) == []

    def test_the_cooldown_expires(self) -> None:
        engine = EventEngine()
        zone = ZoneSpec(zone_id="z1", name="restricted", points=ZONE.points, cooldown_seconds=10.0)
        zones = {"z1": zone}
        handle(engine, [transition(timestamp=0.0)], zones=zones)
        assert len(handle(engine, [transition(timestamp=10.5)], zones=zones)) == 1

    def test_cooldown_is_per_type_so_an_entry_never_silences_an_exit(self) -> None:
        engine = EventEngine()
        zone = ZoneSpec(zone_id="z1", name="restricted", points=ZONE.points, cooldown_seconds=60.0)
        zones = {"z1": zone}
        assert handle(engine, [transition(timestamp=0.0)], zones=zones)
        exits = handle(engine, [transition(TransitionKind.EXITED, timestamp=1.0)], zones=zones)
        assert [e.type for e in exits] == ["ZONE_EXITED"]

    def test_cooldown_is_per_zone(self) -> None:
        engine = EventEngine()
        zones = {
            "z1": ZoneSpec(
                zone_id="z1",
                name="a",
                points=rectangle_polygon(0.0, 0.0, 0.4, 1.0),
                cooldown_seconds=60.0,
            ),
            "z2": ZoneSpec(
                zone_id="z2",
                name="b",
                points=rectangle_polygon(0.6, 0.0, 1.0, 1.0),
                cooldown_seconds=60.0,
            ),
        }
        assert handle(engine, [transition(timestamp=0.0)], zones=zones)
        assert handle(engine, [transition(zone_id="z2", timestamp=0.5)], zones=zones)


class TestDedup:
    def test_the_same_event_inside_one_bucket_is_deduplicated(self) -> None:
        engine = EventEngine()
        zone = ZoneSpec(zone_id="z1", name="restricted", points=ZONE.points, cooldown_seconds=0.0)
        zones = {"z1": zone}
        assert len(handle(engine, [transition(timestamp=1.0)], zones=zones)) == 1
        assert handle(engine, [transition(timestamp=1.5)], zones=zones) == []

    def test_dedup_expires_with_the_bucket(self) -> None:
        engine = EventEngine()
        zone = ZoneSpec(zone_id="z1", name="restricted", points=ZONE.points, cooldown_seconds=0.0)
        zones = {"z1": zone}
        handle(engine, [transition(timestamp=1.0)], zones=zones)
        later = DEDUP_BUCKET_SECONDS * 3
        assert len(handle(engine, [transition(timestamp=later)], zones=zones)) == 1

    def test_different_tracks_are_not_deduplicated_against_each_other(self) -> None:
        engine = EventEngine()
        zone = ZoneSpec(zone_id="z1", name="restricted", points=ZONE.points, cooldown_seconds=0.0)
        zones = {"z1": zone}
        events = handle(
            engine,
            [transition(track_id=1, timestamp=1.0), transition(track_id=2, timestamp=1.0)],
            zones=zones,
        )
        assert {e.track_id for e in events} == {1, 2}

    def test_dedup_memory_is_bounded(self) -> None:
        engine = EventEngine()
        zone = ZoneSpec(zone_id="z1", name="restricted", points=ZONE.points, cooldown_seconds=0.0)
        zones = {"z1": zone}
        for index in range(200):
            handle(engine, [transition(track_id=index, timestamp=float(index))], zones=zones)
        assert len(engine._seen_keys) <= 4096


class TestMultipleTransitions:
    def test_a_batch_produces_one_event_each(self) -> None:
        engine = EventEngine()
        zone = ZoneSpec(zone_id="z1", name="restricted", points=ZONE.points, cooldown_seconds=0.0)
        events = handle(
            engine,
            [transition(track_id=1, timestamp=1.0), transition(track_id=2, timestamp=1.0)],
            zones={"z1": zone},
        )
        assert sorted(e.track_id for e in events) == [1, 2]

    def test_mixed_enter_and_exit_in_one_batch(self) -> None:
        engine = EventEngine()
        zone = ZoneSpec(zone_id="z1", name="restricted", points=ZONE.points, cooldown_seconds=0.0)
        events = handle(
            engine,
            [
                transition(track_id=1, timestamp=1.0),
                transition(TransitionKind.EXITED, track_id=2, timestamp=1.0),
            ],
            zones={"z1": zone},
        )
        assert sorted(e.type for e in events) == ["ZONE_ENTERED", "ZONE_EXITED"]


class TestSeverityResolution:
    def test_the_zone_severity_overrides_the_type_default_for_entries(self) -> None:
        engine = EventEngine()
        critical = ZoneSpec(
            zone_id="z1", name="restricted", points=ZONE.points, severity=Severity.CRITICAL
        )
        events = handle(engine, [transition()], zones={"z1": critical})
        assert events[0].severity is Severity.CRITICAL

    def test_exits_use_the_type_default(self) -> None:
        engine = EventEngine()
        critical = ZoneSpec(
            zone_id="z1", name="restricted", points=ZONE.points, severity=Severity.CRITICAL
        )
        events = handle(engine, [transition(TransitionKind.EXITED)], zones={"z1": critical})
        assert events[0].severity is Severity.INFO


class TestLifecycle:
    def test_reset_clears_the_gates(self) -> None:
        engine = EventEngine()
        zone = ZoneSpec(zone_id="z1", name="restricted", points=ZONE.points, cooldown_seconds=60.0)
        zones = {"z1": zone}
        handle(engine, [transition(timestamp=0.0)], zones=zones)
        assert handle(engine, [transition(timestamp=1.0)], zones=zones) == []
        engine.reset()
        assert handle(engine, [transition(timestamp=1.0)], zones=zones)


class TestEventRecord:
    def test_severity_value_accepts_both_string_and_enum(self) -> None:
        assert (
            EventRecord(
                type="X", severity="warning", occurred_at=0.0, camera_id="c"
            ).severity_value()
            is Severity.WARNING
        )
        assert (
            EventRecord(
                type="X", severity=Severity.INFO, occurred_at=0.0, camera_id="c"
            ).severity_value()
            is Severity.INFO
        )

    def test_metadata_defaults_to_an_empty_dict(self) -> None:
        assert (
            EventRecord(type="X", severity=Severity.INFO, occurred_at=0.0, camera_id="c").metadata
            == {}
        )


class TestMakeZoneEvent:
    def test_builds_a_fully_populated_record(self) -> None:
        engine = EventEngine()
        record = engine.make_zone_event(
            "ZONE_EXITED",
            occurred_at=100.0,
            camera_id="c",
            camera_name="cam",
            session_id="s",
            ctx=context(100.0),
            box=BBox(0.1, 0.2, 0.3, 0.9),
            foot_point=(0.2, 0.9),
            track_id=4,
            label="person",
            confidence=0.77,
            zone_id="z1",
            zone_name="restricted",
            dwell_seconds=3.5,
        )
        assert record.type == "ZONE_EXITED"
        assert record.metadata["dwell_seconds"] == 3.5
        assert record.metadata["box"] == [0.1, 0.2, 0.3, 0.9]
        assert record.metadata["foot_point"] == [0.2, 0.9]

    def test_source_events_carry_their_type_severity(self) -> None:
        engine = EventEngine()
        record = engine.source_event(
            "SOURCE_FAILED",
            camera_id="c",
            camera_name="cam",
            session_id="s",
            metadata={"reason_code": "CAMERA_BUSY"},
        )
        assert record.severity is Severity.ERROR
        assert record.metadata["reason_code"] == "CAMERA_BUSY"
