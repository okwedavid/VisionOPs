"""Normalised-coordinate geometry. Pure functions, no I/O, fully testable.

Coordinates are floats in ``[0, 1]`` with the origin at the top-left of the
source frame (ADR-019). Nothing in this module knows about pixel sizes.
"""

from __future__ import annotations

from collections.abc import Sequence

from .contracts import BBox

Point = tuple[float, float]
Polygon = Sequence[Point]

#: Boxes smaller than this (in normalised area) are treated as degenerate.
_EPS = 1e-9


def clamp01(value: float) -> float:
    """Clamp a scalar into ``[0, 1]``."""
    return 0.0 if value < 0.0 else (1.0 if value > 1.0 else value)


def box_iou(a: BBox, b: BBox) -> float:
    """Intersection-over-union of two normalised boxes.

    Returns ``0.0`` for degenerate inputs rather than raising, because a zero
    score simply means "no association".
    """
    if a.is_degenerate or b.is_degenerate:
        return 0.0
    inter_w = min(a.x2, b.x2) - max(a.x1, b.x1)
    inter_h = min(a.y2, b.y2) - max(a.y1, b.y1)
    if inter_w <= 0.0 or inter_h <= 0.0:
        return 0.0
    inter = inter_w * inter_h
    union = a.area + b.area - inter
    if union <= _EPS:
        return 0.0
    return inter / union


def box_contains_point(box: BBox, point: Point) -> bool:
    """Whether a normalised point lies inside an axis-aligned box (edges inclusive)."""
    x, y = point
    return box.x1 <= x <= box.x2 and box.y1 <= y <= box.y2


def point_in_polygon(point: Point, polygon: Polygon) -> bool:
    """Even-odd (ray-casting) point-in-polygon test.

    A point exactly on an edge may test either way; the ambiguity is one pixel
    wide in practice and the dwell hysteresis absorbs it. Self-intersecting
    polygons produce the mathematically expected (but possibly surprising)
    even-odd result, which is why the UI prevents drawing them.
    """
    if len(polygon) < 3:
        return False
    x, y = point
    inside = False
    n = len(polygon)
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if (yi > y) != (yj > y):
            x_cross = xi + (y - yi) * (xj - xi) / (yj - yi)
            if x < x_cross:
                inside = not inside
        j = i
    return inside


def polygon_area(polygon: Polygon) -> float:
    """Signed area via the shoelace formula (positive for counter-clockwise)."""
    n = len(polygon)
    if n < 3:
        return 0.0
    total = 0.0
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        total += x1 * y2 - x2 * y1
    return total / 2.0


def is_simple_polygon(polygon: Polygon) -> bool:
    """Whether a polygon has no non-adjacent self-intersections."""
    n = len(polygon)
    if n < 3:
        return False
    for i in range(n):
        a1, a2 = polygon[i], polygon[(i + 1) % n]
        for j in range(i + 1, n):
            if j == i or (j + 1) % n == i or j == (i + 1) % n:
                continue
            b1, b2 = polygon[j], polygon[(j + 1) % n]
            if _segments_intersect(a1, a2, b1, b2):
                return False
    return True


def _orient(p: Point, q: Point, r: Point) -> float:
    val = (q[1] - p[1]) * (r[0] - q[0]) - (q[0] - p[0]) * (r[1] - q[1])
    if abs(val) < 1e-12:
        return 0.0
    return 1.0 if val > 0 else -1.0


def _on_segment(p: Point, q: Point, r: Point) -> bool:
    """Whether collinear point ``q`` lies on segment ``p``-``r``."""
    return (
        min(p[0], r[0]) - 1e-12 <= q[0] <= max(p[0], r[0]) + 1e-12
        and min(p[1], r[1]) - 1e-12 <= q[1] <= max(p[1], r[1]) + 1e-12
    )


def segments_intersect(p1: Point, q1: Point, p2: Point, q2: Point) -> bool:
    """Proper or improper intersection test for two closed segments."""
    o1 = _orient(p1, q1, p2)
    o2 = _orient(p1, q1, q2)
    o3 = _orient(p2, q2, p1)
    o4 = _orient(p2, q2, q1)
    if o1 != o2 and o3 != o4:
        return True
    if o1 == 0.0 and _on_segment(p1, p2, q1):
        return True
    if o2 == 0.0 and _on_segment(p1, q2, q1):
        return True
    if o3 == 0.0 and _on_segment(p2, p1, q2):
        return True
    # o4 == 0 puts p2 on the segment q1..q2.
    return o4 == 0.0 and _on_segment(p2, q1, q2)


def _segments_intersect(p1: Point, q1: Point, p2: Point, q2: Point) -> bool:
    return segments_intersect(p1, q1, p2, q2)


def signed_distance_to_line(point: Point, line_start: Point, line_end: Point) -> float:
    """Signed perpendicular distance from ``point`` to the infinite line.

    The sign tells which side the point is on, which is what a directional
    line-crossing test needs (``NEXT`` phase). Returns ``0.0`` for a
    zero-length line.
    """
    dx = line_end[0] - line_start[0]
    dy = line_end[1] - line_start[1]
    length = (dx * dx + dy * dy) ** 0.5
    if length < 1e-12:
        return 0.0
    return ((point[0] - line_start[0]) * dy - (point[1] - line_start[1]) * dx) / length


def rectangle_polygon(x1: float, y1: float, x2: float, y2: float) -> tuple[Point, ...]:
    """A normalised rectangle as a four-point polygon (clockwise, top-left first)."""
    left, right = sorted((clamp01(x1), clamp01(x2)))
    top, bottom = sorted((clamp01(y1), clamp01(y2)))
    return ((left, top), (right, top), (right, bottom), (left, bottom))


def polygon_bbox(polygon: Polygon) -> BBox:
    """Axis-aligned bounds of a polygon."""
    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    return BBox.from_xyxy(min(xs), min(ys), max(xs), max(ys))


def denormalize_box(box: BBox, width: int, height: int) -> tuple[int, int, int, int]:
    """Convert a normalised box to integer pixel coordinates for rendering."""
    return (
        int(box.x1 * width),
        int(box.y1 * height),
        int(box.x2 * width),
        int(box.y2 * height),
    )


__all__ = [
    "Point",
    "Polygon",
    "box_contains_point",
    "box_iou",
    "clamp01",
    "denormalize_box",
    "is_simple_polygon",
    "point_in_polygon",
    "polygon_area",
    "polygon_bbox",
    "rectangle_polygon",
    "segments_intersect",
    "signed_distance_to_line",
]
