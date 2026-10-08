"""Session lifecycle: the worker thread must never die quietly.

These cover the failure modes that are invisible from the API until an operator
reports "the camera shows nothing": a worker thread that dies on an unexpected
exception, and ended sessions that keep occupying the capacity gate.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest
from tests.conftest import CollectingSink, ScriptedVisionModel, make_pipeline


class ExplodingSource:
    """A source that raises from ``open`` — nothing the runner anticipates.

    ``pipeline.step`` already contains its own failures, so the gap this covers
    is everything *around* it: opening, reopening, and the bookkeeping between
    iterations. A path-traversal ``ValueError`` from the media-root check, an
    ``OSError`` from a capture library — anything at all.
    """

    kind = "file"

    def __init__(self) -> None:
        self.calls = 0
        self.closed = False

    def open(self) -> None:
        self.calls += 1
        raise ValueError("path '../../etc/passwd' resolves outside the allowed root")

    def read(self) -> Any:
        return None

    @property
    def at_end(self) -> bool:
        return False

    def close(self) -> None:
        self.closed = True

    def reopen(self) -> None:
        return None

    @property
    def health(self) -> Any:  # pragma: no cover - only touched on the failure path
        raise NotImplementedError


def _runner(source: Any, sink: Any) -> Any:
    from visionops.realtime.hub import RealtimeHub
    from visionops.services.session_service import SessionRunner, SessionState

    pipeline = make_pipeline(
        source=source, model=ScriptedVisionModel([[]]), sink=sink, target_fps=30.0
    )
    return SessionRunner(
        session_id="sess-exploding",
        camera_id="cam-exploding",
        camera_name="lobby",
        source=source,
        pipeline=pipeline,
        hub=RealtimeHub(),
        state=SessionState(
            session_id="sess-exploding",
            camera_id="cam-exploding",
            camera_name="lobby",
            source_kind="file",
        ),
    )


class TestUnexpectedErrorsAreDiagnoses:
    def test_the_worker_thread_survives_an_unexpected_exception(self) -> None:
        sink = CollectingSink()
        runner = _runner(ExplodingSource(), sink)

        runner.start()
        time.sleep(0.3)
        assert runner.is_alive(), "the worker thread died on an unexpected exception"
        assert runner.state.state == "degraded"
        assert runner.state.reason_code == "SESSION_ERROR"
        assert "server logs" in (runner.state.detail or "")

        runner.stop()
        assert runner.state.state == "stopped"

    def test_a_broken_session_ends_as_failed_not_stopped(self) -> None:
        """``SOURCE_STOPPED`` would tell an operator everything was fine.

        The lifecycle event is the artefact an operator reads the morning after,
        so a session that died of an internal error has to say so.
        """
        sink = CollectingSink()
        runner = _runner(ExplodingSource(), sink)
        runner.start()
        time.sleep(0.3)
        runner.stop()

        assert sink.types[-1] == "SOURCE_FAILED", sink.types
        assert sink.events[-1].metadata["reason_code"] == "SESSION_ERROR"

    def test_a_clean_stop_is_reported_as_stopped(self) -> None:
        """The mirror image: a normal stop must not be reported as a failure."""
        sink = CollectingSink()
        runner = _runner(_QuietSource(), sink)
        runner.start()
        time.sleep(0.2)
        runner.stop()
        assert sink.types[-1] == "SOURCE_STOPPED", sink.types

    def test_the_thread_is_idempotent_under_repeated_failures(self) -> None:
        """One bad frame must not turn into a busy loop of tracebacks."""
        source = ExplodingSource()
        runner = _runner(source, CollectingSink())
        runner.start()
        time.sleep(0.5)
        assert runner.is_alive()
        runner.stop()
        assert source.calls < 20, f"{source.calls} spins in half a second is a busy loop"


class TestCapacityReaping:
    """Ended sessions must stop occupying ``VISIONOPS_MAX_SESSIONS``."""

    @staticmethod
    def _manager(max_sessions: int = 1) -> Any:
        from visionops.realtime.hub import RealtimeHub
        from visionops.services.session_service import SessionManager

        model = ScriptedVisionModel([[]])
        return SessionManager(
            max_sessions=max_sessions,
            hub=RealtimeHub(),
            event_service=None,
            settings=None,
            model_info=model.info,
            model=model,
        )

    class _DeadRunner:
        def __init__(self) -> None:
            self.camera_id = "cam-dead"
            self.session_id = "sess-dead"
            self.state = threading.Event()

        def is_alive(self) -> bool:
            return False

    def test_a_dead_session_is_reaped_before_the_capacity_check(self) -> None:
        manager = self._manager(max_sessions=1)
        dead = TestCapacityReaping._DeadRunner()
        manager._sessions["sess-dead"] = dead  # type: ignore[assignment]
        manager._by_camera["cam-dead"] = "sess-dead"

        reaped = manager._reap()
        assert reaped == 1
        assert manager.active_count == 0
        assert manager.for_camera("cam-dead") is None

    def test_a_live_session_is_never_reaped(self) -> None:
        sink = CollectingSink()
        runner = _runner(ExplodingSource(), sink)
        manager = self._manager(max_sessions=1)
        manager._sessions["sess-live"] = runner  # type: ignore[assignment]
        manager._by_camera["cam-exploding"] = "sess-live"
        runner.start()
        try:
            time.sleep(0.2)
            assert manager._reap() == 0
            assert manager.active_count == 1
            assert manager.for_camera("cam-exploding") is runner
        finally:
            runner.stop()

    def test_states_are_safe_to_read_while_sessions_change(self) -> None:
        """``states()`` is called from HTTP threads; a concurrent start must not
        raise "dictionary changed size during iteration"."""
        manager = self._manager(max_sessions=4)
        errors: list[BaseException] = []
        stop = threading.Event()

        def reader() -> None:
            while not stop.is_set():
                try:
                    len(manager.states())
                    _ = manager.active_count
                except BaseException as exc:
                    errors.append(exc)
                    return

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        try:
            for index in range(200):
                manager._sessions[f"sess-{index}"] = TestCapacityReaping._DeadRunner()  # type: ignore[assignment]
                if index % 10 == 0:
                    manager._reap()
        finally:
            stop.set()
            thread.join(timeout=5)
        assert errors == [], f"concurrent reads raised: {errors[0]!r}"


class TestSessionStateIsAValue:
    def test_snapshot_state_keeps_the_broadcast_shape(self) -> None:
        """A late subscriber must get exactly what the broadcasts carry."""
        sink = CollectingSink()
        runner = _runner(ExplodingSource(), sink)
        state = runner.snapshot_state()
        assert state["type"] == "session.state"
        assert state["session_id"] == "sess-exploding"
        for key in ("source", "ai", "stats", "tracks", "events", "zones", "server_time"):
            assert key in state, f"missing {key} from the initial live-state message"


@pytest.mark.parametrize("capacity", [1, 2])
def test_capacity_is_reported(capacity: int) -> None:
    manager = TestCapacityReaping._manager(max_sessions=capacity)
    assert manager.capacity == capacity
    assert manager.active_count == 0


class _QuietSource:
    """A well-behaved source that simply never yields a frame."""

    kind = "file"

    def __init__(self) -> None:
        self.closed = False

    def open(self) -> None:
        return None

    def read(self) -> Any:
        time.sleep(0.02)
        return None

    @property
    def at_end(self) -> bool:
        return False

    def close(self) -> None:
        self.closed = True

    def reopen(self) -> None:
        return None

    @property
    def health(self) -> Any:  # pragma: no cover - only touched on the failure path
        raise NotImplementedError
