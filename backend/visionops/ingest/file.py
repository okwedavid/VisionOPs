"""File-backed video source with optional looping.

Also the deterministic test path: a committed fixture video exercises decode,
rate control, lifecycle and failure handling with no camera hardware.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import cv2

from ..vision.contracts import SourceKind, SourceState
from .base import BaseSource, SourceOpenError

logger = logging.getLogger(__name__)


class FileSource(BaseSource):
    """Reads a video file from disk, looping when configured."""

    kind = SourceKind.FILE

    def __init__(
        self,
        path: Path,
        *,
        loop: bool = False,
        media_root: Path | None = None,
        name: str = "",
    ) -> None:
        super().__init__(name=name or path.name)
        self.path = path
        self.loop = loop
        self.media_root = media_root
        self._capture: cv2.VideoCapture | None = None
        self._frame_count = 0
        self._fps = 0.0
        self._position = 0.0

    def open(self) -> None:
        resolved = self._resolve_path()
        if not resolved.exists():
            raise SourceOpenError(
                "FILE_NOT_FOUND",
                f"video file not found: {resolved}. "
                "Check the source target and VISIONOPS_MEDIA_ROOT.",
            )
        capture = cv2.VideoCapture(str(resolved))
        if not capture.isOpened():
            capture.release()
            raise SourceOpenError(
                "FILE_DECODE_ERROR",
                f"could not open {resolved.name} — unsupported codec or corrupt file",
            )
        self._capture = capture
        self._frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        self._fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        self._position = 0.0
        self._health.duration = self._frame_count / self._fps if self._fps > 0 else None
        self._health.position = 0.0
        self._mark_running()
        logger.info(
            "opened file source %s (%d frames @ %.2f fps)",
            resolved.name,
            self._frame_count,
            self._fps,
        )

    def _resolve_path(self) -> Path:
        """Resolve the path, refusing anything outside the media root.

        The containment check runs for *every* target, including absolute ones:
        skipping it for absolute paths would let any caller who can create a
        source read any video file the process can open (``docs/SECURITY_PRIVACY.md``
        threat T-04). A ``file`` source with no configured media root has no
        boundary to check against, so it refuses rather than trusting the target.
        """
        from ..core.security import resolve_within

        if self.media_root is None:
            raise SourceOpenError(
                "MEDIA_ROOT_NOT_CONFIGURED",
                "no media root is configured, so file sources are refused. "
                "Set VISIONOPS_MEDIA_ROOT to a directory that holds your videos.",
            )
        try:
            return resolve_within(self.media_root, self.path)
        except ValueError as exc:
            raise SourceOpenError(
                "MEDIA_ROOT_VIOLATION",
                f"{exc}. File sources may only read from the configured media root "
                "(VISIONOPS_MEDIA_ROOT).",
            ) from exc

    def _read_raw(self) -> Any:
        if self._capture is None:
            return None
        ok, image = self._capture.read()
        if not ok or image is None:
            if not self.loop or self._frame_count <= 0:
                self._mark_failed(
                    "SOURCE_ENDED",
                    f"reached the end of {self.path.name} ({self._frame_count} frames)",
                )
                return None
            self._capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, image = self._capture.read()
            if not ok or image is None:
                self._mark_failed("SOURCE_ENDED", "loop seek failed")
                return None
        self._position = float(self._capture.get(cv2.CAP_PROP_POS_FRAMES) or 0.0)
        self._health.position = self._position
        return image

    def close(self) -> None:
        if self._capture is not None:
            self._capture.release()
            self._capture = None
        super().close()

    def position(self) -> float | None:
        return self._position

    @property
    def at_end(self) -> bool:
        """A file ends; loop mode never does."""
        return not self.loop and self._health.state is SourceState.FAILED

    def duration(self) -> float | None:
        if self._fps <= 0 or self._frame_count <= 0:
            return None
        return self._frame_count / self._fps

    @property
    def progress(self) -> float:
        """Fraction of the clip played, in ``[0, 1]``."""
        if self._frame_count <= 0:
            return 0.0
        return min(1.0, self._position / self._frame_count)


__all__ = ["FileSource"]
