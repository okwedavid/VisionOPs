"""Frame preprocessing: letterbox resize, normalisation, tensor packing.

The letterbox transform is exact and invertible, which is what lets detections
come back in normalised frame coordinates (ADR-019) with no distortion.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from .contracts import Frame

#: YOLO export convention. Not black, so the model does not see a hard edge.
PAD_VALUE: tuple[int, int, int] = (114, 114, 114)


@dataclass(frozen=True, slots=True)
class LetterboxTransform:
    """Invertible mapping between frame pixels and model-input pixels."""

    scale: float
    pad_x: float
    pad_y: float
    input_width: int
    input_height: int
    frame_width: int
    frame_height: int

    def to_frame_x(self, x: float) -> float:
        return (x - self.pad_x) / (self.scale * self.frame_width)

    def to_frame_y(self, y: float) -> float:
        return (y - self.pad_y) / (self.scale * self.frame_height)

    def to_normalized(self, x: float, y: float) -> tuple[float, float]:
        """Model-input pixel → normalised frame coordinate."""
        return (self.to_frame_x(x), self.to_frame_y(y))


def compute_letterbox(
    frame_width: int,
    frame_height: int,
    input_width: int,
    input_height: int,
) -> LetterboxTransform:
    """Letterbox geometry that preserves aspect ratio (scale + symmetric pad)."""
    if frame_width <= 0 or frame_height <= 0:
        raise ValueError("frame dimensions must be positive")
    if input_width <= 0 or input_height <= 0:
        raise ValueError("input dimensions must be positive")
    scale = min(input_width / frame_width, input_height / frame_height)
    pad_x = (input_width - frame_width * scale) / 2.0
    pad_y = (input_height - frame_height * scale) / 2.0
    return LetterboxTransform(
        scale=scale,
        pad_x=pad_x,
        pad_y=pad_y,
        input_width=input_width,
        input_height=input_height,
        frame_width=frame_width,
        frame_height=frame_height,
    )


def letterbox(
    image: np.ndarray,
    input_width: int,
    input_height: int,
    *,
    use_area: bool = False,
    out: np.ndarray | None = None,
) -> tuple[np.ndarray, LetterboxTransform]:
    """Resize ``image`` into a fixed model input, preserving aspect ratio.

    ``use_area`` selects ``INTER_AREA`` for large downscales (better quality for
    wide-angle footage at a small input size). ``out`` reuses a pre-allocated
    buffer to avoid per-frame allocation in the hot path.
    """
    if image.size == 0:
        raise ValueError("cannot letterbox an empty image")
    frame_h, frame_w = image.shape[:2]
    transform = compute_letterbox(frame_w, frame_h, input_width, input_height)
    new_w = max(1, round(frame_w * transform.scale))
    new_h = max(1, round(frame_h * transform.scale))

    interpolation = cv2.INTER_AREA if use_area and transform.scale < 0.5 else cv2.INTER_LINEAR

    resized = cv2.resize(image, (new_w, new_h), interpolation=interpolation)

    if out is None:
        out = np.full((input_height, input_width, 3), PAD_VALUE, dtype=np.uint8)
    else:
        if out.shape != (input_height, input_width, 3):
            raise ValueError("pre-allocated buffer has the wrong shape")
        out[...] = PAD_VALUE
    top = round(transform.pad_y)
    left = round(transform.pad_x)
    out[top : top + new_h, left : left + new_w] = resized
    return out, transform


def pack_batch(resized: np.ndarray, out: np.ndarray | None = None) -> np.ndarray:
    """Convert a resized BGR uint8 image into a normalised NCHW float32 batch.

    Matches the YOLO export contract: BGR → RGB, ``/255`` scaling, no
    mean/std subtraction, NCHW with a batch dimension of 1.
    """
    if resized.dtype != np.uint8:
        raise ValueError("pack_batch expects uint8 input")
    h, w = resized.shape[:2]
    rgb = resized[:, :, ::-1]  # BGR -> RGB (a view)
    transposed = np.ascontiguousarray(rgb.transpose(2, 0, 1), dtype=np.float32)
    if out is not None and out.shape == (1, 3, h, w):
        np.divide(transposed, 255.0, out=out[0])
        return out
    scaled = transposed / 255.0
    return scaled[None, ...]


def preprocess_frame(
    frame: Frame,
    input_width: int,
    input_height: int,
    *,
    use_area: bool = False,
    buffer: np.ndarray | None = None,
    batch: np.ndarray | None = None,
) -> tuple[np.ndarray, LetterboxTransform]:
    """Convenience: ``Frame`` → model input tensor plus its inverse transform."""
    resized, transform = letterbox(
        frame.image,
        input_width,
        input_height,
        use_area=use_area,
        out=buffer,
    )
    tensor = pack_batch(resized, out=batch)
    return tensor, transform


__all__ = [
    "PAD_VALUE",
    "LetterboxTransform",
    "compute_letterbox",
    "letterbox",
    "pack_batch",
    "preprocess_frame",
]
