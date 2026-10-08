"""A small metrics registry with Prometheus text exposition (ADR-016).

Deliberately not ``prometheus-client``: we need three metric types and a text
exporter, and every dependency is also a security surface. Label cardinality is
bounded — beyond a cap, further label values collapse to ``other`` so a
mis-instrumented call site can neither blow up memory nor the exporter.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Iterable, Mapping, MutableMapping
from typing import Any

#: Default histogram buckets, in seconds: 5 ms → 5 s.
DEFAULT_BUCKETS: tuple[float, ...] = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)

#: Hard ceilings that protect the exporter from unbounded cardinality.
MAX_LABELS = 8
MAX_SERIES_PER_METRIC = 200
_OTHER = "other"

LabelPairs = tuple[tuple[str, str], ...]
Counts = list[int]
HistogramState = tuple[Counts, float, int]


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _fmt(value: float) -> str:
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return f"{value!r}"


def _label_text(pairs: LabelPairs, extra: tuple[str, str] | None = None) -> str:
    items = dict(pairs)
    if extra is not None:
        items[extra[0]] = extra[1]
    if not items:
        return ""
    return "{" + ",".join(f'{k}="{_escape(v)}"' for k, v in sorted(items.items())) + "}"


def _normalise(labels: Mapping[str, Any]) -> LabelPairs:
    clean = {
        str(k): (str(v) if v is not None else "")
        for k, v in labels.items()
        if v is not None and str(k) not in _RESERVED_LABELS
    }
    if len(clean) > MAX_LABELS:
        raise ValueError(f"at most {MAX_LABELS} metric labels are allowed, got {len(clean)}")
    return tuple(sorted(clean.items()))


_RESERVED_LABELS = frozenset({"name", "args", "message", "exc_info"})


class MetricsRegistry:
    """Thread-safe counters, gauges and histograms with a Prometheus exporter."""

    def __init__(self, *, buckets: Iterable[float] = DEFAULT_BUCKETS) -> None:
        self._buckets = tuple(sorted(buckets))
        self._lock = threading.Lock()
        self._counters: dict[str, dict[LabelPairs, float]] = {}
        self._gauges: dict[str, dict[LabelPairs, float]] = {}
        self._histograms: dict[str, dict[LabelPairs, HistogramState]] = {}
        self._help: dict[str, str] = {}

    # -- registration -------------------------------------------------------

    def describe(self, name: str, text: str) -> None:
        """Attach a HELP string to a metric name."""
        with self._lock:
            self._help[name] = text

    # -- recording ----------------------------------------------------------

    def counter(self, name: str, value: float = 1.0, **labels: Any) -> None:
        key = _normalise(labels)
        with self._lock:
            series = self._counters.setdefault(name, {})
            if key not in series and len(series) >= MAX_SERIES_PER_METRIC:
                key = ((_OTHER, ""),)
            series[key] = series.get(key, 0.0) + value

    def gauge(self, name: str, value: float, **labels: Any) -> None:
        key = _normalise(labels)
        with self._lock:
            series = self._gauges.setdefault(name, {})
            if key not in series and len(series) >= MAX_SERIES_PER_METRIC:
                key = ((_OTHER, ""),)
            series[key] = float(value)

    def histogram(self, name: str, value: float, **labels: Any) -> None:
        key = _normalise(labels)
        with self._lock:
            series = self._histograms.setdefault(name, {})
            if key not in series and len(series) >= MAX_SERIES_PER_METRIC:
                key = ((_OTHER, ""),)
            counts, total, count = series.get(key, ([0] * len(self._buckets), 0.0, 0))
            for index, upper in enumerate(self._buckets):
                if value <= upper:
                    counts[index] += 1
            if math.isinf(value):
                counts[-1] += 1
            series[key] = (counts, total + value, count + 1)

    def observe_ms(self, name: str, milliseconds: float, **labels: Any) -> None:
        """Record a millisecond duration on a second-based histogram."""
        self.histogram(name, milliseconds / 1000.0, **labels)

    # -- querying -----------------------------------------------------------

    def total(self, name: str) -> float:
        """Sum of every series of a counter or gauge (ignores labels)."""
        with self._lock:
            return sum(self._counters.get(name, {}).values()) + sum(
                self._gauges.get(name, {}).values()
            )

    def value(self, name: str, **labels: Any) -> float | None:
        """Value of a specific series, or ``None`` when absent."""
        key = _normalise(labels)
        with self._lock:
            if name in self._counters:
                return self._counters[name].get(key)
            if name in self._gauges:
                return self._gauges[name].get(key)
            return None

    def histogram_stats(self, name: str, **labels: Any) -> dict[str, float] | None:
        """``count``, ``sum`` and a linear-interpolated p95 for one series."""
        key = _normalise(labels)
        with self._lock:
            series = self._histograms.get(name)
            if not series or key not in series:
                return None
            counts, total, count = series[key]
        return {
            "count": float(count),
            "sum": total,
            "p50": self._quantile_from(counts, 0.50, total, count),
            "p95": self._quantile_from(counts, 0.95, total, count),
        }

    def _quantile_from(self, counts: Counts, quantile: float, total: float, count: int) -> float:
        if count == 0:
            return 0.0
        target = quantile * count
        cumulative = 0
        for index, upper in enumerate(self._buckets):
            cumulative = counts[index]
            if cumulative >= target:
                lower = self._buckets[index - 1] if index > 0 else 0.0
                span = upper - lower
                if span <= 0:
                    return upper
                return lower + span * (target - (cumulative - counts[index])) / counts[index]
        return self._buckets[-1] if self._buckets else 0.0

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._histograms.clear()

    def drop_label_value(self, label: str, value: str) -> None:
        """Remove every series carrying ``label=value`` (used when a session ends)."""
        with self._lock:
            # Each store maps metric name -> label tuple -> state. The value
            # types differ (float vs HistogramState), but this only manipulates
            # the keys, so the stores are handled through one common type.
            stores: list[MutableMapping[str, Any]] = [
                self._counters,
                self._gauges,
                self._histograms,
            ]
            for store in stores:
                for name, series in list(store.items()):
                    series_map: MutableMapping[Any, Any] = series
                    for key in [k for k in series_map if (label, value) in k]:
                        del series_map[key]
                    if not series_map:
                        del store[name]

    # -- exporting ----------------------------------------------------------

    def render(self) -> str:
        """Prometheus text exposition format."""
        with self._lock:
            counters = {k: dict(v) for k, v in self._counters.items()}
            gauges = {k: dict(v) for k, v in self._gauges.items()}
            histograms = {k: dict(v) for k, v in self._histograms.items()}
            help_text = dict(self._help)

        lines: list[str] = []
        for name in sorted(counters):
            lines.append(f"# HELP {name} {help_text.get(name, name)}")
            lines.append(f"# TYPE {name} counter")
            for key, value in sorted(counters[name].items()):
                lines.append(f"{name}{_label_text(key)} {_fmt(value)}")

        for name in sorted(gauges):
            lines.append(f"# HELP {name} {help_text.get(name, name)}")
            lines.append(f"# TYPE {name} gauge")
            for key, value in sorted(gauges[name].items()):
                lines.append(f"{name}{_label_text(key)} {_fmt(value)}")

        for name in sorted(histograms):
            lines.append(f"# HELP {name} {help_text.get(name, name)}")
            lines.append(f"# TYPE {name} histogram")
            for key, (counts, total, count) in sorted(histograms[name].items()):
                for index, upper in enumerate(self._buckets):
                    lines.append(
                        f"{name}_bucket{_label_text(key, ('le', _fmt(upper)))} {counts[index]}"
                    )
                lines.append(f"{name}_bucket{_label_text(key, ('le', '+Inf'))} {count}")
                lines.append(f"{name}_sum{_label_text(key)} {_fmt(total)}")
                lines.append(f"{name}_count{_label_text(key)} {count}")
        return "\n".join(lines) + "\n"


#: Process-wide registry. One instance per process is the whole point of
#: exporting over HTTP rather than running an agent.
registry = MetricsRegistry()

# Metric names used across the codebase, declared once so typos are impossible.
METRIC_HTTP_REQUESTS = "visionops_http_requests_total"
METRIC_HTTP_LATENCY = "visionops_http_request_duration_seconds"
METRIC_FRAMES = "visionops_pipeline_frames_total"
METRIC_FRAMES_DROPPED = "visionops_pipeline_frames_dropped_total"
METRIC_PIPELINE_FPS = "visionops_pipeline_fps"
METRIC_FRAME_AGE = "visionops_pipeline_frame_age_ms"
METRIC_INFERENCE_LATENCY = "visionops_inference_latency_seconds"
METRIC_ACTIVE_TRACKS = "visionops_active_tracks"
METRIC_TRACKS_CREATED = "visionops_tracks_created_total"
METRIC_EVENTS = "visionops_events_total"
METRIC_EVENTS_SUPPRESSED = "visionops_events_suppressed_total"
METRIC_EVENTS_DROPPED = "visionops_events_dropped_total"
METRIC_EVENT_WRITE_LATENCY = "visionops_event_write_latency_seconds"
METRIC_SOURCE_STATE = "visionops_source_state"
METRIC_SOURCE_FRAMES = "visionops_source_frames_total"
METRIC_SOURCE_ERRORS = "visionops_source_errors_total"
METRIC_SOURCE_RETRIES = "visionops_source_retries_total"
METRIC_SESSION_UPTIME = "visionops_session_uptime_seconds"
METRIC_SESSIONS_ACTIVE = "visionops_sessions_active"
METRIC_WS_CONNECTIONS = "visionops_ws_connections"
METRIC_WS_SENT = "visionops_ws_messages_sent_total"
METRIC_WS_DROPPED = "visionops_ws_messages_dropped_total"
METRIC_RATE_LIMITED = "visionops_rate_limited_total"
METRIC_AUTH_FAILURES = "visionops_auth_failures_total"
METRIC_MEMORY = "visionops_process_resident_memory_bytes"
METRIC_CPU_SECONDS = "visionops_process_cpu_seconds_total"
METRIC_UPTIME = "visionops_process_uptime_seconds"
METRIC_LOG_REDACTIONS = "visionops_log_redactions_total"
METRIC_DETECTIONS = "visionops_detections_total"
METRIC_INFERENCE_ERRORS = "visionops_inference_errors_total"
METRIC_MODEL_STATE = "visionops_model_state"

_DESCRIPTIONS: dict[str, str] = {
    METRIC_HTTP_REQUESTS: "HTTP requests by method, path template and status",
    METRIC_HTTP_LATENCY: "HTTP request duration in seconds",
    METRIC_FRAMES: "Frames processed by the pipeline",
    METRIC_FRAMES_DROPPED: "Frames dropped by the pipeline",
    METRIC_PIPELINE_FPS: "Effective processed frames per second",
    METRIC_FRAME_AGE: "Age of the newest frame in milliseconds",
    METRIC_INFERENCE_LATENCY: "Model inference duration in seconds",
    METRIC_ACTIVE_TRACKS: "Currently reported tracks",
    METRIC_TRACKS_CREATED: "Tracks created since start",
    METRIC_EVENTS: "Events emitted by type and severity",
    METRIC_EVENTS_SUPPRESSED: "Transitions suppressed by gate and reason",
    METRIC_EVENTS_DROPPED: "Events lost after write retries",
    METRIC_EVENT_WRITE_LATENCY: "Event persistence duration in seconds",
    METRIC_SOURCE_STATE: "Current source state (1 for the active state)",
    METRIC_SOURCE_FRAMES: "Frames read from a source",
    METRIC_SOURCE_ERRORS: "Source errors by reason code",
    METRIC_SOURCE_RETRIES: "Source reopen attempts",
    METRIC_SESSION_UPTIME: "Session uptime in seconds",
    METRIC_SESSIONS_ACTIVE: "Active processing sessions",
    METRIC_WS_CONNECTIONS: "Open WebSocket connections",
    METRIC_WS_SENT: "WebSocket messages sent by type",
    METRIC_WS_DROPPED: "WebSocket messages dropped, usually for slow clients",
    METRIC_RATE_LIMITED: "Requests rejected by the rate limiter",
    METRIC_AUTH_FAILURES: "Authentication failures by reason",
    METRIC_MEMORY: "Process resident memory in bytes",
    METRIC_CPU_SECONDS: "Process CPU time in seconds",
    METRIC_UPTIME: "Process uptime in seconds",
    METRIC_LOG_REDACTIONS: "Log fields redacted as possible secrets",
    METRIC_DETECTIONS: "Raw detections by class label",
    METRIC_INFERENCE_ERRORS: "Inference exceptions",
    METRIC_MODEL_STATE: "Model state (1 for the active state)",
}

for _name, _text in _DESCRIPTIONS.items():
    registry.describe(_name, _text)


def resident_memory_bytes() -> int | None:
    """Process RSS in bytes, or ``None`` where it cannot be determined cheaply."""
    try:
        with open("/proc/self/statm", encoding="ascii") as handle:
            pages = int(handle.read().split()[1])
    except (OSError, IndexError, ValueError):  # pragma: no cover - non-Linux
        return None
    import resource

    page_size = resource.getpagesize()
    return pages * page_size


def cpu_seconds() -> float:
    """Process CPU time (user + system) in seconds."""
    import resource

    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


__all__ = [
    "DEFAULT_BUCKETS",
    "MAX_LABELS",
    "MAX_SERIES_PER_METRIC",
    "MetricsRegistry",
    "cpu_seconds",
    "registry",
    "resident_memory_bytes",
]
