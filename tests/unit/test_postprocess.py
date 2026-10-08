"""Post-processing: decoding, thresholding, class filtering and NMS.

NMS is implemented in-house (ADR-007) so these tests exist to prove we own its
correctness — including the class-aware behaviour that keeps two different
objects at the same place.
"""

from __future__ import annotations

import numpy as np
import pytest

from visionops.vision.contracts import BBox
from visionops.vision.postprocess import EDGE_EPSILON, greedy_nms, yolo_output_to_candidates
from visionops.vision.preprocess import compute_letterbox

CLASSES = ("person", "bicycle", "car")


def output_tensor(rows: list[list[float]] | list[float]) -> np.ndarray:
    """Build a ``(1, 4 + C, N)`` YOLO output from ``[cx, cy, w, h, s0, s1, …]`` rows.

    Accepts a single row as a flat list, or several rows as a list of lists.
    """
    matrix = np.array(
        [rows] if rows and not isinstance(rows[0], (list, tuple)) else rows, dtype=np.float32
    )
    return matrix.T[None, :, :]


def square_rows(cx: float, cy: float, half: float, scores: dict[str, float]) -> list[list[float]]:
    row = [cx, cy, half * 2, half * 2]
    row.extend(scores.get(label, 0.0) for label in CLASSES)
    return row


IDENTITY = compute_letterbox(640, 640, 640, 640)  # square: no padding to undo


class TestDecode:
    def test_decodes_a_single_confident_person(self) -> None:
        tensor = output_tensor([square_rows(320, 320, 50, {"person": 0.9})])
        detections = yolo_output_to_candidates(
            tensor,
            CLASSES,
            confidence_threshold=0.35,
            class_filter=frozenset({"person"}),
            transform=IDENTITY,
        )
        assert len(detections) == 1
        detection = detections[0]
        assert detection.label == "person"
        assert detection.class_id == 0
        assert detection.confidence == pytest.approx(0.9)
        # cx=320, half=50 → box (270, 270) to (370, 370) in a 640 input.
        assert detection.box.x1 == pytest.approx(270 / 640, abs=1e-6)
        assert detection.box.y1 == pytest.approx(270 / 640, abs=1e-6)
        assert detection.box.x2 == pytest.approx(370 / 640, abs=1e-6)
        assert detection.box.y2 == pytest.approx(370 / 640, abs=1e-6)
        assert detection.clip is False

    def test_below_threshold_is_dropped(self) -> None:
        tensor = output_tensor([square_rows(320, 320, 50, {"person": 0.2})])
        detections = yolo_output_to_candidates(
            tensor, CLASSES, confidence_threshold=0.35, class_filter=frozenset(), transform=IDENTITY
        )
        assert detections == []

    def test_class_filter_excludes_other_classes(self) -> None:
        tensor = output_tensor([square_rows(320, 320, 50, {"car": 0.95})])
        detections = yolo_output_to_candidates(
            tensor,
            CLASSES,
            confidence_threshold=0.35,
            class_filter=frozenset({"person"}),
            transform=IDENTITY,
        )
        assert detections == []

    def test_empty_class_filter_keeps_everything(self) -> None:
        tensor = output_tensor([square_rows(320, 320, 50, {"car": 0.95})])
        detections = yolo_output_to_candidates(
            tensor, CLASSES, confidence_threshold=0.35, class_filter=frozenset(), transform=IDENTITY
        )
        assert [d.label for d in detections] == ["car"]

    def test_picks_the_highest_scoring_class(self) -> None:
        tensor = output_tensor([square_rows(320, 320, 50, {"person": 0.4, "bicycle": 0.8})])
        detections = yolo_output_to_candidates(
            tensor, CLASSES, confidence_threshold=0.35, class_filter=frozenset(), transform=IDENTITY
        )
        assert [d.label for d in detections] == ["bicycle"]

    def test_accepts_the_detections_first_layout(self) -> None:
        """Some exports are ``(1, N, 4 + C)``; both layouts must decode alike."""
        rows = np.array([square_rows(320, 320, 50, {"person": 0.9})], dtype=np.float32)
        by_row = yolo_output_to_candidates(
            rows[None, :, :],
            CLASSES,
            confidence_threshold=0.35,
            class_filter=frozenset(),
            transform=IDENTITY,
        )
        by_column = yolo_output_to_candidates(
            output_tensor([square_rows(320, 320, 50, {"person": 0.9})]),
            CLASSES,
            confidence_threshold=0.35,
            class_filter=frozenset(),
            transform=IDENTITY,
        )
        assert len(by_row) == len(by_column) == 1
        assert by_row[0].box.as_list() == pytest.approx(by_column[0].box.as_list(), abs=1e-6)

    def test_undoes_the_letterbox_padding(self) -> None:
        """A 1920x1080 frame letterboxed into 640x640 must place the box correctly."""
        transform = compute_letterbox(1920, 1080, 640, 640)
        # Content occupies y 140..500 of the 640-tall input, so a box on the
        # content centre is the *frame* centre, and one at the top of the content
        # is the top of the frame — not of the padded input.
        centred = yolo_output_to_candidates(
            output_tensor([square_rows(320, 320, 20, {"person": 0.9})]),
            CLASSES,
            confidence_threshold=0.35,
            class_filter=frozenset(),
            transform=transform,
        )[0]
        # The box centre is the frame centre; its edges straddle it.
        assert (centred.box.y1 + centred.box.y2) / 2 == pytest.approx(0.5, abs=1e-3)
        assert centred.box.y1 == pytest.approx(0.4444, abs=1e-3)
        assert centred.box.y2 == pytest.approx(0.5556, abs=1e-3)

        top = yolo_output_to_candidates(
            output_tensor([square_rows(320, 160, 20, {"person": 0.9})]),
            CLASSES,
            confidence_threshold=0.35,
            class_filter=frozenset(),
            transform=transform,
        )[0]
        assert top.box.y1 == pytest.approx(0.0, abs=1e-6)
        assert top.box.y2 == pytest.approx(0.111, abs=1e-3)

        # Without the inverse transform this would sit at 25 % of the input
        # height, i.e. clearly wrong — which is exactly what this test guards.
        assert top.box.y1 != pytest.approx(140 / 640, abs=1e-3)

    def test_flags_edge_clipped_detections(self) -> None:
        """A box running off the frame has an unreliable foot point."""
        tensor = output_tensor([square_rows(-40, 320, 60, {"person": 0.9})])
        detections = yolo_output_to_candidates(
            tensor, CLASSES, confidence_threshold=0.35, class_filter=frozenset(), transform=IDENTITY
        )
        assert detections[0].clip is True
        assert detections[0].box.x1 == 0.0

    def test_touching_the_border_counts_as_clipped(self) -> None:
        tensor = output_tensor([square_rows(30, 320, 40, {"person": 0.9})])
        detections = yolo_output_to_candidates(
            tensor, CLASSES, confidence_threshold=0.35, class_filter=frozenset(), transform=IDENTITY
        )
        assert detections[0].clip is True

    def test_clamps_out_of_frame_coordinates(self) -> None:
        tensor = output_tensor([square_rows(700, 320, 100, {"person": 0.9})])
        detections = yolo_output_to_candidates(
            tensor, CLASSES, confidence_threshold=0.35, class_filter=frozenset(), transform=IDENTITY
        )
        assert 0.0 <= detections[0].box.x1 <= 1.0
        assert 0.0 <= detections[0].box.x2 <= 1.0

    def test_rejects_an_uninterpretable_output_shape(self) -> None:
        tensor = np.zeros((1, 11, 5), dtype=np.float32)  # neither 7 nor 5 attributes
        with pytest.raises(ValueError, match="cannot interpret"):
            yolo_output_to_candidates(
                tensor,
                CLASSES,
                confidence_threshold=0.35,
                class_filter=frozenset(),
                transform=IDENTITY,
            )

    def test_handles_a_completely_empty_output(self) -> None:
        tensor = np.zeros((1, 7, 0), dtype=np.float32)
        assert (
            yolo_output_to_candidates(
                tensor,
                CLASSES,
                confidence_threshold=0.35,
                class_filter=frozenset(),
                transform=IDENTITY,
            )
            == []
        )

    def test_uses_the_model_class_map_for_fine_tuned_classes(self) -> None:
        """Class names come from the model, so a fine-tuned model just works."""
        rows = [320.0, 320.0, 100.0, 100.0, 0.0, 0.0, 0.0, 0.9, 0.0]  # 4 + 5 scores
        fine_tuned = (*CLASSES, "forklift", "hi_visor")
        detections = yolo_output_to_candidates(
            output_tensor(rows),
            fine_tuned,
            confidence_threshold=0.35,
            class_filter=frozenset(),
            transform=IDENTITY,
        )
        assert detections[0].label == "forklift"

    @pytest.mark.parametrize("named", [CLASSES[:2], CLASSES, (*CLASSES, "forklift")])
    def test_class_count_mismatch_is_an_error_not_a_mislabel(self, named: tuple[str, ...]) -> None:
        """A class map that disagrees with the model must fail loudly.

        Guessing a label would silently attach the wrong name to every event, so
        the decoder refuses instead of inventing one.
        """
        rows = [320.0, 320.0, 100.0, 100.0, 0.0, 0.0, 0.0, 0.9, 0.0]  # 4 + 5 scores
        with pytest.raises(ValueError, match="cannot interpret"):
            yolo_output_to_candidates(
                output_tensor(rows),
                named,
                confidence_threshold=0.35,
                class_filter=frozenset(),
                transform=IDENTITY,
            )

    def test_records_the_frame_sequence(self) -> None:
        tensor = output_tensor([square_rows(320, 320, 50, {"person": 0.9})])
        detections = yolo_output_to_candidates(
            tensor,
            CLASSES,
            confidence_threshold=0.35,
            class_filter=frozenset(),
            transform=IDENTITY,
            frame_seq=42,
        )
        assert detections[0].frame_seq == 42


def detection(label: str, box: BBox, confidence: float = 0.9) -> object:
    from visionops.vision.contracts import Detection

    class_id = CLASSES.index(label) if label in CLASSES else 0
    return Detection(class_id=class_id, label=label, confidence=confidence, box=box)


class TestNms:
    def test_removes_a_near_duplicate(self) -> None:
        keep = detection("person", BBox(0.10, 0.10, 0.30, 0.60))
        duplicate = detection("person", BBox(0.11, 0.11, 0.31, 0.61))
        assert len(greedy_nms([keep, duplicate], 0.45)) == 1

    def test_keeps_distant_objects(self) -> None:
        left = detection("person", BBox(0.00, 0.10, 0.20, 0.60))
        right = detection("person", BBox(0.70, 0.10, 0.90, 0.60))
        assert len(greedy_nms([left, right], 0.45)) == 2

    def test_keeps_the_highest_scoring_of_a_cluster(self) -> None:
        low = detection("person", BBox(0.10, 0.10, 0.30, 0.60), confidence=0.4)
        high = detection("person", BBox(0.11, 0.10, 0.31, 0.60), confidence=0.9)
        kept = greedy_nms([low, high], 0.45)
        assert len(kept) == 1
        assert kept[0].confidence == pytest.approx(0.9)

    def test_is_class_aware_by_default(self) -> None:
        """A person and a bicycle at the same place are two objects."""
        person = detection("person", BBox(0.10, 0.10, 0.30, 0.60))
        bicycle = detection("bicycle", BBox(0.10, 0.10, 0.30, 0.60))
        assert len(greedy_nms([person, bicycle], 0.45)) == 2

    def test_class_agnostic_mode_merges_them(self) -> None:
        person = detection("person", BBox(0.10, 0.10, 0.30, 0.60))
        bicycle = detection("bicycle", BBox(0.10, 0.10, 0.30, 0.60))
        assert len(greedy_nms([person, bicycle], 0.45, class_aware=False)) == 1

    def test_matches_a_hand_computed_iou_threshold(self) -> None:
        # Two 0.2-wide boxes offset by 0.1 have IoU = (0.1 / 0.3) ≈ 0.333.
        a = detection("person", BBox(0.10, 0.10, 0.30, 0.30))
        b = detection("person", BBox(0.20, 0.10, 0.40, 0.30))
        assert len(greedy_nms([a, b], 0.30)) == 1  # 0.333 > 0.30 → suppress
        assert len(greedy_nms([a, b], 0.35)) == 2  # 0.333 ≤ 0.35 → keep both

    def test_empty_input(self) -> None:
        assert greedy_nms([], 0.45) == []

    def test_respects_max_keep(self) -> None:
        boxes = [detection("person", BBox(i * 0.5, 0.1, i * 0.5 + 0.2, 0.6)) for i in range(10)]
        assert len(greedy_nms(boxes, 0.45, max_keep=3)) == 3

    def test_output_is_sorted_by_confidence(self) -> None:
        items = [
            detection("person", BBox(0.0, 0.0, 0.1, 0.1), confidence=0.5),
            detection("person", BBox(0.5, 0.5, 0.6, 0.6), confidence=0.95),
            detection("person", BBox(0.2, 0.2, 0.3, 0.3), confidence=0.7),
        ]
        assert [d.confidence for d in greedy_nms(items, 0.45)] == [0.95, 0.7, 0.5]


class TestEdgeEpsilon:
    def test_epsilon_is_a_small_positive_number(self) -> None:
        assert 0 < EDGE_EPSILON < 1e-3
