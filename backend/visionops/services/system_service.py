"""System status: the human-facing health aggregate behind the System page.

Every subsystem reports ``(state, reason_code, detail)``. The aggregate turns
those into one verdict. Reason codes are part of the public contract, so clients
may switch on them (``docs/OBSERVABILITY.md`` §4).
"""

from __future__ import annotations

import contextlib
import logging
import shutil
import time
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import text

from ..observability.metrics import (
    cpu_seconds,
    registry,
    resident_memory_bytes,
)

logger = logging.getLogger(__name__)

HEALTHY = "healthy"
DEGRADED = "degraded"
FAILED = "failed"
MIN_FREE_DISK_BYTES = 500 * 1024 * 1024

_SEVERITY_RANK = {"healthy": 0, "degraded": 1, "failed": 2}


@dataclass(frozen=True, slots=True)
class Check:
    """One subsystem's health."""

    state: str
    reason_code: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"state": self.state, "reason_code": self.reason_code, "detail": self.detail}

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK.get(self.state, 1)


class Probe(Protocol):
    """A subsystem health check. Must never raise."""

    def check(self) -> Check: ...


class DatabaseProbe:
    """Connectivity only — a readiness probe must not be slow or destructive."""

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    def check(self) -> Check:
        started = time.perf_counter()
        try:
            with self._session_factory() as session:
                session.execute(text("SELECT 1"))
        except Exception as exc:
            return Check(FAILED, "DB_UNREACHABLE", f"database is unreachable: {exc}")
        return Check("ok", None, f"{(time.perf_counter() - started) * 1000:.1f} ms")


class StorageProbe:
    """Free space and writability of the snapshot directory."""

    def __init__(self, snapshot_dir: Any, *, enabled: bool) -> None:
        self._dir = snapshot_dir
        self._enabled = enabled

    def check(self) -> Check:
        if not self._enabled:
            return Check("ok", None, "snapshots disabled (privacy default)")
        try:
            usage = __import__("shutil").disk_usage(str(self._dir))
        except OSError as exc:
            return Check(DEGRADED, "STORAGE_UNAVAILABLE", f"cannot stat {self._dir}: {exc}")
        free = usage.free
        if free < MIN_FREE_DISK_BYTES:
            return Check(
                DEGRADED,
                "DISK_LOW",
                f"only {free / 1024 / 1024:.0f} MB free — run `visionops prune`",
            )
        return Check("ok", None, f"{free / 1024 / 1024 / 1024:.1f} GB free")


class ModelProbe:
    """Reports whatever the model loader last observed, including failure."""

    def __init__(self, model_state_provider: Any) -> None:
        self._provider = model_state_provider

    def check(self) -> Check:
        return self._provider()


class SessionProbe:
    """Worst state across all running sessions."""

    def __init__(self, session_manager: Any) -> None:
        self._manager = session_manager

    def check(self) -> Check:
        states = self._manager.states()
        if not states:
            return Check("ok", None, "no active sessions")
        worst = Check("ok")
        for state in states:
            if state.state == "failed":
                candidate = Check(FAILED, state.reason_code, f"{state.camera_name}: {state.detail}")
            elif state.state == "degraded":
                candidate = Check(
                    DEGRADED, state.reason_code, f"{state.camera_name}: {state.detail}"
                )
            else:
                continue
            if candidate.rank > worst.rank:
                worst = candidate
        return worst


class SystemService:
    """Aggregates probes, counters and runtime stats into one status document."""

    def __init__(
        self,
        *,
        settings: Any,
        session_manager: Any,
        version: str,
        started_at: float,
        encryption_state: str = "not-configured",
        snapshots_enabled: bool = False,
        model_error: str | None = None,
    ) -> None:
        self._settings = settings
        self._sessions = session_manager
        self._version = version
        self._started_at = started_at
        self._encryption_state = encryption_state
        self._snapshots_enabled = snapshots_enabled
        self._model_error = model_error
        self._probes: list[Probe] = [
            DatabaseProbe(build_probe_session_factory(settings)),
            StorageProbe(settings.snapshot_dir, enabled=snapshots_enabled),
        ]

    def register_probe(self, probe: Probe) -> None:
        self._probes.append(probe)

    def check_all(self) -> dict[str, Check]:
        results: dict[str, Check] = {}
        for probe in self._probes:
            name = type(probe).__name__.removesuffix("Probe").lower()
            try:
                results[name] = probe.check()
            except Exception as exc:
                logger.warning("health probe %s raised", name, exc_info=True)
                results[name] = Check(DEGRADED, "PROBE_ERROR", str(exc))
        return results

    def status(self, *, model_state: str = "unknown", model_info: Any = None) -> dict[str, Any]:
        checks = self.check_all()
        sessions = [state.to_dict() for state in self._sessions.states()]

        model_check = _model_check(model_state, model_info, self._model_error)
        checks["model"] = model_check

        verdict = HEALTHY
        for check in (*checks.values(), model_check):
            if check.rank > _SEVERITY_RANK[verdict]:
                verdict = check.state if check.state in _SEVERITY_RANK else DEGRADED

        free_bytes = None
        with contextlib.suppress(OSError):  # pragma: no cover - platform dependent
            free_bytes = shutil.disk_usage(str(self._settings.data_dir)).free

        return {
            "status": verdict,
            "version": self._version,
            "environment": self._settings.env,
            "uptime_seconds": round(time.time() - self._started_at, 1),
            "model": model_check.to_dict() | (model_info.to_dict() if model_info else {}),
            "sessions": {
                "active": len(sessions),
                "max": self._sessions.capacity,
                "items": sessions,
            },
            "checks": {name: check.to_dict() for name, check in checks.items()},
            "storage": {
                "database": checks.get("database", Check("unknown")).state,
                "snapshot_dir": "enabled" if self._snapshots_enabled else "disabled",
                "disk_free_bytes": free_bytes,
                "credential_encryption": self._encryption_state,
            },
            "counters": {
                "events_total": int(registry.total("visionops_events_total")),
                "events_suppressed_total": int(registry.total("visionops_events_suppressed_total")),
                "events_dropped_total": int(registry.total("visionops_events_dropped_total")),
                "inference_errors_total": int(registry.total("visionops_inference_errors_total")),
                "source_errors_total": int(registry.total("visionops_source_errors_total")),
                "rate_limited_total": int(registry.total("visionops_rate_limited_total")),
                "auth_failures_total": int(registry.total("visionops_auth_failures_total")),
            },
            "runtime": {
                "resident_memory_bytes": resident_memory_bytes(),
                "cpu_seconds": round(cpu_seconds(), 2),
                "log_redactions_total": int(registry.total("visionops_log_redactions_total")),
            },
        }

    def readiness(self) -> tuple[bool, dict[str, Any]]:
        """Readiness: the API can serve requests.

        A missing model makes this *degraded*, not *unready* — configuration,
        history and export still work, and the UI says so honestly.
        """
        checks = self.check_all()
        database = checks.get("database", Check("unknown"))
        ready = database.state != FAILED
        return ready, {name: check.to_dict() for name, check in checks.items()}


def _model_check(state: str, model_info: Any, model_error: str | None = None) -> Check:
    if state == "ready":
        return Check("ok", None, f"{model_info.name if model_info else 'model'} ready")
    if state in ("loading", "downloading"):
        return Check(DEGRADED, "MODEL_LOADING", "the detection model is still loading")
    if state == "degraded":
        return Check(DEGRADED, "MODEL_ERROR", "inference failed recently; retrying")
    if state == "failed":
        reason = getattr(model_info, "reason_code", None) or "MODEL_UNAVAILABLE"
        detail = (
            getattr(model_info, "detail", None)
            or model_error
            or ("the detection model is unavailable — run `make model` and reload")
        )
        code = reason
        if model_error and ":" in model_error:
            code = model_error.split(":", 1)[0].strip()
        return Check(FAILED, code, detail)
    return Check(DEGRADED, "MODEL_DISABLED", "detection is disabled; no events will be produced")


def build_probe_session_factory(settings: Any) -> Any:
    """A session factory for health probes (its own engine, read-only usage)."""
    from ..db.engine import build_engine, build_session_factory

    return build_session_factory(build_engine(settings.database_url))


__all__ = [
    "DEGRADED",
    "FAILED",
    "HEALTHY",
    "MIN_FREE_DISK_BYTES",
    "Check",
    "DatabaseProbe",
    "ModelProbe",
    "Probe",
    "SessionProbe",
    "StorageProbe",
    "SystemService",
]
