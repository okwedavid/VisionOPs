"""System status: the human-facing health aggregate behind the System page.

Every subsystem reports ``(state, reason_code, detail)``. The aggregate turns
those into one verdict. Reason codes are part of the public contract, so clients
may switch on them (``docs/OBSERVABILITY.md`` §4).

Two distinctions matter and are enforced here:

* **Liveness vs readiness.** ``/healthz`` is answered by the process alone;
  ``/readyz`` aggregates dependency probes. A slow dependency must never make a
  liveness probe fail.
* **Required vs optional.** Losing the database makes the API *unready* (503).
  Losing the model, snapshots or a single camera session merely *degrades* the
  platform — the dashboard, history and export keep working.
"""

from __future__ import annotations

import contextlib
import logging
import shutil
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, replace
from datetime import UTC
from typing import Any, Protocol

from sqlalchemy import text

from ..observability.metrics import (
    cpu_seconds,
    registry,
    resident_memory_bytes,
)

logger = logging.getLogger(__name__)

#: Probe-level "all good" state. Distinct from the aggregate :data:`HEALTHY`
#: verdict, but of equal severity — the original bug was ranking them apart.
OK = "ok"
HEALTHY = "healthy"
DEGRADED = "degraded"
FAILED = "failed"
MIN_FREE_DISK_BYTES = 500 * 1024 * 1024

#: Seconds a database probe may wait before it is reported as timed out. Kept
#: below the overall probe deadline and well under any HTTP proxy timeout.
DATABASE_PROBE_TIMEOUT_SECONDS = 2.0
#: Busy timeout for the probe engine's SQLite lock wait (below the thread bound).
DATABASE_PROBE_BUSY_TIMEOUT_MS = 1000
#: Wall-clock budget for the whole probe sweep; probes not started in time are
#: reported as timed out rather than blocking the health endpoint.
DEFAULT_PROBE_DEADLINE_SECONDS = 8.0

_SEVERITY_RANK = {OK: 0, HEALTHY: 0, DEGRADED: 1, FAILED: 2}


@dataclass(frozen=True, slots=True)
class Check:
    """One subsystem's health."""

    state: str
    reason_code: str | None = None
    detail: str | None = None
    required: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "reason_code": self.reason_code,
            "detail": self.detail,
            "required": self.required,
        }

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK.get(self.state, 1)


class Probe(Protocol):
    """A subsystem health check. Must never raise."""

    name: str
    required: bool
    timeout_seconds: float | None

    def check(self) -> Check: ...


class DatabaseProbe:
    """Connectivity only — a readiness probe must not be slow or destructive.

    The failure detail is deliberately generic: the underlying exception can
    echo a connection string (and therefore credentials) for Postgres.
    """

    name = "database"
    required = True
    timeout_seconds: float | None = DATABASE_PROBE_TIMEOUT_SECONDS

    def __init__(self, session_factory: Any, *, timeout_seconds: float | None = None) -> None:
        self._session_factory = session_factory
        if timeout_seconds is not None:
            self.timeout_seconds = timeout_seconds

    def check(self) -> Check:
        started = time.perf_counter()
        try:
            with self._session_factory() as session:
                session.execute(text("SELECT 1"))
        except Exception:
            logger.warning("database health probe failed", exc_info=True)
            return Check(FAILED, "DB_UNREACHABLE", "the database is unreachable", required=True)
        return Check(OK, None, f"{(time.perf_counter() - started) * 1000:.1f} ms", required=True)


class StorageProbe:
    """Free space and writability of the snapshot directory."""

    name = "storage"
    required = False
    timeout_seconds: float | None = None

    def __init__(self, snapshot_dir: Any, *, enabled: bool) -> None:
        self._dir = snapshot_dir
        self._enabled = enabled

    def check(self) -> Check:
        if not self._enabled:
            return Check(OK, None, "snapshots disabled (privacy default)")
        try:
            usage = __import__("shutil").disk_usage(str(self._dir))
        except OSError:
            logger.warning("snapshot directory is not statable", exc_info=True)
            return Check(DEGRADED, "STORAGE_UNAVAILABLE", "the snapshot directory is not writable")
        free = usage.free
        if free < MIN_FREE_DISK_BYTES:
            return Check(
                DEGRADED,
                "DISK_LOW",
                f"only {free / 1024 / 1024:.0f} MB free — run `visionops prune`",
            )
        return Check(OK, None, f"{free / 1024 / 1024 / 1024:.1f} GB free")


class ModelProbe:
    """Reports whatever the model loader last observed, including failure."""

    name = "model"
    required = False
    timeout_seconds: float | None = None

    def __init__(self, model_state_provider: Callable[[], Check]) -> None:
        self._provider = model_state_provider

    def check(self) -> Check:
        return self._provider()


class SessionProbe:
    """Worst state across all running sessions.

    A single camera failing must degrade the *platform* report, never make it
    look offline: sessions are optional, per-source failures are local.
    """

    name = "sessions"
    required = False
    timeout_seconds: float | None = None

    def __init__(self, session_manager: Any) -> None:
        self._manager = session_manager

    def check(self) -> Check:
        states = self._manager.states()
        if not states:
            return Check(OK, None, "no active sessions")
        worst = Check(OK)
        for state in states:
            if state.state == FAILED:
                candidate = Check(FAILED, state.reason_code, f"{state.camera_name}: {state.detail}")
            elif state.state == DEGRADED:
                candidate = Check(
                    DEGRADED, state.reason_code, f"{state.camera_name}: {state.detail}"
                )
            else:
                continue
            if candidate.rank > worst.rank:
                worst = candidate
        return worst


def aggregate_verdict(checks: dict[str, Check]) -> str:
    """Collapse per-subsystem checks into one verdict.

    * a *required* component failing → ``failed`` (the API cannot serve).
    * anything degraded, or an *optional* component failing → ``degraded``.
    * everything healthy → ``healthy``.
    """
    if any(check.required and check.state == FAILED for check in checks.values()):
        return FAILED
    if any(check.rank > _SEVERITY_RANK[HEALTHY] for check in checks.values()):
        return DEGRADED
    return HEALTHY


def _run_with_timeout(probe: Probe, name: str) -> Check:
    """Run a probe, bounding a slow one without leaking a blocking request."""
    timeout = getattr(probe, "timeout_seconds", None)
    if timeout is None:
        return probe.check()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"visionops-probe-{name}")
    try:
        future = executor.submit(probe.check)
        try:
            return future.result(timeout=timeout)
        except FuturesTimeoutError:
            future.cancel()
            logger.warning("health probe %s timed out after %.1fs", name, timeout)
            return Check(DEGRADED, "HEALTH_TIMEOUT", f"{name} check exceeded {timeout:.0f}s")
    finally:
        # ``wait=False``: a probe thread that hung on a dead socket must not
        # block the response. Executor threads are daemons, so it cannot keep
        # the process alive either.
        executor.shutdown(wait=False, cancel_futures=True)


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
        model_state_provider: Callable[[], str] | None = None,
        model_info_provider: Callable[[], Any] | None = None,
        deadline_seconds: float = DEFAULT_PROBE_DEADLINE_SECONDS,
    ) -> None:
        self._settings = settings
        self._sessions = session_manager
        self._version = version
        self._started_at = started_at
        self._encryption_state = encryption_state
        self._snapshots_enabled = snapshots_enabled
        self._model_error = model_error
        self._model_state = model_state_provider or (lambda: "unconfigured")
        self._model_info = model_info_provider or (lambda: None)
        self._deadline_seconds = deadline_seconds
        self._probes: list[Probe] = [
            DatabaseProbe(build_probe_session_factory(settings)),
            StorageProbe(settings.snapshot_dir, enabled=snapshots_enabled),
            ModelProbe(self._model_check),
            SessionProbe(session_manager),
        ]

    def register_probe(self, probe: Probe) -> None:
        self._probes.append(probe)

    def set_model_providers(
        self,
        state_provider: Callable[[], str],
        info_provider: Callable[[], Any],
    ) -> None:
        """Point the model probe at the container's *live* model references.

        Wired after the container exists so a model swapped in at runtime (or by
        a test) is reflected without reconstructing the service.
        """
        self._model_state = state_provider
        self._model_info = info_provider

    def _model_check(self) -> Check:
        return _model_check(self._model_state(), self._model_info(), self._model_error)

    def check_all(self) -> dict[str, Check]:
        results: dict[str, Check] = {}
        deadline = time.monotonic() + self._deadline_seconds
        for probe in self._probes:
            name = getattr(probe, "name", type(probe).__name__.removesuffix("Probe").lower())
            required = bool(getattr(probe, "required", False))
            if time.monotonic() > deadline:
                results[name] = Check(
                    DEGRADED,
                    "HEALTH_TIMEOUT",
                    "health checks exceeded their time budget",
                    required=required,
                )
                continue
            try:
                check = _run_with_timeout(probe, name)
            except Exception:
                # A probe raising is a bug in the probe, not proof the subsystem
                # is down; report it degraded with a generic, safe message.
                logger.warning("health probe %s raised", name, exc_info=True)
                check = Check(DEGRADED, "PROBE_ERROR", "the probe raised an unexpected error")
            results[name] = replace(check, required=required)
        return results

    def status(self) -> dict[str, Any]:
        checks = self.check_all()
        sessions = [state.to_dict() for state in self._sessions.states()]
        model_info = self._model_info()
        model_check = checks.get("model", self._model_check())
        verdict = aggregate_verdict(checks)

        free_bytes = None
        with contextlib.suppress(OSError):  # pragma: no cover - platform dependent
            free_bytes = shutil.disk_usage(str(self._settings.data_dir)).free

        return {
            "status": verdict,
            "version": self._version,
            "environment": self._settings.env,
            "timestamp": _utc_now_iso(),
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

    def readiness(self) -> tuple[str, dict[str, Check]]:
        """Readiness verdict + per-component checks.

        Returns ``("unavailable", checks)`` when a required component failed,
        ``("degraded", checks)`` when an optional one did, else ``("ready", ...)``.
        A missing model makes this *degraded*, not *unready* — configuration,
        history and export still work, and the UI says so honestly.
        """
        checks = self.check_all()
        verdict = aggregate_verdict(checks)
        if verdict == FAILED:
            return "unavailable", checks
        if verdict == DEGRADED:
            return "degraded", checks
        return "ready", checks


def _utc_now_iso() -> str:
    from datetime import datetime

    return datetime.now(UTC).isoformat()


def _model_check(state: str, model_info: Any, model_error: str | None = None) -> Check:
    if state == "ready":
        return Check(OK, None, f"{model_info.name if model_info else 'model'} ready")
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
    """A session factory for health probes (its own engine, read-only usage).

    The engine gets a short busy timeout so a probe answers promptly during a
    write spike instead of inheriting the application's full 5 s wait.
    """
    from ..db.engine import build_engine, build_session_factory

    engine = build_engine(settings.database_url, busy_timeout_ms=DATABASE_PROBE_BUSY_TIMEOUT_MS)
    return build_session_factory(engine)


__all__ = [
    "DATABASE_PROBE_BUSY_TIMEOUT_MS",
    "DATABASE_PROBE_TIMEOUT_SECONDS",
    "DEFAULT_PROBE_DEADLINE_SECONDS",
    "DEGRADED",
    "FAILED",
    "HEALTHY",
    "MIN_FREE_DISK_BYTES",
    "OK",
    "Check",
    "DatabaseProbe",
    "ModelProbe",
    "Probe",
    "SessionProbe",
    "StorageProbe",
    "SystemService",
    "aggregate_verdict",
]
