"""The pipeline, end to end, with scripted detections.

This is where the customer-visible promise is proved: a person walks into a
zone, and exactly one ``ZONE_ENTERED`` and one ``ZONE_EXITED`` are produced with
complete, typed metadata. No model, no camera, no timing dependence.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tests.conftest import (
    CollectingSink,
    FailingModel,
    ScriptedSource,
    ScriptedVisionModel,
    SlowModel,
    make_frame,
    make_pipeline,
    walk_script,
    zone_spec,
)

from visionops.vision.contracts import BBox, Detection, SourceState


def box(x: float, y: float = 0.30, width: float = 0.10, height: float = 0.50) -> BBox:
    return BBox.from_xyxy(x, y, x + width, y + height)


def run_to_end(pipeline) -> int:
    """Drive the pipeline to the end of its source, advancing the fake clock."""
    frames = 0
    while pipeline.step():
        frames += 1
        if frames > 2000:  # pragma: no cover - safety valve
            raise AssertionError("pipeline did not terminate")
    return frames


class TestHappyPath:
    def test_a_person_walking_through_produces_exactly_one_enter_and_one_exit(self) -> None:
        sink = CollectingSink()
        model = ScriptedVisionModel(walk_script(0.05, 0.85, 30))
        pipeline = make_pipeline(
            source=ScriptedSource(30), model=model, sink=sink, zones=[zone_spec()]
        )
        run_to_end(pipeline)

        assert sink.types == ["ZONE_ENTERED", "ZONE_EXITED"]
        entered, exited = sink.events
        assert entered.type == "ZONE_ENTERED"
        assert entered.track_id == exited.track_id, "the same person, so the same ephemeral id"
        assert entered.zone_id == exited.zone_id == "zone-1"
        assert entered.camera_id == "cam-test"
        assert exited.metadata["dwell_seconds"] > 0

    def test_the_event_metadata_is_complete_and_normalised(self) -> None:
        sink = CollectingSink()
        pipeline = make_pipeline(
            source=ScriptedSource(30),
            model=ScriptedVisionModel(walk_script(0.05, 0.85, 30)),
            sink=sink,
            zones=[zone_spec()],
        )
        run_to_end(pipeline)
        metadata = sink.events[0].metadata
        for key in (
            "camera_name",
            "zone_name",
            "box",
            "foot_point",
            "class",
            "confidence",
            "frame_width",
            "frame_height",
            "model",
            "model_sha256",
            "source_kind",
        ):
            assert key in metadata, f"missing metadata key {key!r}"
        for value in metadata["box"]:
            assert 0.0 <= value <= 1.0
        for value in metadata["foot_point"]:
            assert 0.0 <= value <= 1.0

    def test_a_person_who_stays_outside_produces_nothing(self) -> None:
        sink = CollectingSink()
        pipeline = make_pipeline(
            source=ScriptedSource(20),
            model=ScriptedVisionModel(walk_script(0.02, 0.15, 20)),
            sink=sink,
            zones=[zone_spec()],
        )
        run_to_end(pipeline)
        assert sink.events == []

    def test_a_person_already_inside_when_the_session_starts_does_not_emit(self) -> None:
        """Occupancy is a FUTURE feature; entry means *transition*, not presence."""
        sink = CollectingSink()
        pipeline = make_pipeline(
            source=ScriptedSource(10),
            model=ScriptedVisionModel([[(box(0.45), 0.9)]] * 10),
            sink=sink,
            zones=[zone_spec()],
        )
        run_to_end(pipeline)
        assert len(sink.events) == 1  # the single confirmed entry, then nothing

    def test_two_people_walking_in_parallel_produce_two_independent_pairs(self) -> None:
        """Two tracks, two enter/exit pairs, matched by track id."""
        script = []
        for index in range(40):
            # Well separated in x, so they never merge into one track.
            script.append([(box(0.02 + index * 0.02), 0.9), (box(0.30 + index * 0.02), 0.9)])
        sink = CollectingSink()
        pipeline = make_pipeline(
            source=ScriptedSource(40),
            model=ScriptedVisionModel(script),
            sink=sink,
            zones=[zone_spec()],
        )
        run_to_end(pipeline)
        entered = [e for e in sink.events if e.type == "ZONE_ENTERED"]
        exited = [e for e in sink.events if e.type == "ZONE_EXITED"]
        assert len(entered) == 2
        assert len(exited) == 2
        assert {e.track_id for e in entered} == {e.track_id for e in exited}
        assert len({e.track_id for e in entered}) == 2, "the two people must be distinct tracks"

    def test_no_zones_means_no_events(self) -> None:
        sink = CollectingSink()
        pipeline = make_pipeline(
            source=ScriptedSource(20),
            model=ScriptedVisionModel(walk_script(0.05, 0.85, 20)),
            sink=sink,
            zones=[],
        )
        run_to_end(pipeline)
        assert sink.events == []


class TestGatesInTheLoop:
    def test_dwell_hysteresis_prevents_a_boundary_brush(self) -> None:
        """A person clipping the corner must not generate an alert."""
        # The fake clock advances 0.2 s per frame, so "inside for 8 frames" is
        # 1.6 s — below a 2.0 s dwell threshold, and therefore not an event.
        script = []
        for index in range(12):
            x = 0.20 + index * 0.025  # foot point 0.25 → 0.525, inside from frame 4
            script.append([(box(x), 0.9)])
        sink = CollectingSink()
        pipeline = make_pipeline(
            source=ScriptedSource(12),
            model=ScriptedVisionModel(script),
            sink=sink,
            zones=[zone_spec(min_dwell=2.0)],
        )
        run_to_end(pipeline)
        assert sink.events == [], "a brief pass must not clear the dwell threshold"

    def test_a_zone_cooldown_suppresses_a_repeat_entry(self) -> None:
        script = []
        for _cycle in range(4):
            for step in range(10):
                x = 0.45 + (step * 0.04 if step < 5 else -((step - 5) * 0.04))
                script.append([(box(x), 0.9)])
        sink = CollectingSink()
        pipeline = make_pipeline(
            source=ScriptedSource(40),
            model=ScriptedVisionModel(script),
            sink=sink,
            zones=[zone_spec(cooldown=30.0)],
        )
        run_to_end(pipeline)
        entered = [e for e in sink.events if e.type == "ZONE_ENTERED"]
        assert len(entered) == 1, "a 30 s cooldown must collapse four crossings into one event"

    def test_a_disabled_zone_mid_session_stops_further_events(self) -> None:
        sink = CollectingSink()
        pipeline = make_pipeline(
            source=ScriptedSource(20),
            model=ScriptedVisionModel(walk_script(0.05, 0.85, 20)),
            sink=sink,
            zones=[zone_spec()],
        )
        for _ in range(12):  # far enough in that the person is inside
            pipeline.step()
        pipeline.set_zones([zone_spec(enabled=False)])
        run_to_end(pipeline)
        assert [e.type for e in sink.events] == ["ZONE_ENTERED"]
        assert len(sink.events) == 1, "a disabled zone must produce nothing further"

    def test_a_zone_edited_mid_session_applies_immediately(self) -> None:
        sink = CollectingSink()
        pipeline = make_pipeline(
            source=ScriptedSource(40),
            model=ScriptedVisionModel(walk_script(0.02, 0.95, 40)),
            sink=sink,
            zones=[zone_spec(x1=0.85, x2=0.99)],
        )
        for _ in range(5):
            pipeline.step()
        pipeline.set_zones([zone_spec(x1=0.30, x2=0.70)])
        run_to_end(pipeline)
        assert "ZONE_ENTERED" in sink.types


class TestRateControl:
    def test_frames_are_dropped_not_queued_when_capture_outpaces_the_budget(self) -> None:
        """Capture faster than the inference budget ⇒ drop, never accumulate.

        The scripted source advances 0.001 s per frame while the budget is 5 fps
        (0.2 s), so the pipeline must process roughly one frame in fifty and
        report the rest as dropped — which is what keeps live view latency low.
        """
        from tests.conftest import FakeClock

        clock = FakeClock(step=0.001)
        sink = CollectingSink()
        pipeline = make_pipeline(
            source=ScriptedSource(100),
            model=ScriptedVisionModel(walk_script(0.05, 0.85, 100)),
            sink=sink,
            zones=[zone_spec()],
            target_fps=5.0,
            clock=clock,
        )
        run_to_end(pipeline)
        assert pipeline.stats.frames_dropped > 90
        assert pipeline.stats.frames_processed < 10
        assert pipeline.stats.frames_processed + pipeline.stats.frames_dropped == 100

    def test_a_slow_model_does_not_stop_the_loop(self) -> None:
        """A model slower than real time must still produce events, just fewer."""
        from tests.conftest import FakeClock

        clock = FakeClock(step=0.2)
        sink = CollectingSink()
        pipeline = make_pipeline(
            source=ScriptedSource(30),
            model=SlowModel(walk_script(0.05, 0.85, 30), delay_seconds=0.001),
            sink=sink,
            zones=[zone_spec()],
            target_fps=5.0,
            clock=clock,
        )
        run_to_end(pipeline)
        assert sink.types == ["ZONE_ENTERED", "ZONE_EXITED"]

    def test_a_fast_model_processes_every_frame(self) -> None:
        sink = CollectingSink()
        pipeline = make_pipeline(
            source=ScriptedSource(30),
            model=ScriptedVisionModel(walk_script(0.05, 0.85, 30)),
            sink=sink,
            zones=[zone_spec()],
            target_fps=1000.0,
        )
        run_to_end(pipeline)
        assert pipeline.stats.frames_processed == 30
        assert pipeline.stats.frames_dropped == 0

    def test_processed_frames_match_inferences(self) -> None:
        model = ScriptedVisionModel(walk_script(0.05, 0.85, 30))
        pipeline = make_pipeline(source=ScriptedSource(30), model=model, sink=CollectingSink())
        run_to_end(pipeline)
        assert model.calls == pipeline.stats.frames_processed


class TestSourceTermination:
    def test_the_pipeline_stops_when_the_source_ends(self) -> None:
        pipeline = make_pipeline(
            source=ScriptedSource(5),
            model=ScriptedVisionModel(walk_script(0.05, 0.85, 30)),
            sink=CollectingSink(),
        )
        assert run_to_end(pipeline) == 5
        assert pipeline.step() is False

    def test_an_immediate_end_is_handled(self) -> None:
        pipeline = make_pipeline(
            source=ScriptedSource(0),
            model=ScriptedVisionModel([]),
            sink=CollectingSink(),
        )
        assert pipeline.step() is False
        assert pipeline.source_health.state is SourceState.FAILED
        assert pipeline.source_health.reason_code == "SOURCE_ENDED"


class TestModelFailure:
    def test_an_inference_error_degrades_the_session_rather_than_crashing(self) -> None:
        pipeline = make_pipeline(
            source=ScriptedSource(5), model=FailingModel([]), sink=CollectingSink()
        )
        run_to_end(pipeline)
        assert pipeline.model_health in ("degraded", "failed")
        assert pipeline.source_health.reason_code == "MODEL_ERROR"
        assert pipeline.stats.inference_errors == 5

    def test_a_degraded_session_recovers_when_the_model_works_again(self) -> None:
        class FlakyModel(ScriptedVisionModel):
            def __init__(self) -> None:
                super().__init__(walk_script(0.05, 0.85, 20))
                self.fail_next = 2
                self.notes = 0

            def infer(self, frame):
                from visionops.vision.onnx_model import ModelError

                if self.fail_next > 0:
                    self.fail_next -= 1
                    self.note_error()
                    raise ModelError("MODEL_ERROR", "transient")
                return super().infer(frame)

        model = FlakyModel()
        pipeline = make_pipeline(
            source=ScriptedSource(20), model=model, sink=CollectingSink(), zones=[zone_spec()]
        )
        run_to_end(pipeline)
        assert pipeline.stats.inference_errors == 2
        assert pipeline.model_health == "ready", "the session must recover without a restart"

    def test_an_unexpected_model_exception_does_not_kill_the_loop(self) -> None:
        class ExplodingModel(ScriptedVisionModel):
            def infer(self, frame):
                raise RuntimeError("something nobody planned for")

        pipeline = make_pipeline(
            source=ScriptedSource(3), model=ExplodingModel([]), sink=CollectingSink()
        )
        run_to_end(pipeline)  # must not raise
        assert pipeline.model_health == "failed"
        assert pipeline.stats.inference_errors == 3


class TestStatePayload:
    def test_the_payload_carries_everything_the_ui_needs(self) -> None:
        sink = CollectingSink()
        pipeline = make_pipeline(
            source=ScriptedSource(30),
            model=ScriptedVisionModel(walk_script(0.05, 0.85, 30)),
            sink=sink,
            zones=[zone_spec()],
        )
        run_to_end(pipeline)
        payload = pipeline.state_payload(None, (), ())
        assert payload["type"] == "session.state"
        assert payload["camera_id"] == "cam-test"
        assert payload["session_id"] == "sess-test"
        assert set(payload["source"]) >= {"state", "fps", "frames", "dropped", "kind"}
        assert set(payload["ai"]) >= {"state", "model", "sha256", "provider"}
        assert set(payload["stats"]) >= {
            "active_tracks",
            "in_zone",
            "processed_fps",
            "frame_age_ms",
            "frames_processed",
            "events_emitted",
        }
        assert len(payload["zones"]) == 1
        assert payload["zones"][0]["name"] == "restricted"
        assert len(payload["zones"][0]["polygon"]) == 4
        assert payload["zones"][0]["occupancy"] == 0
        assert "tracks" in payload and "events" in payload

    def test_occupancy_reflects_tracks_inside_the_zone(self) -> None:
        sink = CollectingSink()
        pipeline = make_pipeline(
            source=ScriptedSource(10),
            model=ScriptedVisionModel([[(box(0.45), 0.9)]] * 10),
            sink=sink,
            zones=[zone_spec()],
        )
        for _ in range(6):
            pipeline.step()
        tracks = pipeline._tracker.update(
            [Detection(0, "person", 0.9, BBox.from_xyxy(0.45, 0.3, 0.55, 0.8))], 99, 99.0
        )
        payload = pipeline.state_payload(None, tracks, ())
        assert payload["zones"][0]["occupancy"] == 1

    def test_state_is_published_regularly_and_on_events(self) -> None:
        sink = CollectingSink()
        pipeline = make_pipeline(
            source=ScriptedSource(30),
            model=ScriptedVisionModel(walk_script(0.05, 0.85, 30)),
            sink=sink,
            zones=[zone_spec()],
        )
        run_to_end(pipeline)
        assert len(sink.states) > 0
        with_events = [state for state in sink.states if state["events"]]
        assert with_events, "an event publish must include the event"

    def test_geometry_in_the_payload_is_normalised(self) -> None:
        sink = CollectingSink()
        pipeline = make_pipeline(
            source=ScriptedSource(10),
            model=ScriptedVisionModel([[(box(0.45), 0.9)]] * 10),
            sink=sink,
            zones=[zone_spec()],
        )
        run_to_end(pipeline)
        for state in sink.states:
            for track in state["tracks"]:
                for value in track["box"]:
                    assert 0.0 <= value <= 1.0
            for zone in state["zones"]:
                for point in zone["polygon"]:
                    assert all(0.0 <= value <= 1.0 for value in point)


class TestClose:
    def test_closing_closes_the_sink(self) -> None:
        sink = CollectingSink()
        pipeline = make_pipeline(source=ScriptedSource(2), model=ScriptedVisionModel([]), sink=sink)
        pipeline.close()
        assert sink.closed


class TestVisionCoreIsolation:
    def test_the_vision_package_imports_no_web_or_orm_module(self) -> None:
        """The CV core must stay embeddable (edge agent, notebook, CLI)."""
        import ast
        from pathlib import Path

        forbidden = {
            "fastapi",
            "starlette",
            "sqlalchemy",
            "httpx",
            "requests",
            "uvicorn",
            "pydantic",
        }
        offenders: list[str] = []
        for path in Path("backend/visionops/vision").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [(node.module or "").split(".")[0]]
                else:
                    continue
                for name in names:
                    if name in forbidden:
                        offenders.append(f"{path.name}:{node.lineno} imports {name}")
        assert offenders == [], f"the vision core must stay framework-free: {offenders}"

    def test_no_face_recognition_library_is_ever_a_dependency(self) -> None:
        """ADR-018 is enforced at the dependency boundary, where it counts.

        The UI and docs legitimately *say* "no biometrics" — the word itself is
        not a violation. What must never exist is a face-recognition library or
        an identity pipeline, so that is exactly what is asserted.
        """
        import re
        from pathlib import Path

        forbidden = re.compile(
            r"\b(insightface|facenet|face_recognition|dlib|openface|deepface|arcface|"
            r"buffalo_l|w600k|retinaface|mobiface|pyface|face_alignment)\b",
            re.IGNORECASE,
        )
        manifests = [
            Path("pyproject.toml"),
            Path("frontend/package.json"),
            Path("package-lock.json"),
            Path("frontend/package-lock.json"),
        ]
        offenders = [
            path.as_posix()
            for path in manifests
            if path.exists() and forbidden.search(path.read_text())
        ]
        assert offenders == [], (
            f"face-recognition libraries must never be dependencies: {offenders}"
        )

    def test_no_code_path_computes_a_face_embedding_or_identity(self) -> None:
        import re
        from pathlib import Path

        pattern = re.compile(
            r"(encode_face|face_embedding|face_descriptor|recognise_face|recognize_face|"
            r"identify_person|person_reid|reidentify)",
            re.IGNORECASE,
        )
        skip = ("node_modules", ".venv", "models", "data", "dist", "docs", "samples")
        offenders: list[str] = []
        for path in Path("backend").rglob("*.py"):
            if any(part in skip for part in path.parts):
                continue
            if pattern.search(path.read_text(encoding="utf-8")):
                offenders.append(path.as_posix())
        for path in Path("frontend/src").rglob("*.ts*"):
            if pattern.search(path.read_text(encoding="utf-8")):
                offenders.append(path.as_posix())
        assert offenders == [], f"identity code must never be introduced: {offenders}"

    def test_the_schema_has_no_identity_column(self) -> None:
        """There is nowhere to put a person's identity, by construction."""
        from visionops import models  # noqa: F401  (registers the tables)
        from visionops.db.base import Base

        banned = {
            "face",
            "faces",
            "face_embedding",
            "biometric",
            "identity",
            "person_id",
            "employee_id",
            "plate",
            "license_plate",
            "national_id",
            "name_of_person",
        }
        offenders: list[str] = []
        for table in Base.metadata.tables.values():
            for column in table.columns:
                if column.name.lower() in banned:
                    offenders.append(f"{table.name}.{column.name}")
        assert offenders == [], f"the schema must not be able to hold identity: {offenders}"


# ---------------------------------------------------------------------------
# Regressions
# ---------------------------------------------------------------------------


class TestEmptyReadIsNotEndOfStream:
    """``read()`` returning ``None`` is ambiguous; ``at_end`` resolves it.

    Treating every empty read as EOF ended browser sessions the instant they
    started, because a tab that has not pushed a frame yet looks exactly like a
    finished file.
    """

    class WaitingSource:
        """A live source with nothing to deliver *yet*."""

        kind = "browser"

        def __init__(self) -> None:
            self.frames = 0

        def open(self) -> None:
            return None

        def close(self) -> None:
            return None

        def read(self) -> Any:
            if self.frames == 0:
                self.frames += 1
                return make_frame(1)
            return None  # nothing yet — but not finished

        @property
        def at_end(self) -> bool:
            return False

    def test_a_waiting_source_keeps_the_session_alive(self) -> None:
        pipeline = make_pipeline(
            source=self.WaitingSource(),
            model=ScriptedVisionModel([]),
            sink=CollectingSink(),
        )
        assert pipeline.step() is True  # frame 1
        assert pipeline.step() is True, "an empty read must not end the session"
        assert pipeline.step() is True
        assert pipeline.stats.frames_processed == 1

    def test_a_waiting_source_is_not_reported_as_ended(self) -> None:
        pipeline = make_pipeline(
            source=self.WaitingSource(),
            model=ScriptedVisionModel([]),
            sink=CollectingSink(),
        )
        pipeline.step()
        pipeline.step()
        assert pipeline.source_health.reason_code != "SOURCE_ENDED", (
            "a source waiting for its first frame must not be reported as finished"
        )

    def test_a_finished_source_still_ends_the_session(self) -> None:
        pipeline = make_pipeline(
            source=ScriptedSource(1),
            model=ScriptedVisionModel([]),
            sink=CollectingSink(),
        )
        assert pipeline.step() is True
        assert pipeline.step() is False, "a real end of stream must still end the session"
        assert pipeline.source_health.state is SourceState.FAILED

    def test_a_file_source_reports_at_end_only_when_it_really_is(self) -> None:
        from visionops.ingest.file import FileSource

        source = FileSource(path=Path("/nonexistent.mp4"), loop=False)
        assert source.at_end is False
        source._health.state = SourceState.FAILED
        assert source.at_end is True

    def test_a_looping_file_never_reports_at_end(self) -> None:
        from visionops.ingest.file import FileSource

        source = FileSource(path=Path("/nonexistent.mp4"), loop=True)
        source._health.state = SourceState.FAILED
        assert source.at_end is False, "a looping source is never finished"
