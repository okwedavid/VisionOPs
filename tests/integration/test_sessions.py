"""Session lifecycle and the live WebSocket surface.

The session path is where the fake model, the real database and the real
WebSocket transport meet, so these tests deliberately use the full application
lifespan rather than unit-level seams.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import pytest
from tests.conftest import make_frame

FIXTURE_VIDEO = "fixtures/zone-walkthrough.mp4"  # relative to VISIONOPS_MEDIA_ROOT


#: A module-level ``client`` that shadows the shared one with the scripted-model
#: variant, so every test in this file gets a container that can actually run a
#: pipeline. The plain fixture stays untouched for the rest of the suite.
@pytest.fixture
def client(session_client: Any) -> Any:
    return session_client


def _wait_for(predicate: Any, timeout: float = 5.0, interval: float = 0.05) -> bool:
    """Poll ``predicate`` until it is truthy or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _frames_processed(client: Any, auth: dict[str, str], camera_id: str) -> int:
    """Read ``frames_processed`` for a camera, or ``-1`` when unavailable.

    These tests poll a rate-limited endpoint far faster than any client would,
    so a ``429`` here is the test outrunning the limiter rather than a product
    defect: it must be treated as "not yet" instead of exploding with a
    ``KeyError`` on a problem document.
    """
    response = client.get(f"/api/v1/cameras/{camera_id}/session", headers=auth)
    if response.status_code != 200:
        return -1
    body = response.json()
    return int(body["frames_processed"]) if "frames_processed" in body else -1


def _settle(target_fps: float = 30.0) -> None:
    """Wait out one pipeline sampling interval.

    The pipeline is a *sampler*: frames that arrive faster than the camera's
    ``target_fps`` are counted in ``frames_dropped`` and never inferred. That is
    the intended behaviour for a live view (freshness beats completeness), so a
    test that wants a second frame *processed* has to respect the interval
    rather than push two frames back to back.
    """
    time.sleep(1.5 / target_fps)


@pytest.fixture
def camera_row(client: Any, auth: dict[str, str]) -> dict[str, Any]:
    """A real camera with a file source, so a session can actually start."""
    site = client.post("/api/v1/sites", headers=auth, json={"name": "E2E Site"})
    assert site.status_code == 201, site.text
    site_id = site.json()["id"]

    camera = client.post(
        "/api/v1/cameras",
        headers=auth,
        json={"name": "front-door", "site_id": site_id, "target_fps": 30.0},
    )
    assert camera.status_code == 201, camera.text
    camera_id = camera.json()["id"]

    source = client.post(
        f"/api/v1/cameras/{camera_id}/sources",
        headers=auth,
        json={"name": "clip", "kind": "file", "target": FIXTURE_VIDEO},
    )
    assert source.status_code == 201, source.text
    return {"site_id": site_id, "camera_id": camera_id, "source_id": source.json()["id"]}


@pytest.fixture
def zone(client: Any, auth: dict[str, str], camera_row: dict[str, Any]) -> dict[str, Any]:
    response = client.post(
        "/api/v1/zones",
        headers=auth,
        json={
            "camera_id": camera_row["camera_id"],
            "name": "restricted",
            "kind": "polygon",
            "points": [[0.05, 0.05], [0.95, 0.05], [0.95, 0.95], [0.05, 0.95]],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------------------
# Session lifecycle over HTTP
# ---------------------------------------------------------------------------


class TestSessionLifecycle:
    def test_starting_a_session_returns_a_running_session(
        self, client: Any, auth: dict[str, str], camera_row: dict[str, Any]
    ) -> None:
        response = client.post(
            f"/api/v1/cameras/{camera_row['camera_id']}/session",
            headers=auth,
            json={"source_id": camera_row["source_id"]},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["state"] in ("starting", "running")
        assert body["session_id"]
        assert body["camera_id"] == camera_row["camera_id"]

    def test_starting_twice_is_idempotent_not_a_conflict(
        self, client: Any, auth: dict[str, str], camera_row: dict[str, Any]
    ) -> None:
        """A double-clicked Start button must not spawn two pipelines."""
        payload = {"source_id": camera_row["source_id"]}
        first = client.post(
            f"/api/v1/cameras/{camera_row['camera_id']}/session",
            headers=auth,
            json=payload,
        )
        second = client.post(
            f"/api/v1/cameras/{camera_row['camera_id']}/session",
            headers=auth,
            json=payload,
        )
        assert first.status_code == 201
        assert second.status_code == 200, second.text
        assert second.json()["session_id"] == first.json()["session_id"]

    def test_state_is_readable_and_reports_frames(
        self, client: Any, auth: dict[str, str], camera_row: dict[str, Any]
    ) -> None:
        started = client.post(
            f"/api/v1/cameras/{camera_row['camera_id']}/session",
            headers=auth,
            json={"source_id": camera_row["source_id"]},
        )
        session_id = started.json()["session_id"]

        assert _wait_for(
            lambda: (
                _frames_processed(client, auth, camera_row["camera_id"]) > 0
            )
        ), "the session never processed a frame"

        state = client.get(
            f"/api/v1/cameras/{camera_row['camera_id']}/session", headers=auth
        ).json()
        assert state["frames_processed"] > 0
        assert state["session_id"] == session_id

    def test_stopping_a_session_is_idempotent(
        self, client: Any, auth: dict[str, str], camera_row: dict[str, Any]
    ) -> None:
        client.post(
            f"/api/v1/cameras/{camera_row['camera_id']}/session",
            headers=auth,
            json={"source_id": camera_row["source_id"]},
        )
        first = client.delete(f"/api/v1/cameras/{camera_row['camera_id']}/session", headers=auth)
        second = client.delete(f"/api/v1/cameras/{camera_row['camera_id']}/session", headers=auth)
        assert first.status_code == 204
        assert second.status_code == 204, second.text

    def test_a_stopped_session_no_longer_reports_state(
        self, client: Any, auth: dict[str, str], camera_row: dict[str, Any]
    ) -> None:
        """After a stop there is no session to describe, so it is a 404."""
        client.post(
            f"/api/v1/cameras/{camera_row['camera_id']}/session",
            headers=auth,
            json={"source_id": camera_row["source_id"]},
        )
        assert (
            client.get(
                f"/api/v1/cameras/{camera_row['camera_id']}/session", headers=auth
            ).status_code
            == 200
        )

        client.delete(f"/api/v1/cameras/{camera_row['camera_id']}/session", headers=auth)
        gone = client.get(f"/api/v1/cameras/{camera_row['camera_id']}/session", headers=auth)
        assert gone.status_code == 404
        assert gone.json()["code"] == "not_found"

    def test_a_session_for_an_unknown_camera_is_a_404(
        self, client: Any, auth: dict[str, str]
    ) -> None:
        response = client.post("/api/v1/cameras/does-not-exist/session", headers=auth, json={})
        assert response.status_code == 404
        assert response.json()["code"] == "not_found"

    def test_a_source_from_another_organisation_is_refused(
        self, client: Any, auth: dict[str, str], camera_row: dict[str, Any]
    ) -> None:
        missing = client.post(
            f"/api/v1/cameras/{camera_row['camera_id']}/session",
            headers=auth,
            json={"source_id": "01920000-0000-7000-8000-000000000000"},
        )
        assert missing.status_code in (403, 404)

    def test_a_session_needs_a_permission(
        self, client: Any, camera_row: dict[str, Any], mint_key: Any
    ) -> None:
        """Starting a session starts an inference workload: it needs a grant."""
        headers = {"X-API-Key": mint_key(["camera.read"], name="reader3")}
        response = client.post(
            f"/api/v1/cameras/{camera_row['camera_id']}/session", headers=headers, json={}
        )
        assert response.status_code == 403


class TestZoneHotReload:
    def test_a_new_zone_reaches_a_running_session_without_a_restart(
        self, client: Any, auth: dict[str, str], camera_row: dict[str, Any]
    ) -> None:
        """Operators must not have to stop processing to fix a zone."""
        client.post(
            f"/api/v1/cameras/{camera_row['camera_id']}/session",
            headers=auth,
            json={"source_id": camera_row["source_id"]},
        )
        assert _wait_for(
            lambda: (
                _frames_processed(client, auth, camera_row["camera_id"]) > 0
            )
        )
        created = client.post(
            "/api/v1/zones",
            headers=auth,
            json={
                "camera_id": camera_row["camera_id"],
                "name": "late-arrival",
                "kind": "polygon",
                "points": [[0.1, 0.1], [0.4, 0.1], [0.4, 0.4], [0.1, 0.4]],
            },
        )
        assert created.status_code == 201
        listed = client.get(
            f"/api/v1/zones?camera_id={camera_row['camera_id']}", headers=auth
        ).json()
        assert any(zone["id"] == created.json()["id"] for zone in listed)


# ---------------------------------------------------------------------------
# Live state WebSocket
# ---------------------------------------------------------------------------


class TestLiveStateSocket:
    @staticmethod
    def _running(client: Any, auth: dict[str, str], camera_row: dict[str, Any]) -> str:
        response = client.post(
            f"/api/v1/cameras/{camera_row['camera_id']}/session",
            headers=auth,
            json={"source_id": camera_row["source_id"]},
        )
        assert response.status_code == 201, response.text
        return str(response.json()["session_id"])

    def test_the_socket_sends_state_immediately_on_connect(
        self, client: Any, auth: dict[str, str], camera_row: dict[str, Any], api_key: str
    ) -> None:
        """A blank overlay is the classic symptom of this being missing."""
        session_id = self._running(client, auth, camera_row)
        with client.websocket_connect(
            f"/api/v1/stream/sessions/{session_id}?key={api_key}"
        ) as socket:
            payload = json.loads(socket.receive_text())
        # The snapshot must use the broadcast shape, or a client that only reads
        # `type == "session.state"` discards it and the overlay stays blank.
        assert payload["type"] == "session.state"
        assert payload["session_id"] == session_id
        assert "stats" in payload
        assert payload["stats"]["state"] in ("starting", "running", "idle")
        assert isinstance(payload["stats"]["frames_processed"], int)

    def test_the_socket_requires_authentication(
        self, client: Any, auth: dict[str, str], camera_row: dict[str, Any]
    ) -> None:
        session_id = self._running(client, auth, camera_row)
        from starlette.websockets import WebSocketDisconnect

        url = f"/api/v1/stream/sessions/{session_id}"
        with pytest.raises(WebSocketDisconnect), client.websocket_connect(url) as socket:
            socket.receive_text()

    def test_an_unknown_session_is_refused(self, client: Any, api_key: str) -> None:
        from starlette.websockets import WebSocketDisconnect

        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(
                f"/api/v1/stream/sessions/01920000-0000-7000-8000-000000000000?key={api_key}"
            ) as socket,
        ):
            socket.receive_text()

    def test_stats_keep_arriving_while_a_session_runs(
        self, client: Any, auth: dict[str, str], camera_row: dict[str, Any], api_key: str
    ) -> None:
        session_id = self._running(client, auth, camera_row)
        received: list[dict[str, Any]] = []
        with client.websocket_connect(
            f"/api/v1/stream/sessions/{session_id}?key={api_key}"
        ) as socket:
            received.append(json.loads(socket.receive_text()))
            for _ in range(3):
                received.append(json.loads(socket.receive_text()))
        counts = [int(message.get("stats", {}).get("frames_processed", 0)) for message in received]
        assert any(count > 0 for count in counts), f"no frames reported: {counts}"


# ---------------------------------------------------------------------------
# Browser ingest WebSocket
# ---------------------------------------------------------------------------


class TestBrowserIngest:
    def test_the_browser_can_push_frames_and_they_are_processed(
        self, client: Any, auth: dict[str, str], api_key: str
    ) -> None:
        """The no-camera path: the browser *is* the capture device.

        The socket speaks binary JPEG frames after a ``ingest.hello`` text
        greeting, exactly as the page does.
        """
        camera_id, ingest_url = self._browser_session(client, auth)
        session_id = ingest_url.rsplit("session_id=", 1)[-1]

        with client.websocket_connect(f"{ingest_url}&key={api_key}") as socket:
            hello = json.loads(socket.receive_text())
            assert hello["type"] == "ingest.hello"
            assert hello["session_id"] == session_id

            frame = make_frame(1)
            for _ in range(5):
                socket.send_bytes(_jpeg_bytes(frame))

            assert _wait_for(
                lambda: (
                    _frames_processed(client, auth, camera_id) > 0
                )
            ), "frames pushed over the socket were never processed"

    def test_a_malformed_frame_is_counted_and_never_processed(
        self, client: Any, auth: dict[str, str], api_key: str
    ) -> None:
        """Garbage must be dropped and *counted*, not crash the session thread.

        The socket deliberately stays open: one bad frame from a browser is not
        a reason to tear down a camera an operator is watching.
        """
        camera_id, ingest_url = self._browser_session(client, auth)

        with client.websocket_connect(f"{ingest_url}&key={api_key}") as socket:
            assert json.loads(socket.receive_text())["type"] == "ingest.hello"
            for _ in range(20):
                socket.send_bytes(b"!!!not-a-jpeg!!!" * 32)

            # A good frame afterwards must still be accepted: the session
            # survived the garbage rather than wedging.
            socket.send_bytes(_jpeg_bytes(make_frame(1)))
            assert _wait_for(
                lambda: (
                    _frames_processed(client, auth, camera_id) > 0
                )
            ), "the session did not recover after malformed frames"

        state = client.get(f"/api/v1/cameras/{camera_id}/session", headers=auth).json()
        assert state["source"]["reason_code"] is None

    def test_ingest_requires_authentication(self, client: Any, auth: dict[str, str]) -> None:
        _camera_id, ingest_url = self._browser_session(client, auth)

        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect), client.websocket_connect(ingest_url) as socket:
            socket.receive_text()

    def test_ingest_is_refused_for_a_file_backed_session(
        self, client: Any, auth: dict[str, str], camera_row: dict[str, Any], api_key: str
    ) -> None:
        """Only a browser source may be fed by a client."""
        started = client.post(
            f"/api/v1/cameras/{camera_row['camera_id']}/session",
            headers=auth,
            json={"source_id": camera_row["source_id"]},
        )
        session_id = started.json()["session_id"]

        from starlette.websockets import WebSocketDisconnect

        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(
                f"/api/v1/stream/ingest?session_id={session_id}&key={api_key}"
            ) as socket,
        ):
            socket.receive_text()

    @staticmethod
    def _browser_session(client: Any, auth: dict[str, str]) -> tuple[str, str]:
        site = client.post("/api/v1/sites", headers=auth, json={"name": "Browser Site"})
        camera = client.post(
            "/api/v1/cameras",
            headers=auth,
            json={"name": "laptop-cam", "site_id": site.json()["id"], "target_fps": 30.0},
        )
        assert camera.status_code == 201, camera.text
        camera_id = camera.json()["id"]
        source = client.post(
            f"/api/v1/cameras/{camera_id}/sources",
            headers=auth,
            json={"name": "webcam", "kind": "browser", "target": "browser"},
        )
        assert source.status_code == 201, source.text
        started = client.post(
            f"/api/v1/cameras/{camera_id}/session",
            headers=auth,
            json={"source_id": source.json()["id"]},
        )
        assert started.status_code == 201, started.text
        body = started.json()
        assert body["ingest_ws_url"], "the page must be told where to push frames"
        return camera_id, str(body["ingest_ws_url"])


def _jpeg_bytes(frame: Any) -> bytes:
    """Encode a :class:`Frame` as JPEG bytes, the wire format the page sends."""
    import cv2

    ok, buffer = cv2.imencode(".jpg", frame.image)
    assert ok
    return buffer.tobytes()


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


class TestConcurrencySafety:
    def test_parallel_session_starts_create_exactly_one_session(
        self, client: Any, auth: dict[str, str], camera_row: dict[str, Any]
    ) -> None:
        """A race here would double GPU/CPU load for one camera."""
        results: list[int] = []
        lock = threading.Lock()

        def start() -> None:
            response = client.post(
                f"/api/v1/cameras/{camera_row['camera_id']}/session",
                headers=auth,
                json={"source_id": camera_row["source_id"]},
            )
            with lock:
                results.append(response.status_code)

        threads = [threading.Thread(target=start) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert results, "no thread ran"
        assert set(results) <= {200, 201}, f"unexpected statuses: {results}"
        sessions = client.get(
            f"/api/v1/cameras/{camera_row['camera_id']}/session", headers=auth
        ).json()
        assert sessions["session_id"]


class TestBrowserReconnect:
    """Frames already received must survive a client disconnect.

    Closing a tab is exactly when an operator is watching, and the previous
    behaviour discarded the frames that had already arrived.
    """

    def test_frames_pushed_before_a_disconnect_are_still_processed(
        self, client: Any, auth: dict[str, str], api_key: str
    ) -> None:
        camera_id, ingest_url = TestBrowserIngest._browser_session(client, auth)
        session_id = ingest_url.rsplit("session_id=", 1)[-1]
        assert len(session_id) >= 8, "the ingest URL must carry the session it feeds"

        with client.websocket_connect(f"{ingest_url}&key={api_key}") as socket:
            assert json.loads(socket.receive_text())["type"] == "ingest.hello"
            for index in range(5):
                socket.send_bytes(_jpeg_bytes(make_frame(index + 1)))

        # The socket is closed here. Whatever arrived before it closed is real.
        assert _wait_for(
            lambda: _frames_processed(client, auth, camera_id) > 0
        ), "frames received before the disconnect were thrown away"

    def test_a_reconnecting_browser_resumes_the_same_session(
        self, client: Any, auth: dict[str, str], api_key: str
    ) -> None:
        camera_id, ingest_url = TestBrowserIngest._browser_session(client, auth)

        first = client.get(f"/api/v1/cameras/{camera_id}/session", headers=auth).json()
        assert first["session_id"]

        with client.websocket_connect(f"{ingest_url}&key={api_key}") as socket:
            assert json.loads(socket.receive_text())["type"] == "ingest.hello"
            socket.send_bytes(_jpeg_bytes(make_frame(1)))
            assert _wait_for(lambda: _frames_processed(client, auth, camera_id) > 0)

        # Reconnecting must not require restarting the session.
        _settle()
        with client.websocket_connect(f"{ingest_url}&key={api_key}") as socket:
            assert json.loads(socket.receive_text())["type"] == "ingest.hello"
            socket.send_bytes(_jpeg_bytes(make_frame(2)))
            assert _wait_for(
                lambda: _frames_processed(client, auth, camera_id) > 1
            ), "the session did not accept frames after the browser reconnected"
