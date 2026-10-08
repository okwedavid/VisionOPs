"""Browser webcam source: frames pushed from the page over a WebSocket (ADR-009).

The browser is the capture device. This keeps the server free of camera
libraries, makes the webcam path testable in CI with a fake capture device, and
keeps frames on the operator's own machine or LAN — never a third party.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any

import numpy as np

from ..vision.contracts import Frame, SourceKind, SourceState
from .base import BaseSource, SourceOpenError, decode_image

logger = logging.getLogger(__name__)

#: Bounded queue: if the pipeline cannot keep up we drop the *oldest* frame
#: rather than accumulate latency. Freshness beats completeness for live view.
QUEUE_SIZE = 4


class BrowserFrameSource(BaseSource):
    """A push-based source fed by the WebSocket ingest endpoint."""

    kind = SourceKind.BROWSER

    def __init__(self, *, session_id: str, target_fps: float = 5.0, name: str = "browser") -> None:
        super().__init__(name=name)
        self.session_id = session_id
        self.target_fps = max(0.5, target_fps)
        self._queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=QUEUE_SIZE)
        self._client_connected = threading.Event()
        self._declared_size: tuple[int, int] | None = None
        self._invalid_frames = 0
        self._dropped_frames = 0
        self._lock = threading.Lock()

    # -- lifecycle ----------------------------------------------------------

    def open(self) -> None:
        self._health.state = SourceState.STARTING
        self._health.detail = "waiting for the browser to send frames"
        self._health.reason_code = "WAITING_FOR_CLIENT"
        self._opened_at = time.time()

    def close(self) -> None:
        self._client_connected.clear()
        super().close()

    # -- ingest -------------------------------------------------------------

    def attach_client(self, width: int = 0, height: int = 0) -> None:
        with self._lock:
            self._client_connected.set()
            if width and height:
                self._declared_size = (int(width), int(height))
        self._health.state = SourceState.RUNNING
        self._health.reason_code = None
        self._health.detail = None

    def detach_client(self) -> None:
        self._client_connected.clear()
        if not self._closed:
            self._mark_degraded(
                "SOURCE_CLOSED", "the browser disconnected — grant camera access and reconnect"
            )

    @property
    def client_connected(self) -> bool:
        return self._client_connected.is_set()

    def push_frame(self, payload: bytes, *, source_timestamp: float | None = None) -> bool:
        """Accept one encoded frame. Returns ``False`` when it was rejected."""
        image = decode_image(payload)
        if image is None:
            self._invalid_frames += 1
            self._health.dropped += 1
            return False
        try:
            self._queue.put_nowait(image)
        except queue.Full:
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(image)
            except (queue.Empty, queue.Full):  # pragma: no cover - race
                pass
            self._dropped_frames += 1
        self.source_timestamp = source_timestamp
        return True

    # -- reading ------------------------------------------------------------

    def _read_raw(self) -> Any:
        if not self._client_connected.is_set():
            return None
        try:
            return self._queue.get(timeout=1.0)
        except queue.Empty:
            return None

    def read(self) -> Frame | None:
        """Read the next pushed frame, waiting briefly for one to arrive.

        Frames that already arrived are always served, even if the browser has
        since disconnected. Discarding them would silently lose real captures at
        exactly the moment a tab is closed or a network blip happens, which is
        when an operator is most likely to be watching.
        """

        connected = self._client_connected.is_set()

        if not connected:
            try:
                image = self._queue.get_nowait()
            except queue.Empty:
                if self._health.state is not SourceState.STARTING:
                    self._mark_degraded(
                        "WAITING_FOR_CLIENT",
                        "waiting for the browser to connect and share its camera",
                    )
                return None
            return self._frame_from_image(image, connected=False)

        try:
            image = self._queue.get(timeout=2.0)
        except queue.Empty:
            # No frame for 2 s is a stall, not an error: the tab may be backgrounded.
            self._mark_degraded("SOURCE_STALLED", "no frames received from the browser for 2 s")
            return None

        return self._frame_from_image(image)

    def _frame_from_image(self, image: Any, *, connected: bool = True) -> Frame:
        """Bookkeeping and validation shared by the connected and draining paths."""
        self._mark_running()
        now = time.time()
        height, width = image.shape[:2]
        self._seq += 1
        self._track_fps(now)
        self._health.frames += 1
        self._health.last_frame_at = now
        if not connected:
            # Draining after a disconnect: the frame is real, but no more are
            # coming, so the session will stop once the queue empties.
            self._mark_degraded("SOURCE_CLOSED", "draining frames after the browser disconnected")
        self._health.dropped += self._invalid_frames
        if self._opened_at is None:
            self._opened_at = now
        return Frame(
            image=image,
            seq=self._seq,
            timestamp=now,
            width=int(width),
            height=int(height),
            source_timestamp=self.source_timestamp,
        )

    source_timestamp: float | None = None

    def health_extra(self) -> dict[str, object]:
        return {
            "client_connected": self.client_connected,
            "invalid_frames": self._invalid_frames,
            "queue_dropped": self._dropped_frames,
        }

    def open_or_raise(self) -> None:
        """Uniform open path so the session runner handles all sources alike."""
        try:
            self.open()
        except SourceOpenError:  # pragma: no cover - the browser source cannot fail to open
            raise
