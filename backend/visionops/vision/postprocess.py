"""Model output → detections: decode, threshold, class filter, NMS, rescale.

NMS is implemented here rather than pulled from a framework so the runtime stays
torch-free (ADR-007) and so tracker-relevant options (class-aware NMS, score
handling) are fully under our control and unit-tested.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from .contracts import BBox, Detection
from .geometry import box_iou, clamp01
from .preprocess import LetterboxTransform

#: A detection is considered "edge touching" if any coordinate is this close to
#: the frame border. Clipped boxes have an unreliable bottom edge, so their
#: foot point must not drive zone decisions.
EDGE_EPSILON = 1e-4


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(x, -60.0, 60.0)))


def yolo_output_to_candidates(
    output: np.ndarray,
    classes: Sequence[str],
    *,
    confidence_threshold: float,
    class_filter: frozenset[str],
    transform: LetterboxTransform,
    frame_seq: int = 0,
) -> list[Detection]:
    """Decode a YOLO-family output tensor into thresholded candidates.

    Handles both common export layouts:

    * ``(1, 4 + C, N)`` — channels-first, the Ultralytics default
    * ``(1, N, 4 + C)`` — detections-first

    ``output[0]`` is therefore transposed to ``(N, 4 + C)`` and the first four
    columns are read as ``(cx, cy, w, h)`` in model-input pixels.
    """
    if output.ndim == 3:
        if output.shape[0] != 1:
            raise ValueError(f"expected batch dimension of 1, got {output.shape[0]}")
        data = output[0]
    elif output.ndim == 2:
        data = output
    else:
        raise ValueError(f"unexpected model output rank {output.ndim}")

    n_attrs = 4 + len(classes)
    if data.shape[0] == n_attrs:
        data = data.T
    elif data.shape[1] != n_attrs:
        raise ValueError(
            f"cannot interpret model output of shape {data.shape}; expected "
            f"{n_attrs} attributes (4 box + {len(classes)} classes)"
        )

    if data.size == 0:
        return []

    boxes_px = data[:, :4]
    class_scores = data[:, 4:]

    class_ids = np.argmax(class_scores, axis=1)
    scores = class_scores[np.arange(class_scores.shape[0]), class_ids]

    keep = scores >= confidence_threshold
    if class_filter:
        # A vectorised lookup avoids a Python loop over thousands of candidates.
        lookup = np.array([label in class_filter for label in classes], dtype=bool)
        keep &= lookup[class_ids]
    if not keep.any():
        return []

    boxes_px = boxes_px[keep]
    scores = scores[keep]
    class_ids = class_ids[keep]

    detections: list[Detection] = []
    for (cx, cy, w, h), score, class_id in zip(boxes_px, scores, class_ids, strict=True):
        x1 = float((cx - w / 2.0 - transform.pad_x) / (transform.scale * transform.frame_width))
        y1 = float((cy - h / 2.0 - transform.pad_y) / (transform.scale * transform.frame_height))
        x2 = float((cx + w / 2.0 - transform.pad_x) / (transform.scale * transform.frame_width))
        y2 = float((cy + h / 2.0 - transform.pad_y) / (transform.scale * transform.frame_height))
        unclamped = (x1, y1, x2, y2)
        x1, y1, x2, y2 = (clamp01(v) for v in unclamped)
        box = BBox(x1, y1, x2, y2)
        if box.is_degenerate:
            continue
        clipped = any(abs(v - 0.0) < EDGE_EPSILON or abs(v - 1.0) < EDGE_EPSILON for v in unclamped)
        clipped = clipped or any(v < -0.005 or v > 1.005 for v in unclamped)
        label = classes[int(class_id)]
        detections.append(
            Detection(
                class_id=int(class_id),
                label=label,
                confidence=float(score),
                box=box,
                frame_seq=frame_seq,
                clip=clipped,
            )
        )
    return detections


def greedy_nms(
    detections: Sequence[Detection],
    iou_threshold: float,
    *,
    class_aware: bool = True,
    max_keep: int = 300,
) -> list[Detection]:
    """Greedy non-maximum suppression.

    Class-aware by default so two different objects of different classes at the
    same place both survive — important in scenes where a "person" and a
    "backpack" overlap. Returns detections sorted by descending confidence.
    """
    if not detections:
        return []
    order = sorted(range(len(detections)), key=lambda i: detections[i].confidence, reverse=True)
    kept: list[Detection] = []
    for idx in order:
        candidate = detections[idx]
        if len(kept) >= max_keep:
            break
        suppressed = False
        for existing in kept:
            if class_aware and existing.class_id != candidate.class_id:
                continue
            if box_iou(existing.box, candidate.box) > iou_threshold:
                suppressed = True
                break
        if not suppressed:
            kept.append(candidate)
    return kept


def apply_sigmoid(scores: np.ndarray) -> np.ndarray:
    """Sigmoid, exposed for models exported with a separate objectness head."""
    return _sigmoid(scores)


__all__ = [
    "EDGE_EPSILON",
    "apply_sigmoid",
    "greedy_nms",
    "yolo_output_to_candidates",
]
