"""Constant-velocity Kalman filter for normalised bounding boxes.

The state is ``(x1, y1, x2, y2, vx1, vy1, vx2, vy2)``. Working in normalised
space makes the tuning scale-invariant: the same gains work for a 320x240
fixture and a 4K camera (``docs/VISION_ENGINE.md`` §5.1).
"""

from __future__ import annotations

import numpy as np

from .contracts import BBox

#: Process noise. Position noise must be large enough for the filter to *follow*
#: a target that translates a few percent of the frame per frame — too small and
#: the estimate lags the measurement, which also halves the learned velocity and
#: breaks occlusion recovery. Tuned so a target moving 4 %/frame is tracked with
#: no systematic lag (see ``tests/unit/test_kalman.py``).
_PROCESS_NOISE_POS = 2.0e-2
_PROCESS_NOISE_VEL = 4.0e-3
#: Measurement noise. A larger box covers more pixels, so its *relative*
#: measurement is more precise and its absolute normalised noise must be
#: **smaller**. Modelling this correctly is what removes jitter on big, close
#: objects without over-smoothing small distant ones.
_MEASUREMENT_NOISE_FLOOR = 1.0e-3
_MEASUREMENT_NOISE_RANGE = 2.0e-2

_STATE_DIM = 8
_MEASURE_DIM = 4


class KalmanBoxFilter:
    """A minimal, well-conditioned Kalman filter for one box."""

    __slots__ = ("_f", "_h", "_p", "_q", "_r_base", "_x")

    def __init__(self, box: BBox) -> None:
        self._f = np.eye(_STATE_DIM, dtype=np.float64)
        for i in range(4):
            self._f[i, i + 4] = 1.0

        self._h = np.zeros((_MEASURE_DIM, _STATE_DIM), dtype=np.float64)
        for i in range(4):
            self._h[i, i] = 1.0

        self._q = np.eye(_STATE_DIM, dtype=np.float64) * _PROCESS_NOISE_POS
        for i in range(4, 8):
            self._q[i, i] = _PROCESS_NOISE_VEL

        self._x = np.zeros(_STATE_DIM, dtype=np.float64)
        self._x[:4] = (box.x1, box.y1, box.x2, box.y2)
        self._p = np.eye(_STATE_DIM, dtype=np.float64) * 0.1
        self._r_base = np.eye(_MEASURE_DIM, dtype=np.float64)

    @property
    def state(self) -> np.ndarray:
        return self._x

    def measurement_noise(self, box: BBox) -> np.ndarray:
        """Size-aware measurement noise: bigger boxes are measured more precisely."""
        size = max(box.width, box.height, 1e-3)
        sigma = _MEASUREMENT_NOISE_FLOOR + _MEASUREMENT_NOISE_RANGE / (1.0 + size)
        return self._r_base * (sigma * sigma)

    def predict(self) -> BBox:
        """Advance one step and return the predicted box (clamped to the frame)."""
        self._x = self._f @ self._x
        self._p = self._f @ self._p @ self._f.T + self._q
        return self._clamped()

    def update(self, box: BBox) -> None:
        """Fuse a measurement into the state."""
        z = np.array([box.x1, box.y1, box.x2, box.y2], dtype=np.float64)
        y = z - self._h @ self._x
        s = self._h @ self._p @ self._h.T + self.measurement_noise(box)
        try:
            k = self._p @ self._h.T @ np.linalg.inv(s)
        except np.linalg.LinAlgError:  # pragma: no cover - numerically defensive
            return
        self._x = self._x + k @ y
        identity = np.eye(_STATE_DIM, dtype=np.float64)
        self._p = (identity - k @ self._h) @ self._p

    def _clamped(self) -> BBox:
        x1, y1, x2, y2 = (float(v) for v in self._x[:4])
        if x2 < x1:
            x1, x2 = x2, x1
        if y2 < y1:
            y1, y2 = y2, y1
        return BBox.from_xyxy(x1, y1, x2, y2)

    @property
    def velocity(self) -> tuple[float, float]:
        """Velocity of the box centre in normalised units per frame.

        The box centre is the mean of its two corners, so its velocity is the
        mean of their velocities.
        """
        return (float(self._x[4] + self._x[6]) / 2.0, float(self._x[5] + self._x[7]) / 2.0)

    @property
    def smoothed_box(self) -> BBox:
        return self._clamped()


__all__ = ["KalmanBoxFilter"]
