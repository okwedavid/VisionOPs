"""Zone strategy: entry, exit, dwell hysteresis, class and clip filters.

These assertions describe exactly what an operator sees on the dashboard, so
they are written as transitions, not as internal state.
"""

from __future__ import annotations

import pytest

from visionops.vision.contracts import (
    EvalContext,
    Severity,
    Track,
    TrackState,
    TransitionKind,
    ZoneSpec,
)
from visionops.vision.geometry import rectangle_polygon
from visionops.vision.registry import build_zone_strategy
from visionops.vision.zones import LineZoneStrategy, PolygonZoneStrategy


def track(
    track_id: int = 1,
    x: float = 0.5,
    *,
    y1: float = 0.3,
    y2: float = 0.8,
    width: float = 0.1,
    label: str = "person",
    state: TrackState = TrackState.ACTIVE,
    clip: bool = False,
    confidence: float = 0.9,
) -> Track:
    from visionops.vision.contracts import BBox

    return Track(
        track_id=track_id,
        label=label,
        confidence=confidence,
        box=BBox.from_xyxy(x, y1, x + width, y2),
        velocity=(0.0, 0.0),
        state=state,
        first_seen_seq=0,
        last_seen_seq=0,
        age_frames=10,
        hits=10,
        clip=clip,
    )


def zone(**overrides) -> ZoneSpec:
    defaults = {
        "zone_id": "z1",
        "name": "restricted",
        "points": rectangle_polygon(0.4, 0.2, 0.7, 0.95),
        "min_dwell_seconds": 0.0,
        "cooldown_seconds": 0.0,
    }
    defaults.update(overrides)
    return ZoneSpec(**defaults)  # type: ignore[arg-type]


def context(timestamp: float) -> EvalContext:
    return EvalContext(
        timestamp=timestamp, frame_seq=int(timestamp), frame_width=640, frame_height=360
    )


class TestPolygonEntryExit:
    def test_entry_then_exit(self) -> None:
        strategy = PolygonZoneStrategy()
        state: dict[tuple[str, int], str] = {}
        # Foot point at x = 0.5 + 0.05 = 0.55 → inside.
        assert [
            t.kind for t in strategy.evaluate([track(x=0.45)], zone(), context(0.0), state)
        ] == [TransitionKind.ENTERED]
        assert strategy.evaluate([track(x=0.45)], zone(), context(1.0), state) == []
        assert [t.kind for t in strategy.evaluate([track(x=0.9)], zone(), context(2.0), state)] == [
            TransitionKind.EXITED
        ]

    def test_foot_point_decides_not_the_box_centre(self) -> None:
        """A person leaning over the boundary has their centre inside but feet out."""
        strategy = PolygonZoneStrategy()
        state: dict[tuple[str, int], str] = {}
        # Box spans y 0.30..0.80, so the foot point is at y = 0.80.
        # The zone only covers y 0.2..0.75, so the feet are outside it.
        low_zone = zone(points=rectangle_polygon(0.0, 0.2, 1.0, 0.75))
        assert strategy.evaluate([track(x=0.45)], low_zone, context(0.0), state) == []

    def test_fully_inside_emits_nothing_further(self) -> None:
        strategy = PolygonZoneStrategy()
        state: dict[tuple[str, int], str] = {}
        strategy.evaluate([track(x=0.45)], zone(), context(0.0), state)
        for step in range(1, 10):
            assert strategy.evaluate([track(x=0.45)], zone(), context(float(step)), state) == []

    def test_a_person_standing_outside_emits_nothing(self) -> None:
        strategy = PolygonZoneStrategy()
        state: dict[tuple[str, int], str] = {}
        for step in range(5):
            assert strategy.evaluate([track(x=0.05)], zone(), context(float(step)), state) == []

    def test_a_polygon_can_be_any_shape_not_only_a_rectangle(self) -> None:
        strategy = PolygonZoneStrategy()
        # Distinct zone ids: membership is keyed by (zone, track).
        rect_zone = zone(zone_id="rect", points=rectangle_polygon(0.2, 0.2, 0.8, 0.8))
        diamond_zone = zone(
            zone_id="diamond", points=((0.5, 0.2), (0.8, 0.5), (0.5, 0.8), (0.2, 0.5))
        )
        rect_state: dict[tuple[str, int], str] = {}
        diamond_state: dict[tuple[str, int], str] = {}
        # Foot point (0.50, 0.65) is inside both shapes.
        inside_both = track(x=0.45, y1=0.35, y2=0.65, width=0.1)
        assert strategy.evaluate([inside_both], rect_zone, context(0.0), rect_state)
        assert strategy.evaluate([inside_both], diamond_zone, context(0.0), diamond_state)
        # A foot point in the square's corner is inside the square but outside
        # the diamond — proof the two shapes are really treated differently.
        in_square_only = track(x=0.20, y1=0.05, y2=0.25, width=0.1)  # foot (0.25, 0.25)
        square_state: dict[tuple[str, int], str] = {}
        notch_state: dict[tuple[str, int], str] = {}
        assert strategy.evaluate([in_square_only], rect_zone, context(0.0), square_state)
        assert strategy.evaluate([in_square_only], diamond_zone, context(0.0), notch_state) == []

    def test_severity_follows_the_zone_and_inverts_on_exit(self) -> None:
        strategy = PolygonZoneStrategy()
        state: dict[tuple[str, int], str] = {}
        entered = strategy.evaluate(
            [track(x=0.45)], zone(severity=Severity.CRITICAL), context(0.0), state
        )[0]
        assert entered.severity is Severity.CRITICAL
        left = strategy.evaluate(
            [track(x=0.9)], zone(severity=Severity.CRITICAL), context(1.0), state
        )[0]
        assert left.severity is Severity.INFO

    def test_metadata_carries_the_foot_point_and_dwell(self) -> None:
        strategy = PolygonZoneStrategy()
        state: dict[tuple[str, int], str] = {}
        strategy.evaluate([track(x=0.45)], zone(), context(10.0), state)
        left = strategy.evaluate([track(x=0.9)], zone(), context(17.5), state)[0]
        assert left.dwell_seconds == pytest.approx(7.5)
        assert left.foot_point[0] == pytest.approx(0.95)
        assert left.kind is TransitionKind.EXITED


class TestDwellHysteresis:
    def test_a_brief_crossing_emits_nothing(self) -> None:
        """Someone brushing the boundary must not create an event."""
        strategy = PolygonZoneStrategy()
        state: dict[tuple[str, int], str] = {}
        strict = zone(min_dwell_seconds=1.0)
        assert strategy.evaluate([track(x=0.45)], strict, context(0.0), state) == []
        assert strategy.evaluate([track(x=0.9)], strict, context(0.5), state) == []
        # And the track is back to "outside" with no history of a phantom entry.
        assert state[("z1", 1)] == "outside"

    def test_dwell_must_elapse_before_the_entry_is_confirmed(self) -> None:
        strategy = PolygonZoneStrategy()
        state: dict[tuple[str, int], str] = {}
        strict = zone(min_dwell_seconds=1.0)
        assert strategy.evaluate([track(x=0.45)], strict, context(0.0), state) == []
        assert state[("z1", 1)] == "inside_unconfirmed"
        assert [
            t.kind for t in strategy.evaluate([track(x=0.45)], strict, context(0.6), state)
        ] == []
        assert [
            t.kind for t in strategy.evaluate([track(x=0.45)], strict, context(1.2), state)
        ] == [TransitionKind.ENTERED]

    def test_returning_inside_before_dwell_resets_the_timer(self) -> None:
        strategy = PolygonZoneStrategy()
        state: dict[tuple[str, int], str] = {}
        strict = zone(min_dwell_seconds=2.0)
        strategy.evaluate([track(x=0.45)], strict, context(0.0), state)
        strategy.evaluate([track(x=0.9)], strict, context(1.0), state)  # stepped out
        strategy.evaluate([track(x=0.45)], strict, context(1.5), state)  # back in, timer restarts
        assert strategy.evaluate([track(x=0.45)], strict, context(2.0), state) == []
        assert strategy.evaluate([track(x=0.45)], strict, context(4.0), state)

    def test_zero_dwell_confirms_immediately(self) -> None:
        strategy = PolygonZoneStrategy()
        state: dict[tuple[str, int], str] = {}
        assert strategy.evaluate([track(x=0.45)], zone(min_dwell_seconds=0.0), context(0.0), state)


class TestGates:
    def test_a_disabled_zone_emits_nothing(self) -> None:
        strategy = PolygonZoneStrategy()
        state: dict[tuple[str, int], str] = {}
        assert strategy.evaluate([track(x=0.45)], zone(enabled=False), context(0.0), state) == []

    def test_a_zone_with_too_few_points_emits_nothing(self) -> None:
        strategy = PolygonZoneStrategy()
        state: dict[tuple[str, int], str] = {}
        assert (
            strategy.evaluate(
                [track(x=0.45)], zone(points=((0.1, 0.1), (0.9, 0.9))), context(0.0), state
            )
            == []
        )

    def test_class_filter_excludes_other_classes(self) -> None:
        strategy = PolygonZoneStrategy()
        state: dict[tuple[str, int], str] = {}
        people_only = zone(class_filter=frozenset({"person"}))
        assert (
            strategy.evaluate([track(x=0.45, label="forklift")], people_only, context(0.0), state)
            == []
        )
        assert strategy.evaluate([track(x=0.45, label="person")], people_only, context(1.0), state)

    def test_empty_class_filter_accepts_everything(self) -> None:
        strategy = PolygonZoneStrategy()
        state: dict[tuple[str, int], str] = {}
        assert strategy.evaluate([track(x=0.45, label="forklift")], zone(), context(0.0), state)

    def test_a_lost_track_cannot_produce_an_exit(self) -> None:
        """A phantom exit is worse than a missing one: occupancy would be wrong."""
        strategy = PolygonZoneStrategy()
        state: dict[tuple[str, int], str] = {}
        strategy.evaluate([track(x=0.45)], zone(), context(0.0), state)
        assert (
            strategy.evaluate([track(x=0.9, state=TrackState.LOST)], zone(), context(1.0), state)
            == []
        )

    def test_a_clipped_track_cannot_produce_an_entry(self) -> None:
        """A person half outside the frame has an unreliable foot point."""
        strategy = PolygonZoneStrategy()
        state: dict[tuple[str, int], str] = {}
        assert strategy.evaluate([track(x=0.45, clip=True)], zone(), context(0.0), state) == []

    def test_independent_tracks_have_independent_membership(self) -> None:
        strategy = PolygonZoneStrategy()
        state: dict[tuple[str, int], str] = {}
        assert strategy.evaluate([track(1, x=0.45), track(2, x=0.05)], zone(), context(0.0), state)
        assert state[("z1", 1)] == "inside"
        assert state[("z1", 2)] == "outside"

    def test_state_is_pruned_when_a_track_disappears(self) -> None:
        """Memory must stay bounded by live tracks × zones.

        One frame with 50 people is 50 entries; the next frame with one person is
        one. Without pruning this dict would grow for the whole session.
        """
        strategy = PolygonZoneStrategy()
        state: dict[tuple[str, int], str] = {}
        crowd = [track(track_id, x=0.45) for track_id in range(1, 51)]
        strategy.evaluate(crowd, zone(), context(0.0), state)
        assert len(state) == 50
        assert len(strategy._memberships) == 50
        strategy.evaluate([track(1, x=0.45)], zone(), context(1.0), state)
        assert len(state) == 1
        assert len(strategy._memberships) == 1

    def test_two_zones_of_the_same_strategy_are_evaluated_separately(self) -> None:
        strategy = PolygonZoneStrategy()
        left = zone(zone_id="left", points=rectangle_polygon(0.0, 0.2, 0.4, 0.95))
        right = zone(zone_id="right", points=rectangle_polygon(0.6, 0.2, 1.0, 0.95))
        state_left: dict[tuple[str, int], str] = {}
        state_right: dict[tuple[str, int], str] = {}
        person = track(x=0.2)
        assert strategy.evaluate([person], left, context(0.0), state_left)
        assert strategy.evaluate([person], right, context(0.0), state_right) == []


class TestLineStrategy:
    """Line zones are registered and unit-tested; the UI editor lands in Phase 4."""

    def test_emits_when_the_foot_point_crosses_the_line(self) -> None:
        strategy = LineZoneStrategy()
        state: dict[tuple[str, int], str] = {}
        line = zone(kind="line", points=((0.0, 0.5), (1.0, 0.5)))
        # Below the line (foot y 0.9), then above it (foot y 0.45).
        strategy.evaluate([track(x=0.2, y1=0.3, y2=0.9)], line, context(0.0), state)
        crossed = strategy.evaluate([track(x=0.2, y1=0.1, y2=0.45)], line, context(1.0), state)
        assert [t.kind for t in crossed] == [TransitionKind.ENTERED]
        # And crossing back the other way is the inverse transition.
        crossed_back = strategy.evaluate([track(x=0.2, y1=0.3, y2=0.9)], line, context(2.0), state)
        assert [t.kind for t in crossed_back] == [TransitionKind.EXITED]

    def test_no_crossing_emits_nothing(self) -> None:
        strategy = LineZoneStrategy()
        state: dict[tuple[str, int], str] = {}
        line = zone(kind="line", points=((0.0, 0.5), (1.0, 0.5)))
        for step in range(4):
            assert (
                strategy.evaluate([track(x=0.2, y1=0.3, y2=0.9)], line, context(float(step)), state)
                == []
            )

    def test_direction_filter_accepts_the_permitted_way(self) -> None:
        """The line normal is (-dy, dx) = (0, 1), so (0, 1) permits upward travel."""
        strategy = LineZoneStrategy()
        state: dict[tuple[str, int], str] = {}
        line = zone(kind="line", points=((0.0, 0.5), (1.0, 0.5)), direction=(0.0, 1.0))
        strategy.evaluate([track(x=0.2, y1=0.3, y2=0.9)], line, context(0.0), state)
        crossed = strategy.evaluate([track(x=0.2, y1=0.1, y2=0.45)], line, context(1.0), state)
        assert [t.kind for t in crossed] == [TransitionKind.ENTERED]

    def test_direction_filter_rejects_the_wrong_way(self) -> None:
        strategy = LineZoneStrategy()
        state: dict[tuple[str, int], str] = {}
        line = zone(kind="line", points=((0.0, 0.5), (1.0, 0.5)), direction=(0.0, -1.0))
        strategy.evaluate([track(x=0.2, y1=0.3, y2=0.9)], line, context(0.0), state)
        assert strategy.evaluate([track(x=0.2, y1=0.1, y2=0.45)], line, context(1.0), state) == []

    def test_requires_exactly_two_points(self) -> None:
        strategy = LineZoneStrategy()
        state: dict[tuple[str, int], str] = {}
        assert (
            strategy.evaluate(
                [track(x=0.2)], zone(kind="line", points=((0.1, 0.1),)), context(0.0), state
            )
            == []
        )
        assert (
            strategy.evaluate(
                [track(x=0.2)],
                zone(kind="line", points=((0.1, 0.1), (0.9, 0.9), (0.5, 0.2))),
                context(0.0),
                state,
            )
            == []
        )

    def test_a_disabled_line_emits_nothing(self) -> None:
        strategy = LineZoneStrategy()
        state: dict[tuple[str, int], str] = {}
        line = zone(kind="line", points=((0.0, 0.5), (1.0, 0.5)), enabled=False)
        assert strategy.evaluate([track(x=0.2)], line, context(0.0), state) == []


class TestRegistry:
    def test_polygon_and_line_are_registered(self) -> None:
        assert isinstance(build_zone_strategy("polygon"), PolygonZoneStrategy)
        assert isinstance(build_zone_strategy("line"), LineZoneStrategy)

    def test_unknown_kind_is_a_clear_error(self) -> None:
        with pytest.raises(KeyError, match="unknown zone kind"):
            build_zone_strategy("triangle")

    def test_strategy_kinds_match_the_registry_keys(self) -> None:
        assert PolygonZoneStrategy().kind == "polygon"
        assert LineZoneStrategy().kind == "line"
