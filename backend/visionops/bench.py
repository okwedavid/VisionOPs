"""Performance measurement.

No performance claim is made without a number produced by this script on named
hardware (``docs/PRD.md`` §50). It measures inference latency, effective pipeline
FPS and resident memory, and records the host so results are reproducible.
"""

from __future__ import annotations

import logging
import os
import platform
import statistics
import time
from pathlib import Path
from typing import Any

import numpy as np

from .config import Settings

logger = logging.getLogger(__name__)


def host_info() -> dict[str, Any]:
    """Identify the machine so a published number means something."""
    info: dict[str, Any] = {
        "platform": platform.platform(),
        "processor": platform.processor() or "unknown",
        "python": platform.python_version(),
        "logical_cpus": os.cpu_count(),
    }
    try:
        model_name = [
            line.split(":", 1)[1].strip()
            for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines()
            if line.lower().startswith("model name")
        ]
        if model_name:
            info["cpu_model"] = model_name[0]
    except OSError:  # pragma: no cover - non-Linux
        pass
    try:
        mem_kb = 0
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                mem_kb = int(line.split()[1])
                break
        if mem_kb:
            info["memory_gb"] = round(mem_kb / 1024 / 1024, 1)
    except OSError:  # pragma: no cover - non-Linux
        pass
    try:
        import onnxruntime as ort

        info["onnxruntime_version"] = ort.__version__
        info["providers"] = ort.get_available_providers()
    except ImportError:  # pragma: no cover
        pass
    return info


def run_benchmark(
    settings: Settings,
    *,
    frames: int = 60,
    width: int = 640,
    height: int = 360,
    include_file: str | None = None,
) -> dict[str, Any]:
    """Measure the pipeline on synthetic frames (and optionally a real file)."""
    from .vision.contracts import Frame
    from .vision.onnx_model import DetectorConfig, OnnxYoloModel

    results: dict[str, Any] = {
        "host": host_info(),
        "config": {
            "input": {"width": settings.input_width, "height": settings.input_height},
            "confidence_threshold": settings.confidence_threshold,
            "nms_iou_threshold": settings.nms_iou_threshold,
            "class_filter": sorted(settings.class_set),
            "provider": settings.ort_provider,
            "target_fps": settings.target_fps,
        },
    }

    if not settings.model_path.exists():
        results["model"] = {"state": "unavailable", "detail": f"no model at {settings.model_path}"}
        return results

    model = OnnxYoloModel(
        DetectorConfig(
            model_path=settings.model_path,
            provider=settings.ort_provider,
            threads=settings.ort_threads,
            input_width=settings.input_width,
            input_height=settings.input_height,
            confidence_threshold=settings.confidence_threshold,
            nms_iou_threshold=settings.nms_iou_threshold,
            class_filter=settings.class_set,
        )
    )
    info = model.load()
    results["model"] = info.to_dict()

    warmup_ms = model.warmup()
    results["model"]["warmup_ms"] = round(warmup_ms, 1)

    image = _synthetic_frame(width, height)
    latencies: list[float] = []
    detections_total = 0
    for index in range(frames):
        frame = Frame(
            image=image,
            seq=index,
            timestamp=time.time(),
            width=width,
            height=height,
        )
        started = time.perf_counter()
        detections = model.infer(frame)
        latencies.append((time.perf_counter() - started) * 1000.0)
        detections_total += len(detections)

    results["inference_ms"] = {
        "samples": len(latencies),
        "mean": round(statistics.fmean(latencies), 2),
        "p50": round(statistics.median(latencies), 2),
        "p95": round(_percentile(latencies, 0.95), 2),
        "max": round(max(latencies), 2),
        "detections_per_frame": round(detections_total / max(1, len(latencies)), 2),
    }
    if latencies:
        budget_fps = 1000.0 / max(0.001, statistics.median(latencies))
        results["single_stream_fps_ceiling"] = round(budget_fps, 2)
        results["meets_target_fps"] = budget_fps >= settings.target_fps

    from .observability.metrics import resident_memory_bytes

    results["resident_memory_bytes"] = resident_memory_bytes()

    if include_file:
        results["file_pipeline"] = _bench_file(settings, include_file)

    return results


#: A target no real camera reaches, used to disable rate control in benchmarks.
_UNLIMITED_FPS = 10_000.0


def _bench_file(settings: Settings, path: str) -> dict[str, Any]:
    """End-to-end pipeline throughput over a video file."""
    from .ingest.file import FileSource
    from .vision.pipeline import PipelineConfig, VisionPipeline
    from .vision.tracker import TrackerConfig

    class _NullSink:
        def __init__(self) -> None:
            self.events: list[Any] = []

        def emit(self, events: Any) -> None:
            self.events.extend(events)

        def publish_state(self, state: Any) -> None:
            del state

        def close(self) -> None:
            return None

    from .vision.contracts import ModelInfo
    from .vision.onnx_model import DetectorConfig, OnnxYoloModel

    model = OnnxYoloModel(
        DetectorConfig(
            model_path=settings.model_path,
            provider=settings.ort_provider,
            input_width=settings.input_width,
            input_height=settings.input_height,
            confidence_threshold=settings.confidence_threshold,
            nms_iou_threshold=settings.nms_iou_threshold,
            class_filter=settings.class_set,
        )
    )
    info: ModelInfo = model.load()
    source = FileSource(Path(path), loop=False)
    source.open()
    sink = _NullSink()
    pipeline = VisionPipeline(
        camera_id="bench",
        camera_name="bench",
        session_id="bench",
        source=source,
        model=model,
        sink=sink,
        model_info=info,
        zones=(),
        config=PipelineConfig(
            # Rate control is effectively disabled for the measurement: a file
            # is read as far faster than any camera delivers, so keeping the
            # production target_fps here would drop almost every frame and
            # measure the decoder rather than the pipeline. The number produced
            # is therefore the ceiling this host can sustain, not a deployment
            # setting.
            target_fps=_UNLIMITED_FPS,
            publish_fps=settings.publish_fps,
            tracker_config=TrackerConfig(
                match_iou=settings.tracker_match_iou,
                max_age=settings.tracker_max_age,
                min_hits=settings.tracker_min_hits,
            ),
        ),
    )
    started = time.perf_counter()
    steps = 0
    while pipeline.step():
        steps += 1
        if steps > 5000:  # pragma: no cover - safety valve
            break
    elapsed = time.perf_counter() - started
    source.close()

    # Report the frames that were actually *inferred*, not the ones merely read.
    # Rate control deliberately drops frames, so dividing the read count by the
    # elapsed time advertises a throughput the pipeline never achieved — a file
    # benchmark "ran" at 769 fps on a host that does 10.
    inferred = pipeline.stats.frames_processed
    return {
        "path": path,
        "frames_read": steps,
        "frames_processed": inferred,
        "frames_dropped_by_rate_control": pipeline.stats.frames_dropped,
        "elapsed_seconds": round(elapsed, 2),
        "inference_bound_fps": round(inferred / elapsed, 2) if elapsed > 0 else 0.0,
        "note": (
            "measured with rate control disabled, so every frame reached the model: "
            "this is the throughput ceiling of this host, not a deployment setting"
        ),
        "events": len(sink.events),
    }


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(quantile * (len(ordered) - 1))))
    return ordered[index]


def _synthetic_frame(width: int, height: int) -> np.ndarray:
    """A structured synthetic frame: gradients plus rectangles.

    Purposefully busy so inference is measured on representative work rather than
    an empty image that short-circuits in the graph.
    """
    import cv2

    image = np.zeros((height, width, 3), dtype=np.uint8)
    gradient = np.linspace(20, 90, width, dtype=np.uint8)
    image[:, :, 0] = gradient
    image[:, :, 1] = np.linspace(40, 120, height, dtype=np.uint8)[:, None]
    rng = np.random.default_rng(7)
    for _ in range(6):
        x = int(rng.integers(0, max(1, width - 90)))
        y = int(rng.integers(0, max(1, height - 160)))
        cv2.rectangle(image, (x, y), (x + 70, y + 150), (int(rng.integers(60, 220)),) * 3, -1)
    return image


__all__ = ["host_info", "run_benchmark"]
