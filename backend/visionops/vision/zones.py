"""Zone strategies: turns tracks into confirmed boundary transitions.

Membership is decided on each track's **foot point** (bottom-centre of the
box) and gated by dwell hysteresis, so a person standing on a boundary does not
flap an alert storm (``docs/VISION_ENGINE.md`` §6).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from .contracts import (
    EvalContext,
    Severity,
    Track,
    TransitionKind,
    ZoneSpec,
    ZoneTransition,
)
from .geometry import point_in_polygon, signed_distance_to_line

#: Per-(zone, track) membership state keys.
_OUTSIDE = "outside"
_INSIDE_UNCONFIRMED = "inside_unconfirmed"
_INSIDE = "inside"
_OUTSIDE_UNCONFIRMED = "outside_unconfirmed"
_DWELL_NONE = -1.0


class _Dwell(StrEnum):
    NONE = "none"


@dataclass(slots=True)
class _Membership:
    """Hysteresis state for one ``(zone, track)`` pair."""

    state: str = _OUTSIDE
    since: float = 0.0
    confirmed_at: float | None = None


@dataclass(slots=True)
class PolygonZoneStrategy:
    """Polygon containment with dwell hysteresis.

    ``state`` is a mutable dict supplied (and owned) by the pipeline, keyed by
    ``(zone_id, track_id)``. Entries for tracks that disappear are pruned by the
    pipeline, so memory is bounded by live tracks × zones.
    """

    kind: str = "polygon"
    _memberships: dict[tuple[str, int], _Membership] = field(default_factory=dict, init=False)

    def evaluate(
        self,
        tracks: Sequence[Track],
        zone: ZoneSpec,
        ctx: EvalContext,
        state: dict[tuple[str, int], str],
    ) -> list[ZoneTransition]:
        """Return confirmed transitions and update ``state`` in place.

        ``state`` maps ``(zone_id, track_id)`` to a compact encoded membership.
        The encoding keeps the pipeline's state a plain dict of strings, which
        is what the tests and the debug state view rely on.
        """
        if not zone.enabled or len(zone.points) < 3:
            return []

        polygon = zone.points
        transitions: list[ZoneTransition] = []
        seen: set[tuple[str, int]] = set()
        min_dwell = max(0.0, zone.min_dwell_seconds)

        for track in tracks:
            if not track.is_actionable or not zone.accepts_class(track.label):
                continue
            key = (zone.zone_id, track.track_id)
            seen.add(key)
            entry = self._memberships.get(key) or _Membership()
            foot = track.box.foot_point
            inside = point_in_polygon(foot, polygon)

            if inside and entry.state == _OUTSIDE:
                entry.state = _INSIDE_UNCONFIRMED
                entry.since = ctx.timestamp
            elif not inside and entry.state == _INSIDE:
                entry.state = _OUTSIDE_UNCONFIRMED
                entry.since = ctx.timestamp
            elif not inside and entry.state in (_OUTSIDE_UNCONFIRMED, _INSIDE_UNCONFIRMED):
                # Left before dwell was satisfied — no event at all (hysteresis).
                entry.state = _OUTSIDE
            elif inside and entry.state == _OUTSIDE_UNCONFIRMED:
                entry.state = _INSIDE_UNCONFIRMED
                entry.since = ctx.timestamp

            if entry.state == _INSIDE_UNCONFIRMED and (ctx.timestamp - entry.since) >= min_dwell:
                entry.state = _INSIDE
                entry.confirmed_at = ctx.timestamp
                transitions.append(
                    self._transition(TransitionKind.ENTERED, track, zone, ctx, dwell=None)
                )
            elif entry.state == _OUTSIDE_UNCONFIRMED and (ctx.timestamp - entry.since) >= min_dwell:
                entry.state = _OUTSIDE
                dwell = ctx.timestamp - (entry.confirmed_at or entry.since)
                entry.confirmed_at = None
                transitions.append(
                    self._transition(TransitionKind.EXITED, track, zone, ctx, dwell=max(0.0, dwell))
                )

            state[key] = entry.state
            self._memberships[key] = entry

        self._prune(seen, state)
        return transitions

    def _prune(
        self, seen: set[tuple[str, int]], state: dict[tuple[str, int], str] | None = None
    ) -> None:
        """Forget tracks that are no longer present.

        The pipeline-owned ``state`` dict is pruned as well as our internal
        membership table: without that it would grow for the lifetime of the
        session, one entry per track that ever appeared.
        """
        for key in [k for k in self._memberships if k not in seen]:
            del self._memberships[key]
        if state is not None:
            for key in [k for k in state if k not in seen]:
                del state[key]

    @staticmethod
    def _transition(
        kind: TransitionKind,
        track: Track,
        zone: ZoneSpec,
        ctx: EvalContext,
        *,
        dwell: float | None,
    ) -> ZoneTransition:
        return ZoneTransition(
            kind=kind,
            zone_id=zone.zone_id,
            zone_name=zone.name,
            track_id=track.track_id,
            label=track.label,
            confidence=track.confidence,
            box=track.box,
            foot_point=track.box.foot_point,
            dwell_seconds=dwell,
            severity=zone.severity_value() if kind is TransitionKind.ENTERED else Severity.INFO,
            frame_seq=ctx.frame_seq,
            timestamp=ctx.timestamp,
        )


@dataclass(slots=True)
class LineZoneStrategy:
    """Direction-aware line crossing.

    The sign of the signed distance to the line flips when a track crosses it.
    ``ZoneSpec.direction`` (when set) filters the accepted direction. Registered
    but not surfaced in the V0.1 UI (see ``docs/ROADMAP.md`` Phase 4).
    """

    kind: str = "line"
    _memberships: dict[tuple[str, int], _Membership] = field(default_factory=dict, init=False)

    def evaluate(
        self,
        tracks: Sequence[Track],
        zone: ZoneSpec,
        ctx: EvalContext,
        state: dict[tuple[str, int], str],
    ) -> list[ZoneTransition]:
        if not zone.enabled or len(zone.points) != 2:
            return []
        start, end = zone.points
        transitions: list[ZoneTransition] = []
        seen: set[tuple[str, int]] = set()
        min_dwell = max(0.0, zone.min_dwell_seconds)

        for track in tracks:
            if not track.is_actionable or not zone.accepts_class(track.label):
                continue
            key = (zone.zone_id, track.track_id)
            seen.add(key)
            entry = self._memberships.get(key) or _Membership()
            side = signed_distance_to_line(track.box.foot_point, start, end)
            positive = side > 0
            entry_value = state.get(key)
            previous = entry_value == _INSIDE

            if entry_value is None:
                entry.state = _INSIDE if positive else _OUTSIDE
                entry.since = ctx.timestamp
                entry.confirmed_at = ctx.timestamp if positive else None
            elif positive != previous:
                if (ctx.timestamp - entry.since) >= min_dwell:
                    crossed_in = positive
                    if not self._direction_ok(zone, start, end, crossed_in):
                        entry.state = _INSIDE if positive else _OUTSIDE
                        entry.since = ctx.timestamp
                        state[key] = entry.state
                        self._memberships[key] = entry
                        continue
                    dwell = ctx.timestamp - (entry.confirmed_at or entry.since)
                    transitions.append(
                        LineZoneStrategy._transition(
                            TransitionKind.ENTERED if crossed_in else TransitionKind.EXITED,
                            track,
                            zone,
                            ctx,
                            dwell=max(0.0, dwell),
                        )
                    )
                    entry.state = _INSIDE if positive else _OUTSIDE
                    entry.since = ctx.timestamp
                    entry.confirmed_at = ctx.timestamp if positive else None
                else:
                    entry.state = _INSIDE_UNCONFIRMED if positive else _OUTSIDE_UNCONFIRMED
            else:
                if entry.state in (_INSIDE_UNCONFIRMED, _OUTSIDE_UNCONFIRMED):
                    entry.state = _INSIDE if positive else _OUTSIDE
                    entry.since = ctx.timestamp

            state[key] = entry.state
            self._memberships[key] = entry

        self._prune(seen, state)
        return transitions

    @staticmethod
    def _direction_ok(
        zone: ZoneSpec,
        start: tuple[float, float],
        end: tuple[float, float],
        crossed_in: bool,
    ) -> bool:
        """Whether a crossing direction is allowed by ``zone.direction``.

        ``direction`` names the permitted direction of travel, so it is compared
        against the line's **normal** (not its tangent): a crossing from the
        negative side to the positive side moves along ``+normal``.
        """
        if zone.direction is None:
            return True
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        length = (dx * dx + dy * dy) ** 0.5
        if length < 1e-12:
            return True
        normal_x, normal_y = -dy / length, dx / length
        dot = zone.direction[0] * normal_x + zone.direction[1] * normal_y
        if abs(dot) < 1e-9:
            # A direction perpendicular to the normal cannot filter a crossing.
            return True
        return (dot > 0) == crossed_in

    def _prune(
        self, seen: set[tuple[str, int]], state: dict[tuple[str, int], str] | None = None
    ) -> None:
        for key in [k for k in self._memberships if k not in seen]:
            del self._memberships[key]
        if state is not None:
            for key in [k for k in state if k not in seen]:
                del state[key]

    _transition = staticmethod(PolygonZoneStrategy._transition)


__all__ = ["LineZoneStrategy", "PolygonZoneStrategy"]
