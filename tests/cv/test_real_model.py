"""Tests that need the real ONNX detection model.

Deselected by default; run with::

    pytest -m model

The model is **not** committed (see ``models/SHA256SUMS`` and the fetch command
in ``docs/DEPLOYMENT.md``). When it is absent these tests skip with a clear
message rather than silently passing, because "no model, no coverage" must not
look like "covered".
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from visionops.config import Settings
from visionops.vision.contracts import BBox, Frame, SourceState
from visionops.vision.onnx_model import (
    DetectorConfig,
    ModelError,
    ModelState,
    OnnxYoloModel,
    sha256_file,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MODEL_PATH = REPO_ROOT / "models" / "yolo11n.onnx"
SHA256_FILE = REPO_ROOT / "models" / "SHA256SUMS"
FIXTURE_VIDEO = REPO_ROOT / "samples" / "fixtures" / "zone-walkthrough.mp4"

pytestmark = pytest.mark.model


def _skip_unless_model() -> None:
    if not MODEL_PATH.exists():
        pytest.skip(
            f"{MODEL_PATH.name} is not present; fetch it with "
            "`uv run python scripts/fetch_model.py` to run these tests"
        )


@pytest.fixture(scope="module")
def model() -> Any:
    _skip_unless_model()
    detector = OnnxYoloModel(
        DetectorConfig(
            model_path=MODEL_PATH,
            confidence_threshold=0.3,
            nms_iou_threshold=0.5,
            class_filter=frozenset({"person"}),
        )
    )
    detector.load()
    detector.warmup()
    yield detector
    detector.close()


def _fixture_frame(index: int = 0) -> np.ndarray:
    """One decoded frame from the committed synthetic walkthrough."""
    import cv2

    if not FIXTURE_VIDEO.exists():
        pytest.skip("the synthetic walkthrough fixture is not present")
    capture = cv2.VideoCapture(str(FIXTURE_VIDEO))
    try:
        capture.set(cv2.CAP_PROP_POS_FRAMES, index)
        ok, image = capture.read()
    finally:
        capture.release()
    if not ok or image is None:
        pytest.skip(f"frame {index} of the fixture could not be decoded")
    return image


def _frame_from_image(image: np.ndarray, seq: int = 1) -> Frame:
    height, width = image.shape[:2]
    return Frame(image=image, seq=seq, timestamp=float(seq), width=width, height=height)


#: An image containing real people, supplied by the operator. It is never
#: committed: ``samples/SOURCES.md`` requires documented consent for anything
#: with identifiable people in it.
REAL_PHOTO_ENV = "VISIONOPS_TEST_PHOTO"


def _real_photo() -> np.ndarray:
    """A photograph with people in it, for detection assertions.

    Point ``VISIONOPS_TEST_PHOTO`` at one. Without it these tests skip: we will
    not commit footage of people to make a test pass.
    """
    import os

    import cv2

    candidate = os.environ.get(REAL_PHOTO_ENV)
    if not candidate:
        pytest.skip(
            f"set {REAL_PHOTO_ENV} to an image containing people to run the detection tests; "
            "nothing with identifiable people is committed (see samples/SOURCES.md)"
        )
    path = Path(candidate)
    if not path.exists():
        pytest.skip(f"{REAL_PHOTO_ENV}={candidate} does not exist")
    image = cv2.imread(str(path))
    if image is None:
        pytest.skip(f"{path} could not be decoded as an image")
    return image


# ---------------------------------------------------------------------------
# Integrity
# ---------------------------------------------------------------------------


class TestModelIntegrity:
    def test_the_checksum_file_lists_the_model(self) -> None:
        _skip_unless_model()
        assert SHA256_FILE.exists(), "models/SHA256SUMS must document the weight"

    def test_the_file_matches_its_recorded_checksum(self) -> None:
        """A silently swapped weight file is a supply-chain problem."""
        _skip_unless_model()
        recorded = None
        for line in SHA256_FILE.read_text().splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            digest, _, name = line.partition("  ")
            if name.strip() == MODEL_PATH.name:
                recorded = digest.strip()
                break
        assert recorded is not None, f"{MODEL_PATH.name} is not listed in {SHA256_FILE}"
        assert sha256_file(MODEL_PATH) == recorded

    def test_the_model_is_not_committed_to_git(self) -> None:
        """ "Fetch it yourself" only works if the blob is genuinely absent."""
        ignore = REPO_ROOT / ".gitignore"
        assert ignore.exists()
        patterns = ignore.read_text()
        assert any(token in patterns for token in ("models/*.onnx", "*.onnx", "models/")), (
            ".gitignore should exclude the weight file"
        )

    def test_a_missing_model_fails_loudly_rather_than_silently(self, tmp_path: Any) -> None:
        detector = OnnxYoloModel(DetectorConfig(model_path=tmp_path / "not-there.onnx"))
        with pytest.raises(ModelError) as caught:
            detector.load()
        assert caught.value.reason_code == "MODEL_MISSING"
        assert detector.state is ModelState.FAILED


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


class TestRealDetection:
    def test_people_are_found_in_a_real_photograph(self, model: Any) -> None:
        image = _real_photo()
        detections = model.infer(_frame_from_image(image))
        assert detections, "the supplied photograph contains people; none were found"
        assert all(d.label == "person" for d in detections), "the class filter must hold"
        assert all(d.confidence >= 0.3 for d in detections)

    def test_every_box_is_normalised_and_ordered(self, model: Any) -> None:
        """Normalised geometry is a contract the UI and zone engine depend on."""
        for detection in model.infer(_frame_from_image(_real_photo())):
            box = detection.box
            assert isinstance(box, BBox)
            assert 0.0 <= box.x1 <= box.x2 <= 1.0
            assert 0.0 <= box.y1 <= box.y2 <= 1.0

    def test_a_synthetic_drawing_is_not_mistaken_for_a_person(self, model: Any) -> None:
        """The committed fixture contains no people, and must stay that way."""
        assert FIXTURE_VIDEO.exists()
        detections = model.infer(_frame_from_image(_fixture_frame(0)))
        assert detections == [], (
            "a drawn rectangle should not be detected as a person; if this fires, "
            "either the fixture changed or the model is producing phantom detections"
        )

    def test_a_blank_frame_yields_nothing_without_an_error(self, model: Any) -> None:
        blank = np.zeros((360, 640, 3), dtype=np.uint8)
        assert model.infer(_frame_from_image(blank)) == []

    def test_inference_is_deterministic_for_the_same_input(self, model: Any) -> None:
        frame = _frame_from_image(_real_photo())
        first = [(d.label, round(d.confidence, 6), d.box) for d in model.infer(frame)]
        second = [(d.label, round(d.confidence, 6), d.box) for d in model.infer(frame)]
        assert first == second

    def test_the_model_reports_its_own_identity(self, model: Any) -> None:
        info = model.info
        assert info.name
        assert len(info.sha256) == 64
        assert info.provider
        assert "person" in info.classes
        assert (info.input_width, info.input_height) == (640, 640)

    def test_warmup_moves_the_latency_measurement(self, model: Any) -> None:
        model.infer(_frame_from_image(_real_photo()))
        assert model.last_latency_ms is not None
        assert model.last_latency_ms > 0


# ---------------------------------------------------------------------------
# Throughput
# ---------------------------------------------------------------------------


class TestRealPerformance:
    def test_inference_time_is_bounded(self, model: Any) -> None:
        """A regression guard, not a benchmark.

        The bound is deliberately loose: it catches "inference became two orders
        of magnitude slower" (a silent provider change, a fallback to a
        software EP, a lost thread setting). Whether a given host meets a
        deployment's FPS target is a property of that host and is measured and
        published in ``docs/benchmarks.md``, not asserted here.
        """
        frame = _frame_from_image(_fixture_frame(0))
        model.infer(frame)
        latencies: list[float] = []
        for _ in range(5):
            model.infer(frame)
            if model.last_latency_ms is not None:
                latencies.append(model.last_latency_ms)
        mean_ms = sum(latencies) / len(latencies)
        print(f"\nmean yolo11n inference: {mean_ms:.1f} ms ({1000 / mean_ms:.1f} fps) on this host")
        assert mean_ms < 500, (
            f"mean inference {mean_ms:.1f} ms is far outside the documented budget; "
            "check the ONNX provider and thread settings"
        )

    def test_a_short_clip_can_be_processed_end_to_end(self, model: Any) -> None:
        """Decode plus inference, which is what a session actually does."""
        if not FIXTURE_VIDEO.exists():
            pytest.skip("the synthetic walkthrough fixture is not present")
        import cv2

        capture = cv2.VideoCapture(str(FIXTURE_VIDEO))
        assert capture.isOpened()
        processed = 0
        started = time.perf_counter()
        try:
            while processed < 20:
                ok, image = capture.read()
                if not ok:
                    break
                detections = model.infer(_frame_from_image(image, processed + 1))
                assert isinstance(detections, list)
                processed += 1
        finally:
            capture.release()
        assert processed > 0, "no frame could be decoded from the fixture"
        assert time.perf_counter() - started < 60


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class TestRealConfiguration:
    def test_a_tighter_threshold_yields_no_more_detections(self, model: Any) -> None:
        """Sanity check that the threshold is actually wired to the model."""
        frame = _frame_from_image(_real_photo())
        detector = OnnxYoloModel(
            DetectorConfig(
                model_path=MODEL_PATH,
                confidence_threshold=0.95,
                class_filter=frozenset({"person"}),
            )
        )
        try:
            detector.load()
            detector.warmup()
            assert len(detector.infer(frame)) <= len(model.infer(frame))
        finally:
            detector.close()

    def test_a_class_filter_that_matches_nothing_yields_nothing(self) -> None:
        _skip_unless_model()
        detector = OnnxYoloModel(
            DetectorConfig(
                model_path=MODEL_PATH,
                confidence_threshold=0.3,
                class_filter=frozenset({"aardvark"}),
            )
        )
        try:
            detector.load()
            detector.warmup()
            assert detector.infer(_frame_from_image(_real_photo())) == []
        finally:
            detector.close()

    def test_the_default_settings_path_resolves_to_the_shipped_model(self) -> None:
        settings = Settings(data_dir=REPO_ROOT, media_root=REPO_ROOT / "samples")
        assert settings.model_path.name == MODEL_PATH.name

    def test_the_configured_model_reports_a_ready_state(self) -> None:
        _skip_unless_model()
        settings = Settings(data_dir=REPO_ROOT, media_root=REPO_ROOT / "samples")
        detector = OnnxYoloModel(DetectorConfig(model_path=settings.model_path))
        try:
            detector.load()
            detector.warmup()
            assert detector.state is ModelState.READY
        finally:
            detector.close()
        assert SourceState.RUNNING.value == "running"
