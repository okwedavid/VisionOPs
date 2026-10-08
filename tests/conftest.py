"""Shared fixtures.

Two rules for this suite (``docs/TESTING.md`` §2):

1. No camera, no GPU and no network. Every test runs on a laptop.
2. The logic that produces customer events is tested **exactly**, using scripted
   detections and injected clocks — never with tolerances and never against a
   real model (real-model tests are marker-gated and *skipped*, not faked).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLES = REPO_ROOT / "samples"
FIXTURE_VIDEO = SAMPLES / "fixtures" / "zone-walkthrough.mp4"
MODEL_PATH = REPO_ROOT / "models" / "yolo11n.onnx"

os.environ.setdefault("VISIONOPS_ENV", "local")
os.environ.setdefault("VISIONOPS_LOG_LEVEL", "WARNING")
os.environ.setdefault("VISIONOPS_LOG_FORMAT", "console")


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


@pytest.fixture
def session_factory(tmp_path: Path) -> Iterator[Any]:
    """A real SQLite database in a temp directory, with the schema applied."""
    from visionops.db.engine import build_engine, build_session_factory, create_all

    engine = build_engine(f"sqlite:///{tmp_path / 'test.db'}")
    create_all(engine)
    factory = build_session_factory(engine)
    try:
        yield factory
    finally:
        engine.dispose()


@pytest.fixture
def organisation(session_factory: Any) -> str:
    """A real organisation row, so foreign keys are satisfied in tests."""
    from visionops.models import Organization

    with session_factory() as session:
        row = Organization(name="Test Org", slug="test-org", timezone="UTC")
        session.add(row)
        session.commit()
        return str(row.id)


@pytest.fixture
def camera(session_factory: Any, organisation: str) -> dict[str, str]:
    """A site plus a camera, returned as ids."""
    from visionops.models import Camera, Site

    with session_factory() as session:
        site = Site(organization_id=organisation, name="Test Site")
        session.add(site)
        session.commit()
        camera = Camera(organization_id=organisation, site_id=site.id, name="front-door")
        session.add(camera)
        session.commit()
        return {
            "site_id": str(site.id),
            "camera_id": str(camera.id),
            "organization_id": organisation,
        }


@pytest.fixture
def principal(organisation: str) -> Any:
    """A principal bound to the real organisation row, so FKs resolve."""
    from visionops.db.scoped import Principal

    return Principal(organization_id=organisation, actor_id="key-test", key_name="tests")


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


@pytest.fixture
def api_settings(tmp_path: Path) -> Any:
    """Settings pointed at a temp database, the repo samples and no model."""
    from visionops.config import Settings

    return Settings(
        database_url=f"sqlite:///{tmp_path / 'api.db'}",
        data_dir=tmp_path,
        snapshot_dir=tmp_path / "snapshots",
        media_root=SAMPLES,
        model_path=tmp_path / "missing-model.onnx",
        model_auto_download=False,
        log_level="WARNING",
        max_sessions=2,
        target_fps=60.0,  # the maximum: never rate-limit in tests
        publish_fps=30.0,
    )


@pytest.fixture
def api_key(api_settings: Any) -> str:
    """Bootstrap the schema and mint one API key; returns the plaintext key."""
    from visionops.bootstrap import ensure_bootstrap

    organization_id, _name = ensure_bootstrap(api_settings)
    from visionops.core.security import generate_api_key, hash_secret
    from visionops.models import ApiKey

    generated = generate_api_key()
    factory = _factory_for(api_settings)
    with factory() as session:
        session.add(
            ApiKey(
                id="key-for-tests",
                organization_id=organization_id,
                name="tests",
                key_prefix=generated.key_prefix,
                key_hash=hash_secret(generated.secret, generated.salt),
                key_salt=generated.salt,
                permissions=["*"],
            )
        )
        session.commit()
    return generated.full_key


def _factory_for(settings: Any) -> Any:
    from visionops.db.engine import build_engine, build_session_factory

    return build_session_factory(build_engine(settings.database_url))


@pytest.fixture
def client(api_settings: Any, api_key: str) -> Iterator[Any]:
    """A ``TestClient`` with the full application lifespan running."""
    from fastapi.testclient import TestClient

    from visionops.main import create_app

    with TestClient(create_app(api_settings)) as test_client:
        yield test_client


@pytest.fixture
def auth(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key}


# ---------------------------------------------------------------------------
# Vision fakes (the heart of deterministic testing)
# ---------------------------------------------------------------------------


def _as_bbox(value: Any) -> Any:
    """Coerce a scripted box to a :class:`BBox`, accepting ``(x1, y1, x2, y2)``.

    A fake that quietly accepts the wrong type is worse than no fake: the error
    then surfaces deep inside the tracker as a pipeline error instead of here.
    """
    from visionops.vision.contracts import BBox

    if isinstance(value, BBox):
        return value
    if isinstance(value, tuple) and len(value) == 4:
        return BBox.from_xyxy(*value)
    raise TypeError(
        f"scripted detections need a BBox or an (x1, y1, x2, y2) tuple, got {type(value).__name__}"
    )


class ScriptedVisionModel:
    """A :class:`VisionModel` that returns a scripted detection per frame.

    This is what makes the customer-visible logic assertable *exactly*: the test
    declares the detections, the tracker and zone engine then do real work, and
    the resulting event list is compared item by item.
    """

    def __init__(self, script: list[list[Any]], *, classes: tuple[str, ...] = ("person",)) -> None:
        from visionops.vision.onnx_model import ModelInfo

        self._script = script
        self._classes = classes
        self.calls = 0
        self.latencies: list[float] = []
        self._info = ModelInfo(
            name="scripted",
            path="<scripted>",
            sha256="0" * 64,
            provider="ScriptedProvider",
            input_width=640,
            input_height=640,
            classes=classes,
        )
        self.state = "ready"

    @property
    def info(self) -> Any:
        return self._info

    @property
    def last_latency_ms(self) -> float | None:
        return self.latencies[-1] if self.latencies else None

    def warmup(self, iterations: int = 1) -> float:
        return 0.0

    def infer(self, frame: Any) -> list[Any]:
        from visionops.vision.contracts import Detection

        # Indexed by the frame sequence, not the call count, so a rate-limited
        # pipeline still sees the scene progress at its true speed.
        if not self._script:
            # An empty script means "this model sees nothing", which is a valid
            # scene. Indexing into it would raise and masquerade as a model bug.
            self.calls += 1
            return []
        index = min(max(frame.seq - 1, 0), len(self._script) - 1)
        self.calls += 1
        detections: list[Detection] = []
        for item in self._script[index]:
            box, score, label = item[0], item[1], (item[2] if len(item) > 2 else self._classes[0])
            class_id = self._classes.index(label) if label in self._classes else 0
            detections.append(
                Detection(
                    class_id=class_id,
                    label=label,
                    confidence=score,
                    box=_as_bbox(box),
                    frame_seq=frame.seq,
                )
            )
        return detections

    def close(self) -> None:
        self.state = "closed"

    def note_error(self) -> int:
        return 1


class FailingModel(ScriptedVisionModel):
    """Raises :class:`ModelError` on every inference (fault injection)."""

    def infer(self, frame: Any) -> list[Any]:
        from visionops.vision.onnx_model import ModelError

        self.calls += 1
        raise ModelError("MODEL_ERROR", "inference failed: injected fault")


class SlowModel(ScriptedVisionModel):
    """Sleeps to simulate a model that cannot keep up with the capture rate."""

    def __init__(
        self, script: list[list[Any]], *, delay_seconds: float = 0.05, **kwargs: Any
    ) -> None:
        super().__init__(script, **kwargs)
        self.delay_seconds = delay_seconds

    def infer(self, frame: Any) -> list[Any]:
        import time

        time.sleep(self.delay_seconds)
        return super().infer(frame)


class CollectingSink:
    """An :class:`EventSink` that keeps everything, for exact assertions."""

    def __init__(self) -> None:
        self.events: list[Any] = []
        self.states: list[dict[str, Any]] = []
        self.closed = False

    def emit(self, events: Any) -> None:
        from dataclasses import replace

        from visionops.core.ids import new_id

        for event in events:
            self.events.append(event if event.id is not None else replace(event, id=new_id()))

    def publish_state(self, state: dict[str, Any]) -> None:
        self.states.append(state)

    def close(self) -> None:
        self.closed = True

    @property
    def types(self) -> list[str]:
        return [event.type for event in self.events]


@pytest.fixture
def scripted_model() -> ScriptedVisionModel:
    return ScriptedVisionModel([])


@pytest.fixture
def sink() -> CollectingSink:
    return CollectingSink()


# ---------------------------------------------------------------------------
# Geometry helpers for tests
# ---------------------------------------------------------------------------


def walk_script(
    start_x: float,
    end_x: float,
    frames: int,
    *,
    label: str = "person",
    confidence: float = 0.9,
    y1: float = 0.30,
    y2: float = 0.80,
    width: float = 0.10,
) -> list[list[tuple[tuple[float, float, float, float], float, str]]]:
    """A script of one person walking left → right, one detection per frame."""
    from visionops.vision.contracts import BBox

    script: list[list[tuple[tuple[float, float, float, float], float, str]]] = []
    for index in range(frames):
        ratio = index / max(1, frames - 1)
        x = start_x + (end_x - start_x) * ratio
        script.append([(BBox.from_xyxy(x, y1, x + width, y2), confidence, label)])
    return script


def zone_spec(
    *,
    zone_id: str = "zone-1",
    name: str = "restricted",
    x1: float = 0.34,
    y1: float = 0.20,
    x2: float = 0.66,
    y2: float = 0.95,
    min_dwell: float = 0.0,
    cooldown: float = 0.0,
    severity: str = "warning",
    **overrides: Any,
) -> Any:
    from visionops.vision.contracts import Severity, ZoneSpec
    from visionops.vision.geometry import rectangle_polygon

    return ZoneSpec(
        zone_id=zone_id,
        name=name,
        points=rectangle_polygon(x1, y1, x2, y2),
        severity=Severity(severity),
        min_dwell_seconds=min_dwell,
        cooldown_seconds=cooldown,
        **overrides,
    )


def make_frame(seq: int, width: int = 640, height: int = 360, timestamp: float = 0.0) -> Any:
    import numpy as np

    from visionops.vision.contracts import Frame

    return Frame(
        image=np.zeros((height, width, 3), dtype=np.uint8),
        seq=seq,
        timestamp=timestamp,
        width=width,
        height=height,
    )


class FakeClock:
    """A clock the test advances explicitly.

    Rate control compares wall-clock time, so without this a scripted 30-frame
    run would drop 27 frames depending on machine speed. Each ``tick`` is one
    frame interval, making every test deterministic.
    """

    def __init__(self, start: float = 1_000_000.0, step: float = 0.2) -> None:
        self.now = start
        self.step = step

    def __call__(self) -> float:
        return self.now

    def tick(self) -> float:
        self.now += self.step
        return self.now


def make_pipeline(
    *,
    source: Any,
    model: Any,
    sink: Any,
    zones: Any = (),
    model_name: str = "scripted",
    target_fps: float = 1000.0,
    tracker_config: Any = None,
    clock: Any = None,
) -> Any:
    from visionops.vision.onnx_model import ModelInfo

    # A scripted source and the pipeline share one clock, so frame N is always
    # read at the same simulated time and rate control is reproducible.
    if isinstance(source, ScriptedSource):
        source.clock = clock if clock is not None else FakeClock()
        clock = source.clock
    from visionops.vision.pipeline import PipelineConfig, VisionPipeline
    from visionops.vision.tracker import TrackerConfig

    info = ModelInfo(
        name=model_name,
        path="<test>",
        sha256="0" * 64,
        provider="ScriptedProvider",
        input_width=640,
        input_height=640,
        classes=("person",),
    )
    return VisionPipeline(
        camera_id="cam-test",
        camera_name="test-camera",
        session_id="sess-test",
        source=source,
        model=model,
        sink=sink,
        model_info=info,
        zones=list(zones),
        config=PipelineConfig(
            target_fps=target_fps,
            publish_fps=30.0,
            tracker_config=tracker_config or TrackerConfig(min_hits=1, max_age=10),
        ),
        clock=clock or FakeClock(),
    )


class ScriptedSource:
    """A source that yields a fixed number of frames, then ends.

    When given a :class:`FakeClock` it advances it once per frame, so the
    pipeline's rate control sees a deterministic frame interval instead of
    however fast the machine happens to run.
    """

    kind = "file"

    def __init__(self, frames: int, clock: Any = None) -> None:
        self.remaining = frames
        self.seq = 0
        self.clock = clock
        self.position: float | None = None
        self.duration: float | None = None
        self.closed = False

    def open(self) -> None:
        return None

    def read(self) -> Any:
        if self.remaining <= 0:
            return None
        if self.clock is not None:
            self.clock.tick()
        self.remaining -= 1
        self.seq += 1
        timestamp = self.clock.now if self.clock is not None else float(self.seq)
        return make_frame(self.seq, timestamp=timestamp)

    @property
    def at_end(self) -> bool:
        """This fake is a finite clip, so it ends rather than waits."""
        return self.remaining <= 0

    def close(self) -> None:
        self.closed = True

    def reopen(self) -> None:
        self.closed = False
        self.remaining = 0

    def health(self) -> Any:  # pragma: no cover - not used by the pipeline
        raise NotImplementedError


__all__ = [
    "FIXTURE_VIDEO",
    "MODEL_PATH",
    "REPO_ROOT",
    "SAMPLES",
    "CollectingSink",
    "FailingModel",
    "FakeClock",
    "ScriptedSource",
    "ScriptedVisionModel",
    "SlowModel",
    "make_frame",
    "make_pipeline",
    "walk_script",
    "zone_spec",
]


@pytest.fixture
def mint_key(api_settings: Any, api_key: str) -> Any:
    """Mint an extra API key with explicit permissions; returns the plaintext."""

    def _mint(permissions: list[str], *, name: str = "scoped") -> str:
        from visionops.core.security import generate_api_key, hash_secret
        from visionops.models import ApiKey

        generated = generate_api_key()
        factory = _factory_for(api_settings)
        with factory() as session:
            organization_id = (
                session.query(ApiKey).filter(ApiKey.name == "tests").one().organization_id
            )
            session.add(
                ApiKey(
                    organization_id=organization_id,
                    name=name,
                    key_prefix=generated.key_prefix,
                    key_hash=hash_secret(generated.secret, generated.salt),
                    key_salt=generated.salt,
                    permissions=list(permissions),
                )
            )
            session.commit()
        return generated.full_key

    return _mint


@pytest.fixture
def session_client(client: Any) -> Any:
    """A ``TestClient`` whose container serves a scripted model.

    The session path deliberately refuses to start without a loaded model — a
    dashboard that shows detections it never computed is worse than an error.
    These tests therefore swap in :class:`ScriptedVisionModel` *after* the
    lifespan has run, which keeps them hermetic and fast while still exercising
    the real session manager, thread, database and WebSockets.
    """
    container = client.app.state.container
    model = ScriptedVisionModel(
        [
            # A person walks into the zone, stays, then leaves.
            [((0.30, 0.35, 0.18, 0.70), 0.93)],
            [((0.40, 0.35, 0.18, 0.70), 0.93)],
            [((0.50, 0.35, 0.18, 0.70), 0.93)],
            [((0.60, 0.35, 0.18, 0.70), 0.93)],
            [],
        ]
    )
    # The session manager snapshots its dependencies at construction, exactly as
    # it does in production, so both references have to move together.
    container.model = model
    container.model_error = None
    container.model_info = model.info
    container.sessions._model = model
    container.sessions._model_info = model.info
    return client
