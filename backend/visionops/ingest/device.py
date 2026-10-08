"""Local camera (V4L2 / device index) and RTSP sources, both via OpenCV."""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse, urlunparse

import cv2

from ..vision.contracts import SourceKind
from .base import BaseSource, SourceOpenError

logger = logging.getLogger(__name__)

#: Schemes a remote source may use. Anything else (file://, gopher://, dict://)
#: is rejected before we touch it — the SSRF control in docs/SECURITY_PRIVACY.md.
ALLOWED_URL_SCHEMES: frozenset[str] = frozenset({"rtsp", "rtsps", "http", "https"})
#: OpenCV env knobs that make RTSP timeouts and buffering behave predictably.
FFMPEG_ENV = {
    "OPENCV_FFMPEG_CAPTURE_OPTIONS": "rtsp_transport;tcp|buffer_size;1048576|max_delay;500000",
    "OPENCV_FFMPEG_WRITER_OPTIONS": "buffer_size;1048576",
}


def validate_remote_url(target: str) -> str:
    """Validate a remote source URL and reject embedded credentials.

    Credentials belong in encrypted storage, never in a URL that ends up in
    logs, the database or the UI.
    """
    parsed = urlparse(target)
    if parsed.scheme.lower() not in ALLOWED_URL_SCHEMES:
        allowed = ", ".join(sorted(ALLOWED_URL_SCHEMES))
        raise SourceOpenError(
            "RTSP_UNSUPPORTED",
            f"unsupported source scheme {parsed.scheme or '(none)'!r}; allowed: {allowed}",
        )
    if parsed.username or parsed.password or "@" in (parsed.netloc.split("]")[-1]):
        raise SourceOpenError(
            "RTSP_AUTH_FAILED",
            "the source URL must not embed credentials — set them in the source credentials field",
        )
    return target


def build_credentialed_url(target: str, username: str, password: str) -> str:
    """Insert credentials into a validated URL, percent-encoding them."""
    validate_remote_url(target)
    parsed = urlparse(target)
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{quote(username, safe='')}:{quote(password, safe='')}@{host}"
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))


class _CvCaptureSource(BaseSource):
    """Shared OpenCV capture behaviour for device and RTSP sources."""

    def __init__(
        self, *, name: str, open_timeout_ms: int = 5000, read_timeout_ms: int = 5000
    ) -> None:
        super().__init__(name=name)
        self._capture: cv2.VideoCapture | None = None
        self._open_timeout_ms = open_timeout_ms
        self._read_timeout_ms = read_timeout_ms

    def _configure(self, capture: cv2.VideoCapture) -> None:
        for prop, value in (
            (cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, self._open_timeout_ms),
            (cv2.CAP_PROP_READ_TIMEOUT_MSEC, self._read_timeout_ms),
            (cv2.CAP_PROP_BUFFERSIZE, 1),
        ):
            # Not every OpenCV build honours every property; ignore those.
            with contextlib.suppress(cv2.error):  # pragma: no cover - build dependent
                capture.set(prop, value)

    def _read_raw(self) -> Any:
        if self._capture is None:
            return None
        ok, image = self._capture.read()
        if not ok or image is None:
            self._mark_degraded(
                "SOURCE_STALLED",
                "the stream stopped delivering frames — retrying",
            )
            return None
        return image

    def close(self) -> None:
        if self._capture is not None:
            self._capture.release()
            self._capture = None
        super().close()


class DeviceSource(_CvCaptureSource):
    """A locally attached camera addressed by index (``/dev/video0`` = 0)."""

    kind = SourceKind.DEVICE

    def __init__(self, index: int, *, open_timeout_ms: int = 5000, name: str = "") -> None:
        super().__init__(name=name or f"device:{index}", open_timeout_ms=open_timeout_ms)
        self.index = index

    def open(self) -> None:
        capture = cv2.VideoCapture(self.index)
        self._configure(capture)
        if not capture.isOpened():
            capture.release()
            raise SourceOpenError(
                "CAMERA_BUSY" if self._device_exists() else "CAMERA_NOT_FOUND",
                self._failure_detail(),
            )
        self._capture = capture
        self._health.duration = None
        self._mark_running()
        logger.info("opened device source index=%s", self.index)

    def _device_exists(self) -> bool:
        """A camera another process holds is the most common V4L2 failure."""
        return Path(f"/dev/video{self.index}").exists()

    def _failure_detail(self) -> str:
        if self._device_exists():
            return (
                f"camera index {self.index} is present but busy — another application is "
                "probably using it (close browser tabs, video calls or other CV tools)"
            )
        return (
            f"camera index {self.index} could not be opened — check that the device exists "
            "and the user is in the 'video' group"
        )

    def retry(self) -> None:
        self.close()
        self._closed = False
        self.open()


class RtspSource(_CvCaptureSource):
    """A network camera addressed by URL."""

    kind = SourceKind.RTSP

    def __init__(
        self,
        target: str,
        *,
        username: str = "",
        password: str = "",
        transport: str = "tcp",
        open_timeout_ms: int = 5000,
        read_timeout_ms: int = 5000,
        name: str = "",
    ) -> None:
        super().__init__(
            name=name or "rtsp", open_timeout_ms=open_timeout_ms, read_timeout_ms=read_timeout_ms
        )
        self.target = validate_remote_url(target)
        self.username = username
        self.password = password
        self.transport = transport if transport in ("tcp", "udp") else "tcp"

    def open(self) -> None:
        url = (
            build_credentialed_url(self.target, self.username, self.password)
            if self.username
            else self.target
        )
        import os

        previous = {key: os.environ.get(key) for key in FFMPEG_ENV}
        os.environ.update(FFMPEG_ENV)
        try:
            capture = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
            self._configure(capture)
            if not capture.isOpened():
                capture.release()
                raise SourceOpenError("RTSP_TIMEOUT", self._failure_detail())
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self._capture = capture
        self._mark_running()
        # Never log the credentialed URL.
        logger.info("opened rtsp source %s (transport=%s)", self.target, self.transport)

    def _failure_detail(self) -> str:
        if self.username:
            return "the RTSP camera could not be opened — check the URL and credentials"
        return (
            f"could not connect to {self.target} — check the host, port, path and that the "
            f"camera allows connections ({self.transport.upper()})"
        )

    def retry(self) -> None:
        self.close()
        self._closed = False
        self.open()


__all__ = [
    "ALLOWED_URL_SCHEMES",
    "FFMPEG_ENV",
    "DeviceSource",
    "RtspSource",
    "build_credentialed_url",
    "validate_remote_url",
]
