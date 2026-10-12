"""Health endpoints under real request conditions: the liveness/readiness split.

The badge reads ``/readyz``; these tests pin the contract it relies on and the
promise that one camera failing never makes the platform look offline.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from visionops.services.system_service import FAILED, Check, DatabaseProbe, SessionProbe


def _iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


class TestReadyzContract:
    def test_readyz_carries_a_timestamp_and_required_flags(self, client: Any) -> None:
        payload = client.get("/api/v1/readyz").json()
        assert _iso(payload["timestamp"]).tzinfo is not None
        checks = payload["checks"]
        assert checks["api"]["state"] == "ok"
        assert checks["api"]["required"] is True
        assert checks["database"]["required"] is True
        assert checks["model"]["required"] is False
        assert checks["sessions"]["required"] is False

    def test_a_healthy_system_is_ready(self, session_client: Any) -> None:
        payload = session_client.get("/api/v1/readyz").json()
        assert payload["status"] == "ready"
        assert all(check["state"] == "ok" for check in payload["checks"].values())


class TestRequiredVsOptional:
    def test_a_database_failure_is_503_but_liveness_stays_200(
        self, client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            DatabaseProbe,
            "check",
            lambda self: Check(FAILED, "DB_UNREACHABLE", "the database is unreachable"),
        )
        assert client.get("/api/v1/healthz").status_code == 200

        response = client.get("/api/v1/readyz")
        assert response.status_code == 503
        payload = response.json()
        assert payload["status"] == "unavailable"
        assert payload["checks"]["database"]["state"] == "failed"
        assert payload["checks"]["api"]["state"] == "ok"

    def test_the_status_page_still_renders_when_the_database_is_down(
        self, client: Any, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            DatabaseProbe,
            "check",
            lambda self: Check(FAILED, "DB_UNREACHABLE", "the database is unreachable"),
        )
        response = client.get("/api/v1/system/status", headers=auth)
        assert response.status_code == 200
        assert response.json()["status"] == "failed"

    def test_a_failing_camera_degrades_but_never_offlines_the_platform(
        self, session_client: Any, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            SessionProbe,
            "check",
            lambda self: Check(FAILED, "SOURCE_STALLED", "front-door: the source stalled"),
        )
        status = session_client.get("/api/v1/system/status", headers=auth).json()
        assert status["status"] == "degraded"
        assert status["checks"]["sessions"]["state"] == "failed"

        ready = session_client.get("/api/v1/readyz")
        assert ready.status_code == 200
        assert ready.json()["status"] == "degraded"
