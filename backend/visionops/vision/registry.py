"""Extension registries for the vision engine (ADR-015).

One place to register an alternative model, tracker or zone strategy. Code
resolves implementations by name from configuration; no ``if/else`` chains, and
no caller imports a concrete class directly.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .contracts import Tracker, VisionModel, ZoneStrategy
from .event_engine import EVENT_REGISTRY
from .onnx_model import DetectorConfig, OnnxYoloModel
from .tracker import KalmanIouTracker, TrackerConfig
from .zones import LineZoneStrategy, PolygonZoneStrategy

VisionModelFactory = Callable[[Any], VisionModel]
TrackerFactory = Callable[[Any], Tracker]
ZoneStrategyFactory = Callable[[], ZoneStrategy]

MODELS: dict[str, VisionModelFactory] = {"onnx-yolo": OnnxYoloModel}
TRACKERS: dict[str, TrackerFactory] = {"kalman-iou": KalmanIouTracker}
ZONE_STRATEGIES: dict[str, ZoneStrategyFactory] = {
    "polygon": PolygonZoneStrategy,
    "line": LineZoneStrategy,
}
EVENT_TYPES: dict[str, Any] = EVENT_REGISTRY


def build_model(name: str, config: DetectorConfig | Any) -> VisionModel:
    """Build a detection model by registry name."""
    try:
        factory = MODELS[name]
    except KeyError as exc:
        available = ", ".join(sorted(MODELS)) or "(none registered)"
        raise KeyError(f"unknown model {name!r}; registered: {available}") from exc
    return factory(config)


def build_tracker(name: str, config: TrackerConfig | Any) -> Tracker:
    """Build a tracker by registry name."""
    try:
        factory = TRACKERS[name]
    except KeyError as exc:
        available = ", ".join(sorted(TRACKERS)) or "(none registered)"
        raise KeyError(f"unknown tracker {name!r}; registered: {available}") from exc
    return factory(config)


def build_zone_strategy(kind: str) -> ZoneStrategy:
    """Build a zone strategy for a zone ``kind``."""
    try:
        factory = ZONE_STRATEGIES[kind]
    except KeyError as exc:
        available = ", ".join(sorted(ZONE_STRATEGIES)) or "(none registered)"
        raise KeyError(f"unknown zone kind {kind!r}; registered: {available}") from exc
    return factory()


def register_model(name: str, factory: VisionModelFactory) -> None:
    """Register an alternative :class:`VisionModel` implementation."""
    MODELS[name] = factory


def register_tracker(name: str, factory: TrackerFactory) -> None:
    """Register an alternative :class:`Tracker` implementation."""
    TRACKERS[name] = factory


def register_zone_strategy(kind: str, factory: ZoneStrategyFactory) -> None:
    """Register an alternative :class:`ZoneStrategy` implementation."""
    ZONE_STRATEGIES[kind] = factory


__all__ = [
    "EVENT_TYPES",
    "MODELS",
    "TRACKERS",
    "ZONE_STRATEGIES",
    "build_model",
    "build_tracker",
    "build_zone_strategy",
    "register_model",
    "register_tracker",
    "register_zone_strategy",
]
