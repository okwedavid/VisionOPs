"""ONNX Runtime adapter for YOLO-family detection models.

This is the only shipped :class:`~visionops.vision.contracts.VisionModel`
implementation. Swapping in a different runtime, an NPU delegate or a cloud API
means writing another adapter and registering it
(``docs/TECHNICAL_DECISIONS.md`` ADR-006/ADR-015).
"""

from __future__ import annotations

import hashlib
import logging
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .contracts import Detection, Frame, ModelInfo, ModelState
from .postprocess import greedy_nms, yolo_output_to_candidates
from .preprocess import LetterboxTransform, preprocess_frame

logger = logging.getLogger(__name__)

#: COCO-80 fallback used only when the model carries no ``names`` metadata.
_FALLBACK_COCO = [
    "person",
    "bicycle",
    "car",
    "motorcycle",
    "airplane",
    "bus",
    "train",
    "truck",
    "boat",
    "traffic_light",
    "fire_hydrant",
    "stop_sign",
    "parking_meter",
    "bench",
    "bird",
    "cat",
    "dog",
    "horse",
    "sheep",
    "cow",
    "elephant",
    "bear",
    "zebra",
    "giraffe",
    "backpack",
    "umbrella",
    "handbag",
    "tie",
    "suitcase",
    "frisbee",
    "skis",
    "snowboard",
    "sports_ball",
    "kite",
    "baseball_bat",
    "baseball_glove",
    "skateboard",
    "surfboard",
    "tennis_racket",
    "bottle",
    "wine_glass",
    "cup",
    "fork",
    "knife",
    "spoon",
    "bowl",
    "banana",
    "apple",
    "sandwich",
    "orange",
    "broccoli",
    "carrot",
    "hot_dog",
    "pizza",
    "donut",
    "cake",
    "chair",
    "couch",
    "potted_plant",
    "bed",
    "dining_table",
    "toilet",
    "tv",
    "laptop",
    "mouse",
    "remote",
    "keyboard",
    "cell_phone",
    "microwave",
    "oven",
    "toaster",
    "sink",
    "refrigerator",
    "book",
    "clock",
    "vase",
    "scissors",
    "teddy_bear",
    "hair_drier",
    "toothbrush",
]

_METADATA_CLASSES_KEY = "names"
_METADATA_STRIDE_KEY = "stride"
_METADATA_TASK_KEY = "task"
_METADATA_VERSION_KEY = "version"
_METADATA_AUTHOR_KEY = "author"


class ModelError(RuntimeError):
    """Raised when the model cannot be loaded, validated or executed."""

    def __init__(self, reason_code: str, detail: str) -> None:
        super().__init__(detail)
        self.reason_code = reason_code
        self.detail = detail


def sha256_file(path: Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def parse_class_names(raw: str | None) -> tuple[str, ...]:
    """Parse the ``names`` metadata of a YOLO ONNX export.

    The export writes a Python-list literal (``{0: 'person', 1: 'bicycle'}`` or
    ``['person', 'bicycle']``). We parse it without ``eval`` so a model file can
    never execute code in our process.
    """
    if not raw:
        return ()
    text = raw.strip()
    if not (text.startswith("[") or text.startswith("{")):
        return ()
    try:
        if text.startswith("{"):
            pairs: dict[int, str] = {}
            for item in text[1:-1].split(","):
                if not item.strip():
                    continue
                key, _, value = item.partition(":")
                pairs[int(key.strip().strip("'\""))] = value.strip().strip("'\"")
            return tuple(pairs[index] for index in sorted(pairs))
        return tuple(item.strip().strip("'\"") for item in text[1:-1].split(",") if item.strip())
    except (ValueError, IndexError):  # pragma: no cover - defensive
        logger.warning("could not parse model class names; falling back to generic names")
        return ()


@dataclass(slots=True)
class DetectorConfig:
    """Runtime configuration for :class:`OnnxYoloModel`."""

    model_path: Path
    provider: str = "CPUExecutionProvider"
    threads: int = 0
    input_width: int = 640
    input_height: int = 640
    confidence_threshold: float = 0.35
    nms_iou_threshold: float = 0.45
    class_filter: frozenset[str] = frozenset({"person"})
    expected_sha256: str | None = None
    use_area_interpolation: bool = False


class OnnxYoloModel:
    """A YOLO-family ONNX detector running on ONNX Runtime."""

    def __init__(self, config: DetectorConfig) -> None:
        self._config = config
        self._session = None
        self._info: ModelInfo | None = None
        self._state = ModelState.LOADING
        self._lock = threading.Lock()
        self._input_name: str = ""
        self._output_name: str = ""
        self._classes: tuple[str, ...] = ()
        self._input_shape: tuple[int, int] = (config.input_width, config.input_height)
        self._buffer: np.ndarray | None = None
        self._batch: np.ndarray | None = None
        self._consecutive_errors = 0
        self._last_latency_ms: float | None = None

    # -- properties ---------------------------------------------------------

    @property
    def state(self) -> ModelState:
        return self._state

    @property
    def last_latency_ms(self) -> float | None:
        return self._last_latency_ms

    @property
    def config(self) -> DetectorConfig:
        return self._config

    @property
    def info(self) -> ModelInfo:
        if self._info is None:
            raise ModelError("MODEL_MISSING", "model is not loaded")
        return self._info

    # -- lifecycle ----------------------------------------------------------

    def load(self) -> ModelInfo:
        """Create the inference session. Raises :class:`ModelError` on failure."""
        path = self._config.model_path
        if not path.exists():
            self._state = ModelState.FAILED
            raise ModelError(
                "MODEL_MISSING",
                f"no model at {path}. Run `make model` (or set VISIONOPS_MODEL_PATH) to fetch it.",
            )
        if path.stat().st_size == 0:
            self._state = ModelState.FAILED
            raise ModelError("MODEL_CORRUPT", f"model file {path} is empty")

        checksum = sha256_file(path)
        if self._config.expected_sha256 and checksum != self._config.expected_sha256.lower():
            self._state = ModelState.FAILED
            raise ModelError(
                "MODEL_INTEGRITY",
                f"model checksum mismatch for {path}: expected "
                f"{self._config.expected_sha256}, got {checksum}",
            )

        try:
            import onnxruntime as ort
        except ImportError as exc:  # pragma: no cover - install-time problem
            self._state = ModelState.FAILED
            raise ModelError("MODEL_RUNTIME", "onnxruntime is not installed") from exc

        available = set(ort.get_available_providers())
        requested = self._config.provider
        providers = [requested] if requested in available else [ort.get_available_providers()[0]]
        fallback = None if requested in available else requested

        options = ort.SessionOptions()
        options.log_severity_level = 3  # errors only
        if self._config.threads > 0:
            options.intra_op_num_threads = self._config.threads
            options.inter_op_num_threads = 1

        try:
            session = ort.InferenceSession(
                str(path),
                sess_options=options,
                providers=providers,
            )
        except Exception as exc:
            self._state = ModelState.FAILED
            raise ModelError("MODEL_CORRUPT", f"could not load ONNX model: {exc}") from exc

        inputs = session.get_inputs()
        if not inputs:
            self._state = ModelState.FAILED
            raise ModelError("MODEL_CORRUPT", "model exposes no inputs")
        outputs = session.get_outputs()
        if not outputs:
            self._state = ModelState.FAILED
            raise ModelError("MODEL_CORRUPT", "model exposes no outputs")

        meta = session.get_modelmeta().custom_metadata_map or {}
        classes = parse_class_names(meta.get(_METADATA_CLASSES_KEY))
        if not classes:
            logger.warning(
                "model %s has no usable '%s' metadata; class names are generic",
                path.name,
                _METADATA_CLASSES_KEY,
            )
            classes = tuple(_FALLBACK_COCO)

        input_shape = inputs[0].shape
        if len(input_shape) == 4:
            _, channels, height, width = input_shape
            if isinstance(height, int) and height > 0:
                self._input_shape = (int(width), int(height))
            if isinstance(channels, int) and channels != 3:
                raise ModelError(
                    "MODEL_UNSUPPORTED", f"expected 3 input channels, model declares {channels}"
                )

        self._session = session
        self._input_name = inputs[0].name
        self._output_name = outputs[0].name
        self._classes = classes
        self._state = ModelState.READY
        self._info = ModelInfo(
            name=path.stem,
            path=str(path),
            sha256=checksum,
            provider=providers[0],
            fallback_provider=fallback,
            input_width=self._input_shape[0],
            input_height=self._input_shape[1],
            classes=classes,
            version=meta.get(_METADATA_VERSION_KEY, "unknown"),
        )
        if fallback:
            logger.warning(
                "execution provider %s unavailable; using %s instead", fallback, providers[0]
            )
        if self._input_shape != (self._config.input_width, self._config.input_height):
            logger.info(
                "model input size is %sx%s; ignoring configured %sx%s",
                self._input_shape[0],
                self._input_shape[1],
                self._config.input_width,
                self._config.input_height,
            )
        return self._info

    def warmup(self, iterations: int = 1) -> float:
        """Run throwaway inferences so the first real frame is not slow."""
        import time

        if self._session is None:
            raise ModelError("MODEL_MISSING", "model is not loaded")
        width, height = self._input_shape
        synthetic = Frame(
            image=np.zeros((max(64, height // 2), max(64, width // 2), 3), dtype=np.uint8),
            seq=0,
            timestamp=0.0,
            width=max(64, width // 2),
            height=max(64, height // 2),
        )
        self._buffer = np.full((height, width, 3), 114, dtype=np.uint8)
        self._batch = np.empty((1, 3, height, width), dtype=np.float32)
        started = time.perf_counter()
        for _ in range(max(1, iterations)):
            tensor, transform = preprocess_frame(
                synthetic,
                width,
                height,
                buffer=self._buffer,
                batch=self._batch,
            )
            self._raw_infer(tensor, transform)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if self._info is not None:
            self._info = ModelInfo(
                name=self._info.name,
                path=self._info.path,
                sha256=self._info.sha256,
                provider=self._info.provider,
                input_width=self._info.input_width,
                input_height=self._info.input_height,
                classes=self._info.classes,
                version=self._info.version,
                warmup_ms=elapsed_ms,
                fallback_provider=self._info.fallback_provider,
            )
        logger.info(
            "model %s warmed up in %.1f ms", self._info.name if self._info else "?", elapsed_ms
        )
        return elapsed_ms

    def close(self) -> None:
        self._session = None
        self._state = ModelState.UNCONFIGURED

    # -- inference ----------------------------------------------------------

    def infer(self, frame: Frame) -> list[Detection]:
        """Run one inference. Raises :class:`ModelError` on an execution failure."""
        if self._session is None:
            raise ModelError("MODEL_MISSING", "model is not loaded")

        import time

        width, height = self._input_shape
        if self._buffer is None:
            self._buffer = np.full((height, width, 3), 114, dtype=np.uint8)
        if self._batch is None:
            self._batch = np.empty((1, 3, height, width), dtype=np.float32)

        try:
            with self._lock:
                started = time.perf_counter()
                tensor, transform = preprocess_frame(
                    frame,
                    width,
                    height,
                    use_area=self._config.use_area_interpolation,
                    buffer=self._buffer,
                    batch=self._batch,
                )
                output = self._raw_infer(tensor, transform)
                candidates = yolo_output_to_candidates(
                    output,
                    self._classes,
                    confidence_threshold=self._config.confidence_threshold,
                    class_filter=self._config.class_filter,
                    transform=transform,
                    frame_seq=frame.seq,
                )
                detections = greedy_nms(
                    candidates, self._config.nms_iou_threshold, class_aware=True
                )
                self._last_latency_ms = (time.perf_counter() - started) * 1000.0
        except ModelError:
            raise
        except Exception as exc:
            raise ModelError("MODEL_ERROR", f"inference failed: {exc}") from exc

        self._consecutive_errors = 0
        if self._state is not ModelState.READY:
            self._state = ModelState.READY
        return detections

    def _raw_infer(self, tensor: np.ndarray, transform: LetterboxTransform) -> np.ndarray:
        if self._session is None:  # pragma: no cover - guarded by callers
            raise ModelError("MODEL_MISSING", "model is not loaded")
        outputs = self._session.run([self._output_name], {self._input_name: tensor})
        if not outputs:  # pragma: no cover - defensive
            raise ModelError("MODEL_ERROR", "model returned no output")
        return outputs[0]

    def note_error(self) -> int:
        """Record a failed inference and return the consecutive error count."""
        self._consecutive_errors += 1
        if self._consecutive_errors >= 1:
            self._state = ModelState.DEGRADED
        return self._consecutive_errors


def build_model(config: DetectorConfig) -> OnnxYoloModel:
    """Convenience factory: construct, load and warm up a model."""
    model = OnnxYoloModel(config)
    model.load()
    model.warmup()
    return model


__all__ = [
    "DetectorConfig",
    "ModelError",
    "OnnxYoloModel",
    "build_model",
    "parse_class_names",
    "sha256_file",
]
