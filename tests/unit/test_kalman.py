"""Kalman filter: no lag, correct velocity, stable covariance.

The filter's tuning matters more than it looks: too little position process noise
and the estimate lags the measurement, which halves the learned velocity and
breaks occlusion recovery (``docs/VISION_ENGINE.md`` §5.1).
"""

from __future__ import annotations

import numpy as np
import pytest

from visionops.vision.contracts import BBox
from visionops.vision.kalman import KalmanBoxFilter


def walk(steps: int, start: float = 0.1, step: float = 0.04) -> list[BBox]:
    return [
        BBox.from_xyxy(start + step * i, 0.3, start + step * i + 0.1, 0.8) for i in range(steps)
    ]


class TestInit:
    def test_starts_at_the_measurement_with_zero_velocity(self) -> None:
        box = BBox(0.1, 0.2, 0.3, 0.8)
        kf = KalmanBoxFilter(box)
        assert kf.smoothed_box.as_list() == pytest.approx(box.as_list(), abs=1e-9)
        assert kf.velocity == pytest.approx((0.0, 0.0), abs=1e-9)

    def test_state_is_eight_dimensional(self) -> None:
        assert KalmanBoxFilter(BBox(0.1, 0.1, 0.2, 0.2)).state.shape == (8,)


class TestTracking:
    def test_follows_a_stationary_target(self) -> None:
        box = BBox(0.2, 0.2, 0.4, 0.8)
        kf = KalmanBoxFilter(box)
        for _ in range(30):
            kf.predict()
            kf.update(box)
        assert kf.smoothed_box.as_list() == pytest.approx(box.as_list(), abs=0.01)

    def test_has_no_systematic_lag_on_a_constant_velocity_target(self) -> None:
        """The estimate must not trail the measurement.

        This is the property whose loss silently broke occlusion recovery: a
        lagged filter reports half the true velocity.
        """
        boxes = walk(40, start=0.05, step=0.02)
        kf = KalmanBoxFilter(boxes[0])
        for box in boxes[1:]:
            kf.predict()
            kf.update(box)
        lag = kf.smoothed_box.x1 - boxes[-1].x1
        assert abs(lag) < 0.005, f"filter lags the target by {lag:+.4f}"

    def test_learns_the_true_velocity(self) -> None:
        boxes = walk(40, start=0.05, step=0.02)  # stays inside the frame
        kf = KalmanBoxFilter(boxes[0])
        for box in boxes[1:]:
            kf.predict()
            kf.update(box)
        assert kf.velocity[0] == pytest.approx(0.02, abs=0.004)
        assert kf.velocity[1] == pytest.approx(0.0, abs=0.01)

    def test_ignores_a_single_noisy_measurement(self) -> None:
        boxes = walk(20, start=0.2, step=0.02)
        kf = KalmanBoxFilter(boxes[0])
        for box in boxes[1:]:
            kf.predict()
            kf.update(box)
        clean = kf.smoothed_box.x1
        noisy = BBox.from_xyxy(boxes[-1].x1 + 0.08, 0.3, boxes[-1].x2 + 0.08, 0.8)
        kf.predict()
        kf.update(noisy)
        assert kf.smoothed_box.x1 - clean < 0.08  # smoothed, not jumped to

    def test_smaller_boxes_are_treated_as_measured_less_precisely(self) -> None:
        """Size-aware measurement noise: a small distant person is noisier."""
        big = KalmanBoxFilter(BBox(0.1, 0.1, 0.6, 0.9))
        small = KalmanBoxFilter(BBox(0.4, 0.4, 0.45, 0.5))
        assert (
            big.measurement_noise(BBox(0.1, 0.1, 0.6, 0.9)).max()
            < small.measurement_noise(BBox(0.4, 0.4, 0.45, 0.5)).max()
        )

    def test_larger_boxes_move_further_per_observation(self) -> None:
        kf = KalmanBoxFilter(BBox(0.1, 0.1, 0.9, 0.9))
        for _ in range(20):
            kf.predict()
            kf.update(BBox(0.1, 0.1, 0.9, 0.9))
        before = kf.smoothed_box.x1
        kf.update(BBox(0.1, 0.1, 0.9, 0.9))
        assert abs(kf.smoothed_box.x1 - before) < 0.01


class TestPrediction:
    def test_projects_forward_along_the_learned_velocity(self) -> None:
        boxes = walk(20, start=0.1, step=0.02)
        kf = KalmanBoxFilter(boxes[0])
        for box in boxes[1:]:
            kf.predict()
            kf.update(box)
        current = kf.smoothed_box.x1
        for _ in range(3):
            predicted = kf.predict()
        assert predicted.x1 == pytest.approx(current + 3 * 0.02, abs=0.01)

    def test_prediction_keeps_the_box_well_formed(self) -> None:
        kf = KalmanBoxFilter(BBox(0.1, 0.1, 0.2, 0.2))
        for _ in range(200):
            box = kf.predict()
            assert box.x1 <= box.x2
            assert box.y1 <= box.y2
            assert 0.0 <= box.x1 <= 1.0
            assert 0.0 <= box.y2 <= 1.0

    def test_prediction_clamps_to_the_frame(self) -> None:
        kf = KalmanBoxFilter(BBox(0.85, 0.1, 0.95, 0.9))
        for _ in range(50):
            kf.predict()
            kf.update(BBox(0.85, 0.1, 0.95, 0.9))
        for _ in range(50):
            box = kf.predict()
        assert 0.0 <= box.x1 <= 1.0
        assert 0.0 <= box.x2 <= 1.0


class TestCovariance:
    def test_covariance_stays_symmetric_and_positive(self) -> None:
        kf = KalmanBoxFilter(BBox(0.1, 0.1, 0.3, 0.7))
        rng = np.random.default_rng(11)
        for _ in range(60):
            kf.predict()
            jitter = BBox.from_xyxy(
                0.1 + rng.normal(0, 0.002), 0.1, 0.3 + rng.normal(0, 0.002), 0.7
            )
            kf.update(jitter)
            covariance = kf._p
            assert np.allclose(covariance, covariance.T, atol=1e-12)
            assert np.all(np.diag(covariance) >= -1e-12)

    def test_covariance_shrinks_as_observations_accumulate(self) -> None:
        kf = KalmanBoxFilter(BBox(0.2, 0.2, 0.4, 0.8))
        kf.predict()
        for _ in range(40):
            kf.predict()
            kf.update(BBox(0.2, 0.2, 0.4, 0.8))
        assert np.trace(kf._p) < 1.0


class TestDegenerateInput:
    def test_inverted_state_is_repaired_not_raised(self) -> None:
        kf = KalmanBoxFilter(BBox(0.1, 0.1, 0.5, 0.5))
        # Force an inconsistent state the way a wild prediction might.
        kf._x[0], kf._x[2] = 0.8, 0.1
        box = kf.smoothed_box
        assert box.x1 <= box.x2

    def test_zero_area_measurement_is_tolerated(self) -> None:
        kf = KalmanBoxFilter(BBox(0.2, 0.2, 0.2, 0.2))
        kf.update(BBox(0.2, 0.2, 0.2, 0.2))  # must not raise
        assert kf.smoothed_box is not None
