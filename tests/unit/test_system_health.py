"""Health aggregation, required/optional components, timeouts and safe messages.

These are the guarantees the badge depends on. The original defect — a healthy
system reported as ``degraded`` — is pinned by
:meth:`TestAggregation.test_a_fully_healthy_system_is_healthy`.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from visionops.services.system_service import (
    DEGRADED,
    FAILED,
    HEALTHY,
    OK,
    Check,
    DatabaseProbe,
    SystemService,
    aggregate_verdict,
)


class _FakeSessions:
    capacity = 4

    def states(self) -> list[Any]:
        return []


class _ModelInfo:
    name = "yolo11n"

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "state": "ready"}


def build_service(tmp_path: Path, **kwargs: Any) -> SystemService:
    settings = SimpleNamespace(
        database_url="sqlite:///:memory:",
        snapshot_dir=tmp_path,
        data_dir=tmp_path,
        env="test",
    )
    return SystemService(
        settings=settings,
        session_manager=_FakeSessions(),
        version="0.1.0",
        started_at=time.time(),
        **kwargs,
    )


class TestRank:
    def test_ok_and_healthy_share_the_lowest_rank(self) -> None:
        assert Check(OK).rank == Check(HEALTHY).rank == 0

    def test_ordering_is_ok_lt_degraded_lt_failed(self) -> None:
        assert Check(OK).rank < Check(DEGRADED).rank < Check(FAILED).rank


class TestAggregation:
    def test_everything_ok_is_healthy(self) -> None:
        checks = {"database": Check(OK, required=True), "model": Check(OK)}
        assert aggregate_verdict(checks) == HEALTHY

    def test_a_required_failure_is_failed(self) -> None:
        checks = {"database": Check(FAILED, "DB_UNREACHABLE", required=True)}
        assert aggregate_verdict(checks) == FAILED

    def test_an_optional_failure_is_degraded_not_failed(self) -> None:
        checks = {"database": Check(OK, required=True), "model": Check(FAILED, "MODEL_MISSING")}
        assert aggregate_verdict(checks) == DEGRADED

    def test_a_required_degradation_is_degraded(self) -> None:
        checks = {"database": Check(DEGRADED, "DB_SLOW", required=True)}
        assert aggregate_verdict(checks) == DEGRADED

    def test_a_fully_healthy_system_is_healthy(self, tmp_path: Path) -> None:
        """Regression: probe state ``ok`` used to rank as degraded (the bug)."""
        service = build_service(tmp_path)
        service.set_model_providers(lambda: "ready", lambda: _ModelInfo())
        assert service.status()["status"] == HEALTHY

    def test_status_is_serialisable_and_flags_required(self, tmp_path: Path) -> None:
        checks = build_service(tmp_path).check_all()
        assert set(checks) >= {"database", "storage", "model", "sessions"}
        assert checks["database"].required is True
        assert checks["model"].required is False
        assert checks["sessions"].required is False
        assert set(checks["database"].to_dict()) == {"state", "reason_code", "detail", "required"}


class TestProbeResilience:
    def test_a_raising_probe_is_contained_and_sanitised(self, tmp_path: Path) -> None:
        class BoomProbe:
            required = False
            timeout_seconds = None

            def check(self) -> Check:
                raise RuntimeError("postgresql://user:hunter2@host/db refused")

        service = build_service(tmp_path)
        service.register_probe(BoomProbe())
        result = service.check_all()["boom"]
        assert result.state == DEGRADED
        assert result.reason_code == "PROBE_ERROR"
        assert "hunter2" not in (result.detail or "")

    def test_a_slow_probe_times_out_quickly(self, tmp_path: Path) -> None:
        class SlowProbe:
            required = False
            timeout_seconds = 0.05

            def check(self) -> Check:
                time.sleep(0.5)
                return Check(OK)

        service = build_service(tmp_path)
        service.register_probe(SlowProbe())
        started = time.monotonic()
        result = service.check_all()["slow"]
        assert time.monotonic() - started < 0.5
        assert result.state == DEGRADED
        assert result.reason_code == "HEALTH_TIMEOUT"

    def test_an_exhausted_budget_skips_remaining_probes(self, tmp_path: Path) -> None:
        service = build_service(tmp_path, deadline_seconds=-1.0)
        checks = service.check_all()
        assert checks  # at least one key exists
        assert all(check.reason_code == "HEALTH_TIMEOUT" for check in checks.values())

    def test_database_failure_detail_never_leaks_the_dsn(self) -> None:
        class BoomFactory:
            def __call__(self) -> Any:
                raise RuntimeError("could not connect to postgresql://user:hunter2@db/prod")

        check = DatabaseProbe(BoomFactory()).check()
        assert check.state == FAILED
        assert check.reason_code == "DB_UNREACHABLE"
        assert check.required is True
        assert "hunter2" not in (check.detail or "")
        assert "postgresql" not in (check.detail or "")
