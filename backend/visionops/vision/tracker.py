"""Self-contained multi-object tracker: Kalman prediction + two-stage IoU association.

Stage 1 matches high-confidence detections to active tracks. Stage 2 creates
tracks from still-unmatched high-confidence detections. Stage 3 re-associates
low-confidence detections against recently lost tracks using their *predicted*
boxes, which is what preserves an ID through a short occlusion.

This is deliberately dependency-free (ADR-008): a better tracker (real
ByteTrack, BoT-SORT, DeepSORT) is a drop-in replacement because the interface is
a single method.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from .contracts import BBox, Detection, Track, TrackState
from .geometry import box_iou
from .kalman import KalmanBoxFilter

#: Detections at or above this score are considered "high confidence" and may
#: start a new track; the remainder may only re-associate existing tracks.
HIGH_SCORE = 0.5
#: Second-stage (re-association) gate. Lower than the first because we compare
#: against a *predicted* box, which is less certain.
LOW_SCORE_IOU_GATE = 0.2


@dataclass(slots=True)
class _Track:
    track_id: int
    label: str
    kf: KalmanBoxFilter
    state: TrackState
    first_seen_seq: int
    last_seen_seq: int
    age_frames: int
    hits: int
    misses: int
    confidence: float
    clip: bool
    confirmed: bool
    #: True when this track was observed (created or matched) in the previous
    #: frame. Drives stage-1 eligibility without conflating "active" with the
    #: reported state, which also reflects confirmation.
    matched_prev: bool = True
    #: Frames since this track was last matched. Retirement is governed by
    #: *staleness*, not total age: a person followed for ten minutes must not be
    #: dropped just because ``max_age`` frames have passed.
    since_matched: int = 0

    @property
    def last_box(self) -> BBox:
        return self.kf.smoothed_box


@dataclass(slots=True)
class TrackerConfig:
    """Tunables. Defaults documented in ``docs/VISION_ENGINE.md`` §3."""

    match_iou: float = 0.3
    max_age: int = 30
    min_hits: int = 3
    high_score: float = HIGH_SCORE


@dataclass(slots=True)
class KalmanIouTracker:
    """Kalman + IoU tracker with ByteTrack-style two-stage association."""

    config: TrackerConfig = field(default_factory=TrackerConfig)
    _tracks: dict[int, _Track] = field(default_factory=dict, init=False)
    _next_id: int = field(default=1, init=False)
    _frame_seq: int = field(default=-1, init=False)

    # -- public interface ---------------------------------------------------

    def reset(self) -> None:
        """Forget every track and restart the id space. Call between sessions."""
        self._tracks.clear()
        self._next_id = 1
        self._frame_seq = -1

    @property
    def active_count(self) -> int:
        return sum(1 for t in self._tracks.values() if t.state is TrackState.ACTIVE)

    @property
    def track_count(self) -> int:
        return len(self._tracks)

    def update(
        self,
        detections: Sequence[Detection],
        frame_seq: int,
        timestamp: float,
    ) -> list[Track]:
        """Advance the tracker by one frame and return the reported tracks."""
        if frame_seq <= self._frame_seq:
            # Non-monotonic input (a replayed sequence) — keep ids stable anyway.
            frame_seq = self._frame_seq + 1
        self._frame_seq = frame_seq

        # 1. Predict every existing track one step forward. Which tracks were
        #    *observed last frame* is captured before anything changes, so
        #    stage-1 association can still match them.
        previously_active: set[int] = set()
        for track_id, track in self._tracks.items():
            track.kf.predict()
            track.age_frames += 1
            track.since_matched += 1
            if track.matched_prev:
                previously_active.add(track_id)
            track.matched_prev = False

        # 2. ByteTrack-style association.
        #    Stage 1 — high-confidence detections against tracks observed last frame.
        #    Stage 2 — low-confidence detections against the same tracks, which
        #               keeps an ID alive while the detector momentarily drops it.
        #    Stage 3 — still-unmatched detections against recently lost tracks,
        #               compared against each track's *predicted* box.
        #    Stage 4 — remaining high-confidence detections start new tracks.
        high = [d for d in detections if d.confidence >= self.config.high_score]
        low = [d for d in detections if d.confidence < self.config.high_score]
        high = self._associate(high, self.config.match_iou, eligible=previously_active)
        low = self._associate(low, self.config.match_iou, eligible=previously_active)

        # Anything observed last frame that did not match this frame is now LOST.
        # It stays available for stage 3 until ``max_age`` expires.
        for track_id in previously_active:
            lost: _Track | None = self._tracks.get(track_id)
            if lost is not None and not lost.matched_prev:
                lost.state = TrackState.LOST
                lost.misses += 1

        # Stage 3 — recover identities across a short occlusion. This runs before
        # new tracks are created, otherwise every re-appearance would look like a
        # new object and produce duplicate zone events.
        if high:
            high = self._associate(high, LOW_SCORE_IOU_GATE, eligible=self._lost_ids())
        if low:
            self._associate(low, LOW_SCORE_IOU_GATE, eligible=self._lost_ids())

        # Stage 4 — the remainder start new tracks.
        for detection in high:
            self._create_track(detection, frame_seq)

        # 5. Retire tracks that have been unmatched for too long. This is based
        #    on staleness, never on total age — see ``since_matched``.
        for track_id in [
            tid
            for tid, track in self._tracks.items()
            if track.since_matched > self.config.max_age and not track.matched_prev
        ]:
            del self._tracks[track_id]

        return self._report(frame_seq)

    # -- internals ----------------------------------------------------------

    def _associate(
        self,
        detections: Sequence[Detection],
        iou_gate: float,
        *,
        eligible: set[int] | None = None,
    ) -> list[Detection]:
        """Greedy IoU matching. Returns the detections that found no match.

        ``eligible`` restricts which track ids may be matched (stage 1 uses the
        tracks that were active before this frame; stage 3 leaves it ``None`` so
        lost tracks are eligible).
        """
        if not detections or not self._tracks:
            return list(detections)

        pairs: list[tuple[float, int, int]] = []
        for det_idx, detection in enumerate(detections):
            for track_id, track in self._tracks.items():
                if eligible is not None and track_id not in eligible:
                    continue
                iou = box_iou(track.last_box, detection.box)
                if iou >= iou_gate:
                    pairs.append((iou, det_idx, track_id))

        # Highest IoU first; ties broken deterministically by id.
        pairs.sort(key=lambda p: (-p[0], p[1], p[2]))

        used_detections: set[int] = set()
        used_tracks: set[int] = set()
        for _iou, det_idx, track_id in pairs:
            if det_idx in used_detections or track_id in used_tracks:
                continue
            used_detections.add(det_idx)
            used_tracks.add(track_id)
            self._apply(self._tracks[track_id], detections[det_idx])

        return [d for i, d in enumerate(detections) if i not in used_detections]

    def _lost_ids(self) -> set[int]:
        """Ids of tracks that are LOST but still within ``max_age`` of staleness."""
        return {
            track_id
            for track_id, track in self._tracks.items()
            if track.state is TrackState.LOST and track.since_matched <= self.config.max_age
        }

    def _create_track(self, detection: Detection, frame_seq: int) -> _Track:
        track = _Track(
            track_id=self._next_id,
            label=detection.label,
            kf=KalmanBoxFilter(detection.box),
            state=TrackState.ACTIVE,
            first_seen_seq=frame_seq,
            last_seen_seq=frame_seq,
            age_frames=0,
            hits=1,
            misses=0,
            confidence=detection.confidence,
            clip=detection.clip,
            # A track needs ``min_hits`` observations before it is reported, so a
            # single-frame false positive cannot create a zone event. With
            # ``min_hits == 1`` the operator has explicitly asked for no such
            # gating, so the track is confirmed immediately.
            confirmed=self.config.min_hits <= 1,
        )
        self._next_id += 1
        self._tracks[track.track_id] = track
        return track

    def _apply(self, track: _Track, detection: Detection) -> None:
        track.kf.update(detection.box)
        track.state = TrackState.ACTIVE
        track.matched_prev = True
        track.since_matched = 0
        track.last_seen_seq = detection.frame_seq or self._frame_seq
        track.hits += 1
        track.misses = 0
        track.confidence = detection.confidence
        track.label = detection.label
        track.clip = detection.clip
        if track.hits >= self.config.min_hits:
            track.confirmed = True

    def _report(self, frame_seq: int) -> list[Track]:
        reported: list[Track] = []
        for track in sorted(self._tracks.values(), key=lambda t: t.track_id):
            if not track.confirmed:
                continue
            reported.append(
                Track(
                    track_id=track.track_id,
                    label=track.label,
                    confidence=round(track.confidence, 4),
                    box=track.last_box,
                    velocity=(
                        round(track.kf.velocity[0], 6),
                        round(track.kf.velocity[1], 6),
                    ),
                    state=track.state,
                    first_seen_seq=track.first_seen_seq,
                    last_seen_seq=track.last_seen_seq,
                    age_frames=track.age_frames,
                    hits=track.hits,
                    clip=track.clip,
                    frame_seq=frame_seq,
                )
            )
        return reported


__all__ = ["HIGH_SCORE", "KalmanIouTracker", "TrackerConfig"]
