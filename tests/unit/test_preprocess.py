"""Preprocessing: the letterbox transform must be exactly invertible.

If the inverse is wrong, every detection lands in the wrong place and every zone
decision is wrong — so this is asserted to 1e-6, not to a tolerance.
"""

from __future__ import annotations

import numpy as np
import pytest

from visionops.vision.contracts import Frame
from visionops.vision.preprocess import (
    PAD_VALUE,
    LetterboxTransform,
    compute_letterbox,
    letterbox,
    pack_batch,
    preprocess_frame,
)


def frame(width: int = 640, height: int = 360, seq: int = 0) -> Frame:
    image = np.random.default_rng(3).integers(0, 255, (height, width, 3), dtype=np.uint8)
    return Frame(image=image, seq=seq, timestamp=0.0, width=width, height=height)


class TestComputeLetterbox:
    def test_wide_frame_pads_vertically(self) -> None:
        transform = compute_letterbox(1920, 1080, 640, 640)
        assert transform.scale == pytest.approx(1 / 3)
        assert transform.pad_x == pytest.approx(0.0)
        # 1080 * (1/3) = 360 of content, so (640 - 360) / 2 = 140 of padding.
        assert transform.pad_y == pytest.approx(140.0)

    def test_tall_frame_pads_horizontally(self) -> None:
        transform = compute_letterbox(480, 640, 640, 640)
        assert transform.scale == pytest.approx(1.0)
        assert transform.pad_y == pytest.approx(0.0)
        assert transform.pad_x == pytest.approx(80.0)

    def test_square_frame_has_no_padding(self) -> None:
        transform = compute_letterbox(640, 640, 640, 640)
        assert transform.scale == pytest.approx(1.0)
        assert transform.pad_x == 0.0
        assert transform.pad_y == 0.0

    def test_aspect_ratio_is_preserved(self) -> None:
        transform = compute_letterbox(1920, 1080, 640, 640)
        scaled_width = 1920 * transform.scale
        scaled_height = 1080 * transform.scale
        assert scaled_width / scaled_height == pytest.approx(1920 / 1080)

    def test_rejects_non_positive_dimensions(self) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            compute_letterbox(0, 100, 640, 640)
        with pytest.raises(ValueError, match="must be positive"):
            compute_letterbox(100, 100, 640, 0)


class TestLetterbox:
    def test_output_shape_and_padding(self) -> None:
        image = np.zeros((1080, 1920, 3), dtype=np.uint8)
        resized, transform = letterbox(image, 640, 640)
        assert resized.shape == (640, 640, 3)
        assert tuple(resized[0, 0]) == PAD_VALUE
        # The centre holds the (black) content, the corners hold the pad value.
        assert tuple(resized[320, 320]) == (0, 0, 0)
        assert transform.pad_x == pytest.approx(0.0)

    def test_content_is_centred(self) -> None:
        image = np.zeros((360, 640, 3), dtype=np.uint8)
        image[100:200, 200:300] = 255
        resized, _transform = letterbox(image, 640, 640)
        # The content band must be horizontally centred, not stretched.
        # The padding value is 114 and the content here is 255, so look for >200.
        rows = np.where(resized[:, :, 0].max(axis=1) > 200)[0]
        assert rows.min() > 200 and rows.max() < 440

    def test_reuses_a_preallocated_buffer(self) -> None:
        image = np.zeros((1080, 1920, 3), dtype=np.uint8)
        buffer = np.full((640, 640, 3), 7, dtype=np.uint8)
        resized, _ = letterbox(image, 640, 640, out=buffer)
        assert resized is buffer
        # The buffer must be fully reset between frames, not blended with the
        # previous frame's padding value.
        letterbox(image, 640, 640, out=buffer)
        assert tuple(buffer[0, 0]) == PAD_VALUE

    def test_rejects_a_mismatched_buffer(self) -> None:
        image = np.zeros((100, 100, 3), dtype=np.uint8)
        with pytest.raises(ValueError, match="wrong shape"):
            letterbox(image, 640, 640, out=np.zeros((100, 100, 3), dtype=np.uint8))

    def test_rejects_an_empty_image(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            letterbox(np.zeros((0, 0, 3), dtype=np.uint8), 640, 640)

    def test_inter_area_path_runs(self) -> None:
        image = np.zeros((1080, 1920, 3), dtype=np.uint8)
        resized, _ = letterbox(image, 640, 640, use_area=True)
        assert resized.shape == (640, 640, 3)


class TestInverseTransform:
    @pytest.mark.parametrize(
        ("frame_w", "frame_h"),
        [(1920, 1080), (640, 360), (480, 640), (1280, 720), (333, 999)],
    )
    def test_round_trip_is_exact(self, frame_w: int, frame_h: int) -> None:
        """A model-input pixel must map back to the same input pixel."""
        transform = compute_letterbox(frame_w, frame_h, 640, 640)
        for x_input in (0.0, 10.0, 320.0, 639.0):
            x_norm = transform.to_frame_x(x_input)
            assert (x_norm * transform.scale * frame_w) + transform.pad_x == pytest.approx(
                x_input, abs=1e-6
            )
            y_norm = transform.to_frame_y(x_input)
            assert (y_norm * transform.scale * frame_h) + transform.pad_y == pytest.approx(
                x_input, abs=1e-6
            )

    @pytest.mark.parametrize(("frame_w", "frame_h"), [(1920, 1080), (640, 360), (1280, 720)])
    def test_unpadded_axis_maps_into_the_unit_square(self, frame_w: int, frame_h: int) -> None:
        """On an axis with no padding, input pixels map straight into [0, 1]."""
        transform = compute_letterbox(frame_w, frame_h, 640, 640)
        assert transform.to_frame_x(0.0) == pytest.approx(0.0, abs=1e-9)
        assert transform.to_frame_x(640.0) == pytest.approx(1.0, abs=1e-9)

    def test_content_region_maps_into_the_unit_square(self) -> None:
        transform = compute_letterbox(1920, 1080, 640, 640)
        assert transform.to_frame_x(0.0) == pytest.approx(0.0, abs=1e-9)
        assert transform.to_frame_x(640.0) == pytest.approx(1.0)
        # Vertically the content is a 360 px band inside 640, so the padding
        # maps outside the frame and is exactly what clamping exists for.
        assert transform.to_frame_y(140.0) == pytest.approx(0.0)
        assert transform.to_frame_y(500.0) == pytest.approx(1.0)
        assert transform.to_frame_y(0.0) < 0.0
        assert transform.to_frame_y(640.0) > 1.0

    def test_detection_boxes_are_transformed_consistently(self) -> None:
        """A box centred in the model input must be centred in the frame."""
        transform = compute_letterbox(1920, 1080, 640, 640)
        centre = transform.to_normalized(320.0, 320.0)
        assert centre == pytest.approx((0.5, 0.5), abs=1e-6)


class TestPackBatch:
    def test_produces_nchw_float32_in_unit_range(self) -> None:
        image = np.full((4, 4, 3), 255, dtype=np.uint8)
        resized, _ = letterbox(image, 640, 640)
        tensor = pack_batch(resized)
        assert tensor.shape == (1, 3, 640, 640)
        assert tensor.dtype == np.float32
        assert tensor.max() <= 1.0
        assert tensor.min() >= 0.0

    def test_swaps_bgr_to_rgb(self) -> None:
        """OpenCV frames are BGR; the YOLO export expects RGB."""
        image = np.zeros((2, 2, 3), dtype=np.uint8)
        image[:, :, 0] = 255  # pure blue in BGR
        tensor = pack_batch(image)
        # After the swap, blue must be in channel 2 and red must be empty.
        assert tensor[0, 2].max() == pytest.approx(1.0)
        assert tensor[0, 0].max() == pytest.approx(0.0)
        assert tensor[0, 1].max() == pytest.approx(0.0)

    def test_normalises_to_the_unit_range(self) -> None:
        """BGR (0, 128, 255) becomes RGB (255, 128, 0) scaled to [0, 1]."""
        image = np.array([[[0, 128, 255]]], dtype=np.uint8)
        tensor = pack_batch(image)
        assert tensor[0, 0, 0, 0] == pytest.approx(1.0)
        assert tensor[0, 1, 0, 0] == pytest.approx(128 / 255.0)
        assert tensor[0, 2, 0, 0] == pytest.approx(0.0)

    def test_reuses_a_batch_buffer(self) -> None:
        image = np.full((8, 8, 3), 10, dtype=np.uint8)
        batch = np.empty((1, 3, 8, 8), dtype=np.float32)
        tensor = pack_batch(image, out=batch)
        assert tensor is batch
        assert batch.mean() == pytest.approx(10 / 255.0, abs=1e-6)

    def test_rejects_non_uint8(self) -> None:
        with pytest.raises(ValueError, match="uint8"):
            pack_batch(np.zeros((4, 4, 3), dtype=np.float32))


class TestPreprocessFrame:
    def test_returns_tensor_and_transform(self) -> None:
        tensor, transform = preprocess_frame(frame(1920, 1080), 640, 640)
        assert tensor.shape == (1, 3, 640, 640)
        assert isinstance(transform, LetterboxTransform)
        assert transform.scale == pytest.approx(640 / 1920)

    def test_accepts_preallocated_buffers(self) -> None:
        buffer = np.empty((640, 640, 3), dtype=np.uint8)
        batch = np.empty((1, 3, 640, 640), dtype=np.float32)
        tensor, _ = preprocess_frame(frame(), 640, 640, buffer=buffer, batch=batch)
        assert tensor is batch
