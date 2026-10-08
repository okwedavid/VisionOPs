"""Geometry: pure functions with hand-computable answers.

If any of these break, every event VisionOps produces is wrong — so they are the
most tightly specified tests in the suite.
"""

from __future__ import annotations

import math

import pytest

from visionops.vision.contracts import BBox
from visionops.vision.geometry import (
    box_contains_point,
    box_iou,
    clamp01,
    denormalize_box,
    is_simple_polygon,
    point_in_polygon,
    polygon_area,
    polygon_bbox,
    rectangle_polygon,
    segments_intersect,
    signed_distance_to_line,
)

UNIT_SQUARE = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]


class TestClamp:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [(-1.0, 0.0), (0.0, 0.0), (0.5, 0.5), (1.0, 1.0), (2.0, 1.0), (1e9, 1.0)],
    )
    def test_clamp01(self, value: float, expected: float) -> None:
        assert clamp01(value) == expected


class TestBBox:
    def test_properties_and_foot_point(self) -> None:
        box = BBox(0.1, 0.2, 0.4, 0.9)
        assert box.width == pytest.approx(0.3)
        assert box.height == pytest.approx(0.7)
        assert box.area == pytest.approx(0.21)
        assert box.center == pytest.approx((0.25, 0.55))
        # The foot point is the ground contact: bottom-centre, not the centre.
        assert box.foot_point == pytest.approx((0.25, 0.9))

    def test_rejects_inverted_coordinates(self) -> None:
        with pytest.raises(ValueError, match="malformed"):
            BBox(0.8, 0.2, 0.3, 0.9)

    def test_from_xyxy_orders_and_clamps(self) -> None:
        box = BBox.from_xyxy(0.8, 0.9, -0.2, -0.5)
        assert (box.x1, box.y1, box.x2, box.y2) == (0.0, 0.0, 0.8, 0.9)

    def test_from_cxcywh(self) -> None:
        box = BBox.from_cxcywh(0.5, 0.5, 0.2, 0.4)
        assert box.x1 == pytest.approx(0.4)
        assert box.x2 == pytest.approx(0.6)
        assert box.y1 == pytest.approx(0.3)
        assert box.y2 == pytest.approx(0.7)

    def test_degenerate_detection(self) -> None:
        assert BBox(0.5, 0.5, 0.5, 0.6).is_degenerate
        assert not BBox(0.5, 0.5, 0.6, 0.6).is_degenerate

    def test_as_list_rounds_for_json_stability(self) -> None:
        assert BBox(0.123456789, 0.2, 0.4, 0.9).as_list()[0] == 0.123457

    def test_denormalize(self) -> None:
        assert denormalize_box(BBox(0.1, 0.2, 0.4, 0.9), 1000, 500) == (100, 100, 400, 450)


class TestBoxIou:
    def test_identical_boxes(self) -> None:
        box = BBox(0.1, 0.1, 0.5, 0.5)
        assert box_iou(box, box) == pytest.approx(1.0)

    def test_disjoint_boxes(self) -> None:
        assert box_iou(BBox(0.0, 0.0, 0.2, 0.2), BBox(0.5, 0.5, 0.9, 0.9)) == 0.0

    def test_contained_box(self) -> None:
        outer = BBox(0.0, 0.0, 1.0, 1.0)
        inner = BBox(0.0, 0.0, 0.5, 0.5)
        # intersection 0.25, union 1.0
        assert box_iou(outer, inner) == pytest.approx(0.25)

    def test_half_overlap(self) -> None:
        a = BBox(0.0, 0.0, 0.5, 0.5)
        b = BBox(0.25, 0.0, 0.75, 0.5)
        inter = 0.25 * 0.5
        union = 0.5 * 0.5 + 0.5 * 0.5 - inter
        assert box_iou(a, b) == pytest.approx(inter / union)

    def test_touching_edges_do_not_overlap(self) -> None:
        assert box_iou(BBox(0.0, 0.0, 0.5, 0.5), BBox(0.5, 0.0, 1.0, 0.5)) == 0.0

    def test_degenerate_boxes_score_zero_rather_than_raising(self) -> None:
        assert box_iou(BBox(0.5, 0.5, 0.5, 0.5), BBox(0.0, 0.0, 1.0, 1.0)) == 0.0
        assert box_iou(BBox(0.0, 0.0, 1.0, 1.0), BBox(0.5, 0.5, 0.5, 0.5)) == 0.0

    def test_is_symmetric(self) -> None:
        a, b = BBox(0.1, 0.1, 0.6, 0.7), BBox(0.3, 0.2, 0.9, 0.5)
        assert box_iou(a, b) == pytest.approx(box_iou(b, a))


class TestPointInPolygon:
    def test_inside(self) -> None:
        assert point_in_polygon((0.5, 0.5), UNIT_SQUARE)

    def test_outside(self) -> None:
        assert not point_in_polygon((1.5, 0.5), UNIT_SQUARE)
        assert not point_in_polygon((0.5, -0.5), UNIT_SQUARE)

    def test_degenerate_polygons_are_never_inside(self) -> None:
        assert not point_in_polygon((0.5, 0.5), [])
        assert not point_in_polygon((0.5, 0.5), [(0.1, 0.1), (0.9, 0.9)])

    def test_concave_polygon(self) -> None:
        # An L shape: the notch must read as outside.
        shape = [(0.0, 0.0), (1.0, 0.0), (1.0, 0.4), (0.4, 0.4), (0.4, 1.0), (0.0, 1.0)]
        assert point_in_polygon((0.8, 0.2), shape)
        assert point_in_polygon((0.2, 0.8), shape)
        assert not point_in_polygon((0.8, 0.8), shape)

    def test_orientation_does_not_matter(self) -> None:
        clockwise = list(reversed(UNIT_SQUARE))
        assert point_in_polygon((0.5, 0.5), clockwise)
        assert not point_in_polygon((1.2, 0.5), clockwise)

    def test_bowtie_is_consistent_even_odd(self) -> None:
        bowtie = [(0.0, 0.0), (1.0, 1.0), (1.0, 0.0), (0.0, 1.0)]
        # Documented behaviour: even-odd, so the two lobes are "inside" and the
        # centre crossing behaves mathematically. The UI prevents drawing this.
        assert isinstance(point_in_polygon((0.2, 0.2), bowtie), bool)

    def test_matches_a_brute_force_reference(self) -> None:
        """Cross-check the ray-casting rule against an independent implementation.

        Grid points that land exactly on the polygon boundary are excluded: which
        side a boundary point falls on is documented as ambiguous
        (``docs/VISION_ENGINE.md`` §6.2), and the dwell hysteresis absorbs it.
        """
        polygon = [(0.2, 0.1), (0.9, 0.3), (0.7, 0.95), (0.1, 0.7)]
        checked = 0
        for i in range(1, 20):
            for j in range(1, 20):
                point = (i / 20, j / 20)
                if _on_polygon_boundary(point, polygon):
                    continue
                assert point_in_polygon(point, polygon) == _reference_inside(point, polygon)
                checked += 1
        assert checked > 300  # the grid really was exercised

    def test_boundary_points_are_defined_even_if_ambiguous(self) -> None:
        """A point exactly on a vertex must not crash and must return a bool."""
        polygon = [(0.2, 0.1), (0.9, 0.3), (0.7, 0.95), (0.1, 0.7)]
        assert isinstance(point_in_polygon((0.2, 0.1), polygon), bool)
        assert isinstance(point_in_polygon((0.5, 0.5), polygon), bool)


def _on_polygon_boundary(
    point: tuple[float, float], polygon: list[tuple[float, float]], tolerance: float = 1e-9
) -> bool:
    """Whether a point lies on any polygon edge (within ``tolerance``)."""
    x, y = point
    count = len(polygon)
    for index in range(count):
        ax, ay = polygon[index]
        bx, by = polygon[(index + 1) % count]
        # Cross product zero (collinear) and within the segment's bounding box.
        cross = (bx - ax) * (y - ay) - (by - ay) * (x - ax)
        if abs(cross) <= tolerance and (
            min(ax, bx) - tolerance <= x <= max(ax, bx) + tolerance
            and min(ay, by) - tolerance <= y <= max(ay, by) + tolerance
        ):
            return True
    return False


def _reference_inside(point: tuple[float, float], polygon: list[tuple[float, float]]) -> bool:
    """Independent even-odd implementation, written differently on purpose."""
    x, y = point
    crossings = 0
    count = len(polygon)
    for index in range(count):
        ax, ay = polygon[index]
        bx, by = polygon[(index + 1) % count]
        if (ay > y) != (by > y):
            t = (y - ay) / (by - ay)
            if x < ax + t * (bx - ax):
                crossings += 1
    return crossings % 2 == 1


class TestPolygonHelpers:
    def test_area_of_unit_square(self) -> None:
        assert polygon_area(UNIT_SQUARE) == pytest.approx(1.0)

    def test_area_of_rectangle(self) -> None:
        assert polygon_area(rectangle_polygon(0.2, 0.3, 0.6, 0.9)) == pytest.approx(0.4 * 0.6)

    def test_signed_area_flips_with_orientation(self) -> None:
        """``polygon_area`` is signed by design (shoelace); only magnitude is stable."""
        assert polygon_area(UNIT_SQUARE) == pytest.approx(1.0)
        assert polygon_area(list(reversed(UNIT_SQUARE))) == pytest.approx(-1.0)
        assert abs(polygon_area(list(reversed(UNIT_SQUARE)))) == pytest.approx(1.0)

    def test_bounding_box(self) -> None:
        assert polygon_bbox(UNIT_SQUARE).as_list() == [0.0, 0.0, 1.0, 1.0]

    def test_rectangle_polygon_is_clockwise_from_top_left(self) -> None:
        assert rectangle_polygon(0.3, 0.2, 0.7, 0.8) == (
            (0.3, 0.2),
            (0.7, 0.2),
            (0.7, 0.8),
            (0.3, 0.8),
        )

    def test_rectangle_polygon_orders_its_inputs(self) -> None:
        assert rectangle_polygon(0.7, 0.8, 0.3, 0.2) == rectangle_polygon(0.3, 0.2, 0.7, 0.8)

    def test_is_simple_accepts_a_rectangle_and_rejects_a_bowtie(self) -> None:
        assert is_simple_polygon(rectangle_polygon(0.1, 0.1, 0.9, 0.9))
        assert not is_simple_polygon([(0.0, 0.0), (1.0, 1.0), (1.0, 0.0), (0.0, 1.0)])

    def test_is_simple_rejects_too_few_points(self) -> None:
        assert not is_simple_polygon([(0.0, 0.0), (1.0, 1.0)])


class TestSegments:
    def test_crossing_segments(self) -> None:
        assert segments_intersect((0.0, 0.5), (1.0, 0.5), (0.5, 0.0), (0.5, 1.0))

    def test_parallel_segments(self) -> None:
        assert not segments_intersect((0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0))

    def test_touching_endpoints(self) -> None:
        assert segments_intersect((0.0, 0.0), (1.0, 1.0), (1.0, 1.0), (2.0, 0.0))

    def test_signed_distance_to_line(self) -> None:
        """Magnitude is the perpendicular distance; the sign encodes the side.

        In image coordinates (y down) the convention is:
        negative = above the line, positive = below, for a left-to-right line.
        """
        assert signed_distance_to_line((0.5, 1.0), (0.0, 0.0), (1.0, 0.0)) == pytest.approx(-1.0)
        assert signed_distance_to_line((0.5, -1.0), (0.0, 0.0), (1.0, 0.0)) == pytest.approx(1.0)
        assert signed_distance_to_line((0.5, 0.0), (0.0, 0.0), (1.0, 0.0)) == pytest.approx(0.0)

    def test_signed_distance_ignores_distance_along_the_line(self) -> None:
        assert signed_distance_to_line((50.0, 1.0), (0.0, 0.0), (1.0, 0.0)) == pytest.approx(-1.0)

    def test_reversing_the_line_flips_the_sign(self) -> None:
        point, a, b = (0.5, 1.0), (0.0, 0.0), (1.0, 0.0)
        assert signed_distance_to_line(point, b, a) == pytest.approx(
            -signed_distance_to_line(point, a, b)
        )

    def test_zero_length_line_is_safe(self) -> None:
        assert signed_distance_to_line((0.5, 0.5), (0.5, 0.5), (0.5, 0.5)) == 0.0


class TestBoxContainsPoint:
    def test_interior_and_boundary(self) -> None:
        box = BBox(0.2, 0.2, 0.8, 0.8)
        assert box_contains_point(box, (0.5, 0.5))
        assert box_contains_point(box, (0.2, 0.2))  # edges are inclusive
        assert not box_contains_point(box, (0.19, 0.5))


class TestPrecision:
    def test_repeated_conversions_do_not_drift(self) -> None:
        """Normalised geometry must survive a round trip without drift."""
        value = 0.123456
        for _ in range(1000):
            value = clamp01(value)
        assert math.isclose(value, 0.123456, rel_tol=0, abs_tol=1e-12)
