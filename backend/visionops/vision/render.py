"""Server-side frame annotation for the MJPEG preview.

Deliberately separate from the browser overlay: the browser draws detections
itself from WebSocket state (lower latency, no video round-trip), while this
produces a self-contained JPEG for curl, ffplay, recording and the fallback
preview path.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import cv2
import numpy as np

from .contracts import Severity, Track, ZoneSpec
from .geometry import denormalize_box, point_in_polygon

logger = logging.getLogger(__name__)

SEVERITY_COLORS: dict[str, tuple[int, int, int]] = {
    "info": (148, 163, 184),  # slate (BGR)
    "warning": (11, 168, 245),  # amber
    "critical": (68, 68, 239),  # red
    "error": (68, 68, 239),
}
TRACK_COLOR: tuple[int, int, int] = (56, 189, 248)  # sky
FONT = cv2.FONT_HERSHEY_SIMPLEX
#: Preview downscale: the preview is for situational awareness, not evidence.
MAX_PREVIEW_WIDTH = 640


def _hex_to_bgr(value: str, default: tuple[int, int, int]) -> tuple[int, int, int]:
    text = str(value or "").lstrip("#")
    if len(text) != 6:
        return default
    try:
        r, g, b = int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    except ValueError:
        return default
    return (b, g, r)


def draw_zone(image: np.ndarray, zone: ZoneSpec, occupancy: int = 0) -> None:
    height, width = image.shape[:2]
    color = _hex_to_bgr(getattr(zone, "color", "") or "", SEVERITY_COLORS["warning"])
    thickness = 2 if zone.enabled else 1
    polygon = np.array([[int(x * width), int(y * height)] for x, y in zone.points], dtype=np.int32)
    if len(polygon) < 2:
        return
    overlay = image.copy()
    cv2.fillPoly(overlay, [polygon], color)
    cv2.addWeighted(overlay, 0.12, image, 0.88, 0, image)
    cv2.polylines(image, [polygon], True, color, thickness, cv2.LINE_AA)

    label = f"{zone.name} ({occupancy})" if zone.enabled else f"{zone.name} (off)"
    anchor = (int(polygon[0][0]), max(14, int(polygon[0][1]) - 6))
    cv2.putText(image, label, anchor, FONT, 0.45, color, 1, cv2.LINE_AA)


def draw_track(image: np.ndarray, track: Track, in_zone: bool) -> None:
    height, width = image.shape[:2]
    x1, y1, x2, y2 = denormalize_box(track.box, width, height)
    color = SEVERITY_COLORS["warning"] if in_zone else TRACK_COLOR
    cv2.rectangle(image, (x1, y1), (x2, y2), color, 2 if in_zone else 1, cv2.LINE_AA)
    fx, fy = int(track.box.foot_point[0] * width), int(track.box.foot_point[1] * height)
    cv2.circle(image, (fx, fy), 3, color, -1, cv2.LINE_AA)
    text = f"#{track.track_id} {track.confidence:.2f}"
    cv2.putText(image, text, (x1, max(12, y1 - 5)), FONT, 0.4, color, 1, cv2.LINE_AA)


def annotate(
    image: np.ndarray,
    zones: Sequence[ZoneSpec] = (),
    tracks: Sequence[Track] = (),
    *,
    max_width: int = MAX_PREVIEW_WIDTH,
) -> np.ndarray:
    """Draw zones then tracks onto a copy of ``image``."""
    canvas = image.copy()
    height, width = canvas.shape[:2]
    for zone in zones:
        occupancy = sum(
            1
            for track in tracks
            if zone.accepts_class(track.label)
            and point_in_polygon(track.box.foot_point, zone.points)
        )
        draw_zone(canvas, zone, occupancy)
    for track in tracks:
        in_zone = any(
            zone.enabled
            and zone.accepts_class(track.label)
            and point_in_polygon(track.box.foot_point, zone.points)
            for zone in zones
        )
        draw_track(canvas, track, in_zone)
    if max_width and width > max_width:
        ratio = max_width / width
        canvas = cv2.resize(
            canvas, (max_width, max(1, int(height * ratio))), interpolation=cv2.INTER_AREA
        )
    return canvas


def annotate_with_severity(
    image: np.ndarray, zones: Sequence[ZoneSpec], tracks: Sequence[Track]
) -> np.ndarray:
    return annotate(image, zones, tracks)


def severity_color(severity: Severity | str) -> tuple[int, int, int]:
    return SEVERITY_COLORS.get(str(severity), SEVERITY_COLORS["info"])


__all__ = [
    "SEVERITY_COLORS",
    "annotate",
    "annotate_with_severity",
    "draw_track",
    "draw_zone",
    "severity_color",
]
