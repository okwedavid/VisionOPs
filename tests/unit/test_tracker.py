"""Tracker: stable IDs, occlusion recovery, and no ID explosion.

The tracker is what turns "detections" into "a person who entered at 10:15 and
left at 10:16", so these assertions are exact rather than statistical.
"""

from __future__ import annotations

import pytest

from visionops.vision.contracts import BBox, Detection, TrackState
from visionops.vision.tracker import HIGH_SCORE, KalmanIouTracker, TrackerConfig


def detect(
    x: float,
    *,
    y: float = 0.3,
    width: float = 0.1,
    height: float = 0.5,
    confidence: float = 0.9,
    label: str = "person",
    frame_seq: int = 0,
) -> Detection:
    return Detection(
        class_id=0,
        label=label,
        confidence=confidence,
        box=BBox.from_xyxy(x, y, x + width, y + height),
        frame_seq=frame_seq,
    )


def walk(start: float, end: float, frames: int, *, width: float = 0.1) -> list[Detection]:
    return [
        detect(start + (end - start) * i / max(1, frames - 1), width=width, frame_seq=i)
        for i in range(frames)
    ]


def run(tracker: KalmanIouTracker, frames: list[Detection]) -> list[list[int]]:
    """Feed frames, returning the reported track ids per frame."""
    reported: list[list[int]] = []
    for index, detection in enumerate(frames):
        tracks = tracker.update([detection], index, float(index))
        reported.append([track.track_id for track in tracks])
    return reported


class TestSingleObject:
    def test_keeps_one_id_for_a_straight_walk(self) -> None:
        tracker = KalmanIouTracker(TrackerConfig(min_hits=1))
        ids = {ids[0] for ids in run(tracker, walk(0.05, 0.85, 30)) if ids}
        assert ids == {1}

    def test_starts_reporting_after_min_hits(self) -> None:
        tracker = KalmanIouTracker(TrackerConfig(min_hits=3))
        reported = run(tracker, walk(0.2, 0.4, 8))
        assert reported[0] == [] and reported[1] == []
        assert reported[2] == [1]

    def test_a_single_frame_detection_creates_no_actionable_track(self) -> None:
        """min_hits must stop a one-frame false positive from causing an event."""
        tracker = KalmanIouTracker(TrackerConfig(min_hits=3))
        assert tracker.update([detect(0.3)], 0, 0.0) == []
        assert tracker.update([], 1, 1.0) == []

    def test_velocity_and_age_are_reported(self) -> None:
        tracker = KalmanIouTracker(TrackerConfig(min_hits=1))
        tracker.update([detect(0.10)], 0, 0.0)
        for index in range(1, 20):
            tracks = tracker.update([detect(0.10 + 0.02 * index)], index, float(index))
        track = tracks[0]
        assert track.velocity[0] > 0.01
        assert track.age_frames >= 15
        assert track.hits >= 15

    def test_first_and_last_seen_sequences(self) -> None:
        tracker = KalmanIouTracker(TrackerConfig(min_hits=1))
        for index in range(5):
            tracks = tracker.update([detect(0.3, frame_seq=index)], index, float(index))
        assert tracks[0].first_seen_seq == 0
        assert tracks[0].last_seen_seq == 4


class TestOcclusion:
    def test_survives_a_three_frame_detector_dropout(self) -> None:
        """The detector losing an object for a few frames must not lose the ID.

        This is the property that depends on the Kalman filter having learned the
        true velocity: without it the predicted box misses and a duplicate
        ``ZONE_ENTERED`` is produced (``docs/VISION_ENGINE.md`` §5.1).
        """
        tracker = KalmanIouTracker(TrackerConfig(min_hits=1, max_age=30))
        step = 0.8 / 19
        ids: set[int] = set()
        states: list[TrackState] = []
        for index in range(20):
            detections: list[Detection] = (
                [] if 10 <= index <= 12 else [detect(0.05 + step * index, frame_seq=index)]
            )
            for track in tracker.update(detections, index, float(index)):
                if index >= 9:
                    ids.add(track.track_id)
                if index == 11:
                    states.append(track.state)
        assert states == [TrackState.LOST], "the track must be reported lost during the gap"
        assert ids == {1}, "the ID must survive the occlusion"
        assert tracker.active_count == 1

    def test_reports_a_track_as_lost_while_missing_then_active_again(self) -> None:
        tracker = KalmanIouTracker(TrackerConfig(min_hits=1))
        tracker.update([detect(0.3)], 0, 0.0)
        lost = tracker.update([], 1, 1.0)
        assert lost and lost[0].state is TrackState.LOST
        assert not lost[0].is_actionable
        back = tracker.update([detect(0.31)], 2, 2.0)
        assert back and back[0].state is TrackState.ACTIVE

    def test_lost_tracks_are_retired_after_max_age(self) -> None:
        tracker = KalmanIouTracker(TrackerConfig(min_hits=1, max_age=5))
        tracker.update([detect(0.3)], 0, 0.0)
        for index in range(1, 12):
            tracker.update([], index, float(index))
        assert tracker.track_count == 0

    def test_a_new_object_after_a_long_gap_gets_a_new_id(self) -> None:
        tracker = KalmanIouTracker(TrackerConfig(min_hits=1, max_age=3))
        tracker.update([detect(0.1)], 0, 0.0)
        for index in range(1, 10):
            tracker.update([], index, float(index))
        tracks = tracker.update([detect(0.1)], 10, 10.0)
        assert tracks[0].track_id != 1


class TestTwoStageAssociation:
    def test_a_low_score_detection_keeps_an_existing_track_alive(self) -> None:
        tracker = KalmanIouTracker(TrackerConfig(min_hits=1, high_score=HIGH_SCORE))
        tracker.update([detect(0.30, confidence=0.95)], 0, 0.0)
        # The detector momentarily loses confidence but the object is still there.
        tracks = tracker.update([detect(0.32, confidence=0.4)], 1, 1.0)
        assert [t.track_id for t in tracks] == [1]
        assert tracks[0].state is TrackState.ACTIVE

    def test_a_low_score_detection_alone_never_starts_a_track(self) -> None:
        tracker = KalmanIouTracker(TrackerConfig(min_hits=1, high_score=HIGH_SCORE))
        assert tracker.update([detect(0.30, confidence=0.4)], 0, 0.0) == []

    def test_uses_a_raised_high_score_threshold(self) -> None:
        tracker = KalmanIouTracker(TrackerConfig(min_hits=1, high_score=0.8))
        assert tracker.update([detect(0.3, confidence=0.6)], 0, 0.0) == []
        assert len(tracker.update([detect(0.3, confidence=0.9)], 1, 1.0)) == 1


class TestMultipleObjects:
    def test_two_people_keep_separate_ids(self) -> None:
        tracker = KalmanIouTracker(TrackerConfig(min_hits=1))
        for index in range(20):
            step = index * 0.02
            tracks = tracker.update(
                [detect(0.10 + step, frame_seq=index), detect(0.60 - step, frame_seq=index)],
                index,
                float(index),
            )
            if index > 3:
                assert len(tracks) == 2
                assert {t.track_id for t in tracks} == {1, 2}

    def test_people_crossing_do_not_merge(self) -> None:
        tracker = KalmanIouTracker(TrackerConfig(min_hits=1))
        for index in range(20):
            step = index * 0.03
            tracks = tracker.update(
                [detect(0.20 + step, frame_seq=index), detect(0.70 - step, frame_seq=index)],
                index,
                float(index),
            )
        assert len(tracks) == 2

    def test_ids_are_assigned_in_detection_order(self) -> None:
        tracker = KalmanIouTracker(TrackerConfig(min_hits=1))
        tracks = tracker.update([detect(0.1), detect(0.6)], 0, 0.0)
        assert sorted(t.track_id for t in tracks) == [1, 2]

    def test_active_count_tracks_only_actionable_tracks(self) -> None:
        tracker = KalmanIouTracker(TrackerConfig(min_hits=1))
        tracker.update([detect(0.1), detect(0.6)], 0, 0.0)
        assert tracker.active_count == 2
        tracker.update([], 1, 1.0)
        assert tracker.active_count == 0
        assert tracker.track_count == 2


class TestLabelsAndClipping:
    def test_label_follows_the_detection(self) -> None:
        tracker = KalmanIouTracker(TrackerConfig(min_hits=1))
        tracker.update([detect(0.3, label="person")], 0, 0.0)
        tracks = tracker.update([detect(0.31, label="person")], 1, 1.0)
        assert tracks[0].label == "person"

    def test_clip_flag_is_propagated(self) -> None:
        from visionops.vision.contracts import Detection as D

        clipped = D(
            class_id=0, label="person", confidence=0.9, box=BBox(0.0, 0.3, 0.1, 0.8), clip=True
        )
        tracker = KalmanIouTracker(TrackerConfig(min_hits=1))
        tracks = tracker.update([clipped], 0, 0.0)
        assert tracks[0].clip is True
        assert not tracks[0].is_actionable


class TestLifecycle:
    def test_reset_clears_tracks_and_restarts_ids(self) -> None:
        tracker = KalmanIouTracker(TrackerConfig(min_hits=1))
        tracker.update([detect(0.3)], 0, 0.0)
        tracker.reset()
        assert tracker.track_count == 0
        assert tracker.update([detect(0.3)], 1, 1.0)[0].track_id == 1

    def test_non_monotonic_frames_do_not_duplicate_tracks(self) -> None:
        tracker = KalmanIouTracker(TrackerConfig(min_hits=1))
        tracker.update([detect(0.3)], 5, 5.0)
        tracks = tracker.update([detect(0.31)], 2, 6.0)  # replayed sequence
        assert [t.track_id for t in tracks] == [1]

    def test_confidence_is_reported(self) -> None:
        tracker = KalmanIouTracker(TrackerConfig(min_hits=1))
        tracks = tracker.update([detect(0.3, confidence=0.87)], 0, 0.0)
        assert tracks[0].confidence == pytest.approx(0.87, abs=1e-4)


class TestConfigDefaults:
    def test_defaults_match_the_documented_values(self) -> None:
        config = TrackerConfig()
        assert config.match_iou == 0.3
        assert config.max_age == 30
        assert config.min_hits == 3
        assert config.high_score == 0.5

    def test_unknown_registry_name_is_a_clear_error(self) -> None:
        from visionops.vision.registry import build_tracker

        with pytest.raises(KeyError, match="unknown tracker"):
            build_tracker("does-not-exist", TrackerConfig())
