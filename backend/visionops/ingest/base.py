"""The :class:`VideoSource` interface and shared helpers.

A source's only job is to produce frames and report its own health. It makes no
computer-vision decisions (``docs/REPOSITORY_STRUCTURE.md`` §2.1).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Protocol

import cv2
import numpy as np

from ..vision.contracts import Frame, SourceHealth, SourceKind, SourceState

logger = logging.getLogger(__name__)

#: Reason codes are part of the public contract: clients may switch on them.
REASON_CODES: frozenset[str] = frozenset(
    {
        "CAMERA_BUSY",
        "CAMERA_NOT_FOUND",
        "CAMERA_NO_PERMISSION",
        "RTSP_TIMEOUT",
        "RTSP_AUTH_FAILED",
        "RTSP_UNSUPPORTED",
        "FILE_NOT_FOUND",
        "FILE_DECODE_ERROR",
        "SOURCE_CLOSED",
        "SOURCE_STALLED",
        "SOURCE_ENDED",
        "INVALID_FRAME",
        "WAITING_FOR_CLIENT",
    }
)


class SourceOpenError(RuntimeError):
    """A source could not be opened. Carries a stable reason code."""

    def __init__(self, reason_code: str, detail: str) -> None:
        super().__init__(detail)
        self.reason_code = reason_code
        self.detail = detail


class VideoSource(Protocol):
    """Produces frames and reports health."""

    kind: SourceKind

    def open(self) -> None: ...

    def read(self) -> Frame | None:
        """Return the next frame, or ``None`` when there is none right now.

        ``None`` is ambiguous on its own — it means both "the stream has
        ended" and "no frame has arrived yet". Pair it with :attr:`at_end` to
        tell those apart.
        """
        ...

    @property
    def at_end(self) -> bool:
        """Whether the stream has genuinely finished.

        ``False`` for live sources: a browser tab that has not pushed yet and a
        camera that stalled for a moment are both *waiting*, not finished.
        """
        ...

    def close(self) -> None: ...

    def reopen(self) -> None: ...

    @property
    def health(self) -> SourceHealth: ...

    def position(self) -> float | None: ...

    def duration(self) -> float | None: ...


class BaseSource:
    """Shared bookkeeping: sequencing, timestamps, fps and health."""

    kind: SourceKind = SourceKind.FILE

    def __init__(self, *, name: str = "") -> None:
        self.name = name
        self._health = SourceHealth(kind=self.kind)
        self._seq = 0
        self._opened_at: float | None = None
        self._last_frame_at: float | None = None
        self._frame_times: list[float] = []
        self._closed = False

    # -- to implement -------------------------------------------------------

    def open(self) -> None:
        raise NotImplementedError

    def _read_raw(self) -> np.ndarray | None:
        raise NotImplementedError

    def close(self) -> None:
        self._closed = True
        self._health.state = SourceState.STOPPED

    # -- shared -------------------------------------------------------------

    @property
    def health(self) -> SourceHealth:
        return self._health

    def position(self) -> float | None:
        return self._health.position

    def duration(self) -> float | None:
        return self._health.duration

    @property
    def at_end(self) -> bool:
        """A source is finished only once it has said it failed."""
        return self._health.state is SourceState.FAILED

    def read(self) -> Frame | None:
        if self._closed:
            return None
        image = self._read_raw()
        now = time.time()
        if image is None:
            # The subclass has already said what the empty read means: a file at
            # its end marks FAILED, a stalled camera or an unconnected browser
            # tab marks DEGRADED. Overwriting it here with RUNNING would throw
            # that diagnosis away, so the state is left exactly as set.
            return None
        if image.size == 0:
            self._health.dropped += 1
            self._health.reason_code = "INVALID_FRAME"
            self._health.detail = "decoder returned an empty frame"
            return None

        height, width = image.shape[:2]
        self._seq += 1
        self._last_frame_at = now
        self._track_fps(now)
        self._health.frames += 1
        self._health.last_frame_at = now
        if self._health.state in (SourceState.STARTING, SourceState.DEGRADED):
            self._health.state = SourceState.RUNNING
        if self._opened_at is None:
            self._opened_at = now

        return Frame(
            image=image,
            seq=self._seq,
            timestamp=now,
            width=int(width),
            height=int(height),
        )

    def _track_fps(self, now: float) -> None:
        self._frame_times.append(now)
        if len(self._frame_times) > 30:
            del self._frame_times[:-30]
        if len(self._frame_times) >= 2:
            span = self._frame_times[-1] - self._frame_times[0]
            if span > 0:
                self._health.fps = round((len(self._frame_times) - 1) / span, 2)

    def _mark_running(self) -> None:
        self._health.state = SourceState.RUNNING
        self._health.reason_code = None
        self._health.detail = None

    def _mark_failed(self, reason_code: str, detail: str) -> None:
        self._health.state = SourceState.FAILED
        self._health.reason_code = reason_code
        self._health.detail = detail

    def _mark_degraded(self, reason_code: str, detail: str) -> None:
        self._health.state = SourceState.DEGRADED
        self._health.reason_code = reason_code
        self._health.detail = detail

    def retry_later(self) -> None:
        self._health.retries += 1

    def reopen(self) -> None:
        """Close and re-open, so a retry starts from a clean capture handle."""
        try:
            self.close()
        except Exception:
            logger.debug("error closing source before reopen", exc_info=True)
        self._closed = False
        self._health.state = SourceState.STARTING


def opencv_availability_error() -> str | None:
    """A human-readable reason when OpenCV cannot be used, or ``None``."""
    try:
        cv2.getBuildInformation()
    except Exception as exc:
        return f"OpenCV is unavailable: {exc}"
    return None


def decode_image(payload: bytes) -> np.ndarray | None:
    """Decode a JPEG/PNG payload into a BGR array, or ``None`` when invalid."""
    if not payload:
        return None
    buffer = np.frombuffer(payload, dtype=np.uint8)
    image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    if image is None or image.size == 0:
        return None
    return image


def encode_jpeg(image: np.ndarray, quality: int = 70) -> bytes:
    """Encode a BGR image as JPEG (used for snapshots and MJPEG preview)."""
    ok, buffer = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise RuntimeError("failed to encode JPEG")
    return buffer.tobytes()


SourceFactory = Callable[..., VideoSource]


__all__ = [
    "REASON_CODES",
    "BaseSource",
    "SourceFactory",
    "SourceOpenError",
    "VideoSource",
    "decode_image",
    "encode_jpeg",
    "opencv_availability_error",
]
