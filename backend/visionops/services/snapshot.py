"""Optional event snapshots.

Snapshots are **off by default**. When an organisation enables them, one
annotated JPEG is written per event under a date-partitioned path, with
owner-only permissions, and the file is deleted with its event by the retention
job. Privacy-by-default, opt-in evidence.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from ..core.security import resolve_within
from ..ingest.base import encode_jpeg
from ..vision.contracts import EventRecord, Frame
from ..vision.render import annotate

logger = logging.getLogger(__name__)

JPEG_QUALITY = 75
#: Snapshots are evidence, not identity records: keep them small.
MAX_SNAPSHOT_WIDTH = 960


class SnapshotWriter:
    """Writes annotated JPEGs for events into a date-partitioned directory."""

    def __init__(self, *, root: Path, camera_id_label: str = "unknown") -> None:
        self._root = Path(root)
        self._camera_id = camera_id_label

    def write(self, event: EventRecord, frame: Frame) -> str | None:
        """Render and store one snapshot; return its root-relative path.

        Any failure returns ``None`` rather than raising: losing a snapshot must
        never lose the event.
        """
        try:
            moment = datetime.fromtimestamp(event.occurred_at, UTC)
            day = moment.strftime("%Y/%m/%d")
            directory = self._root / day
            directory.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):  # pragma: no cover - platform dependent
                directory.chmod(0o700)
            event_id = event.id or "unknown"
            annotated = annotate(
                frame.image,
                (),
                (),
                max_width=MAX_SNAPSHOT_WIDTH,
            )
            payload = encode_jpeg(annotated, quality=JPEG_QUALITY)
            path = directory / f"{event_id}.jpg"
            path.write_bytes(payload)
            with contextlib.suppress(OSError):  # pragma: no cover - platform dependent
                path.chmod(0o600)
            relative = path.relative_to(self._root).as_posix()
            logger.debug("wrote snapshot %s for event %s", relative, event_id)
            return relative
        except Exception:
            logger.warning("snapshot write failed for event %s", event.id, exc_info=True)
            return None

    def resolve(self, relative_path: str) -> Path:
        """Resolve a stored relative path, refusing traversal."""
        return resolve_within(self._root, relative_path)

    def delete(self, relative_paths: list[str]) -> int:
        """Delete stored snapshots, ignoring ones that are already gone."""
        removed = 0
        for relative in relative_paths:
            try:
                self.resolve(relative).unlink(missing_ok=True)
                removed += 1
            except (ValueError, OSError) as exc:
                logger.warning("could not delete snapshot %s: %s", relative, exc)
        return removed


def blank_snapshot(width: int = 640, height: int = 360, label: str = "visionops") -> np.ndarray:
    """A placeholder image (used when a real frame is unavailable)."""
    image = np.zeros((height, width, 3), dtype=np.uint8)
    cv2.putText(
        image, label, (16, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (200, 200, 200), 1, cv2.LINE_AA
    )
    return image


def snapshot_mjpeg(frame_image: Any) -> bytes:
    return encode_jpeg(frame_image, quality=70)


__all__ = ["MAX_SNAPSHOT_WIDTH", "SnapshotWriter", "blank_snapshot", "snapshot_mjpeg"]
