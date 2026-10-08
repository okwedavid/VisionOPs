"""Tests for the operator CLI (``visionops.cli``).

The CLI is the documented entrypoint, so it is exercised through :func:`main`
exactly as an operator (or the Makefile) invokes it: real argument parsing, real
SQLite migrations, real bootstrap. Only the two genuinely external edges —
uvicorn and the model download — are patched.
"""

from __future__ import annotations

import argparse
import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from tests.conftest import REPO_ROOT

from visionops import cli
from visionops.config import Settings
from visionops.core.security import generate_api_key


class _Provider:
    """Stands in for the cached ``get_settings`` singleton."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.clears = 0

    def __call__(self) -> Settings:
        return self._settings

    def cache_clear(self) -> None:
        self.clears += 1


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.chdir(tmp_path)
    return Settings(
        env="local",
        database_url=f"sqlite:///{tmp_path / 'cli.db'}",
        auto_migrate=True,
        data_dir=tmp_path / "data",
        snapshot_dir=tmp_path / "data" / "snapshots",
        media_root=tmp_path / "data" / "media",
        model_path=tmp_path / "yolo11n.onnx",
        model_auto_download=False,
        api_key=generate_api_key().full_key,
        log_level="WARNING",
        log_format="console",
    )


@pytest.fixture
def use_settings(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> _Provider:
    provider = _Provider(settings)
    monkeypatch.setattr(cli, "get_settings", provider)
    return provider


def test_parser_rejects_no_command() -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([])


def test_main_dispatches_to_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "cmd_init", lambda _args: 7)
    assert cli.main(["init"]) == 7


def test_main_returns_130_on_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    def _interrupt(_args: argparse.Namespace) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "cmd_init", _interrupt)
    assert cli.main(["init"]) == 130


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def test_init_creates_schema_and_organisation(
    settings: Settings, use_settings: _Provider, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["init"]) == 0
    out = capsys.readouterr().out
    assert "organisation:" in out
    assert "Camera credentials cannot be stored" in out
    assert Path(settings.database_url.removeprefix("sqlite:///")).exists()


def test_init_with_master_key_skips_the_warning(
    settings: Settings, use_settings: _Provider, capsys: pytest.CaptureFixture[str]
) -> None:
    settings.master_key = base64.urlsafe_b64encode(b"k" * 32).decode()
    assert cli.main(["init"]) == 0
    assert "VISIONOPS_MASTER_KEY=" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# key
# ---------------------------------------------------------------------------


def _first_key_id(settings: Settings) -> str:
    from visionops.db.engine import build_engine, build_session_factory
    from visionops.models import ApiKey

    engine = build_engine(settings.database_url)
    try:
        factory = build_session_factory(engine)
        with factory() as session:
            return str(session.query(ApiKey).first().id)
    finally:
        engine.dispose()


def test_key_add_list_and_revoke(
    settings: Settings, use_settings: _Provider, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["init"]) == 0
    capsys.readouterr()

    assert cli.main(["key", "add", "--name", "ops", "--permission", "events:read"]) == 0
    created = capsys.readouterr().out
    assert "API key created" in created
    assert "vo_" in created

    assert cli.main(["key", "list"]) == 0
    listing = capsys.readouterr().out
    assert "PREFIX" in listing
    assert "ops" in listing

    key_id = _first_key_id(settings)
    assert cli.main(["key", "revoke", key_id]) == 0
    assert "revoked" in capsys.readouterr().out

    assert cli.main(["key", "revoke", "does-not-exist"]) == 1
    assert "no key with id" in capsys.readouterr().err


def test_key_without_subcommand_defaults_to_add(
    settings: Settings, use_settings: _Provider, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["key"]) == 0
    assert "API key created" in capsys.readouterr().out


def test_key_list_with_no_keys(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    use_settings: _Provider,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from visionops.db.engine import build_engine, build_session_factory, create_all
    from visionops.models import Organization

    engine = build_engine(settings.database_url)
    create_all(engine)
    with build_session_factory(engine)() as session:
        session.add(Organization(name="Org", slug="org", timezone="UTC"))
        session.commit()
    engine.dispose()
    monkeypatch.setattr(cli, "run_migrations", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "ensure_bootstrap", lambda _settings: ("org-1", "Org"))
    assert cli.main(["key", "list"]) == 0
    assert "no API keys yet" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# migrate
# ---------------------------------------------------------------------------


def test_migrate_up_and_down(
    settings: Settings, use_settings: _Provider, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["migrate"]) == 0
    assert "schema at" in capsys.readouterr().out
    assert cli.main(["migrate", "--downgrade", "base"]) == 0
    assert "downgraded to base" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# prune
# ---------------------------------------------------------------------------


def _seed_expired(
    settings: Settings,
) -> Path:
    from visionops.db.engine import build_engine, build_session_factory
    from visionops.models import AuditLog, Event, Organization

    snapshot = settings.snapshot_dir / "2020" / "01" / "01" / "old.jpg"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_bytes(b"jpeg")
    old = datetime.now(UTC) - timedelta(days=30)

    engine = build_engine(settings.database_url)
    try:
        factory = build_session_factory(engine)
        with factory() as session:
            organization = session.query(Organization).first()
            organization.retention_days = 1
            session.add(
                Event(
                    organization_id=organization.id,
                    type="zone.entered",
                    severity="warning",
                    occurred_at=old,
                    snapshot_path="2020/01/01/old.jpg",
                )
            )
            session.add(
                AuditLog(
                    organization_id=organization.id,
                    actor_type="system",
                    actor_id="seed",
                    action="test",
                    outcome="success",
                    created_at=old,
                )
            )
            session.commit()
    finally:
        engine.dispose()
    return snapshot


def test_prune_dry_run_then_real(
    settings: Settings, use_settings: _Provider, capsys: pytest.CaptureFixture[str]
) -> None:
    settings.audit_retention_days = 1
    assert cli.main(["init"]) == 0
    snapshot = _seed_expired(settings)
    capsys.readouterr()

    assert cli.main(["prune", "--dry-run"]) == 0
    dry = capsys.readouterr().out
    assert "would delete: 1 event(s)" in dry
    assert snapshot.exists()

    assert cli.main(["prune"]) == 0
    real = capsys.readouterr().out
    assert "deleted: 1 event(s), 1 snapshot file(s), 1 audit row(s)" in real
    assert not snapshot.exists()


def test_prune_on_empty_database(
    settings: Settings, use_settings: _Provider, capsys: pytest.CaptureFixture[str]
) -> None:
    from visionops.db.engine import build_engine, create_all

    engine = build_engine(settings.database_url)
    create_all(engine)
    engine.dispose()
    assert cli.main(["prune"]) == 0
    assert "deleted: 0 event(s)" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# model
# ---------------------------------------------------------------------------


def test_model_info_missing(
    settings: Settings, use_settings: _Provider, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["model", "--info"]) == 1
    assert "no model at" in capsys.readouterr().out


def test_model_info_reports_hash(
    settings: Settings, use_settings: _Provider, capsys: pytest.CaptureFixture[str]
) -> None:
    settings.model_path.write_bytes(b"weights")
    assert cli.main(["model", "--info"]) == 0
    assert "sha256:" in capsys.readouterr().out


def test_model_already_present(
    settings: Settings, use_settings: _Provider, capsys: pytest.CaptureFixture[str]
) -> None:
    settings.model_path.write_bytes(b"weights")
    assert cli.main(["model"]) == 0
    assert "already present" in capsys.readouterr().out


def test_model_download_disabled(
    settings: Settings, use_settings: _Provider, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["model"]) == 1
    assert "auto-download is disabled" in capsys.readouterr().out


class _Response:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._payload


def test_model_force_download_rejects_a_bad_file(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    use_settings: _Provider,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import urllib.request

    captured: dict[str, Any] = {}

    def _urlopen(url: str, timeout: int) -> _Response:
        captured["url"] = url
        return _Response(b"not-an-onnx-file")

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    assert cli.main(["model", "--force"]) == 1
    err = capsys.readouterr().err
    assert "not a usable model" in err
    assert captured["url"] == settings.model_url


def test_model_download_failure_is_reported(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    use_settings: _Provider,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import urllib.request

    def _urlopen(url: str, timeout: int) -> _Response:
        raise OSError(f"cannot reach {url}")

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    assert cli.main(["model", "--force"]) == 1
    assert "download failed" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# bench
# ---------------------------------------------------------------------------


def test_bench_prints_and_writes_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    settings: Settings,
    use_settings: _Provider,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import visionops.bench

    monkeypatch.setattr(
        visionops.bench,
        "run_benchmark",
        lambda _settings, frames, include_file: {"frames": frames, "file": include_file},
    )
    output = tmp_path / "bench.json"
    assert cli.main(["bench", "--frames", "5", "--output", str(output)]) == 0
    assert capsys.readouterr().out.count("frames") >= 1
    assert json.loads(output.read_text(encoding="utf-8")) == {"frames": 5, "file": None}


# ---------------------------------------------------------------------------
# demo
# ---------------------------------------------------------------------------


def test_demo_without_fixture_fails(
    settings: Settings, use_settings: _Provider, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["demo"]) == 1
    assert "fixture not found" in capsys.readouterr().err


def test_demo_seeds_idempotently(
    settings: Settings, use_settings: _Provider, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = REPO_ROOT / "samples" / "fixtures" / "zone-walkthrough.mp4"
    assert cli.main(["demo", "--fixture", str(fixture)]) == 0
    first = capsys.readouterr().out
    assert "demo data ready" in first
    assert "Demo Camera" in first

    assert cli.main(["demo", "--fixture", str(fixture)]) == 0
    assert "demo data ready" in capsys.readouterr().out

    from visionops.db.engine import build_engine, build_session_factory
    from visionops.models import Camera, VideoSource, Zone

    engine = build_engine(settings.database_url)
    try:
        factory = build_session_factory(engine)
        with factory() as session:
            assert session.query(Camera).count() == 1
            assert session.query(VideoSource).count() == 1
            assert session.query(Zone).count() == 1
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# serve and .env handling
# ---------------------------------------------------------------------------


def test_serve_uses_settings_defaults(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, use_settings: _Provider
) -> None:
    import uvicorn

    captured: dict[str, Any] = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: captured.update(kwargs, app=app))
    assert cli.main(["serve"]) == 0
    assert captured["app"] == "visionops.main:create_app"
    assert captured["factory"] is True
    assert captured["reload"] is False


def test_serve_honours_cli_overrides(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, use_settings: _Provider
) -> None:
    import uvicorn

    captured: dict[str, Any] = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: captured.update(kwargs))
    assert cli.main(["serve", "--host", "0.0.0.0", "--port", "9001", "--reload"]) == 0
    assert captured["port"] == 9001
    assert captured["reload"] is True


def test_load_env_file_is_conservative(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import os

    env_file = tmp_path / "custom.env"
    env_file.write_text(
        "\n".join(
            [
                "# a comment",
                "",
                "VISIONOPS_CLI_TEST_VALUE=from-file",
                "VISIONOPS_CLI_TEST_QUOTED='quoted'",
                "not-a-pair",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("VISIONOPS_CLI_TEST_VALUE", raising=False)
    monkeypatch.delenv("VISIONOPS_CLI_TEST_QUOTED", raising=False)

    cli._load_env_file(str(env_file))
    assert os.environ["VISIONOPS_CLI_TEST_VALUE"] == "from-file"
    assert os.environ["VISIONOPS_CLI_TEST_QUOTED"] == "quoted"

    monkeypatch.setenv("VISIONOPS_CLI_TEST_VALUE", "keep-me")
    cli._load_env_file(str(env_file))
    assert os.environ["VISIONOPS_CLI_TEST_VALUE"] == "keep-me"

    cli._load_env_file(str(tmp_path / "missing.env"))


def test_settings_loads_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, settings: Settings
) -> None:
    env_file = tmp_path / "custom.env"
    env_file.write_text("VISIONOPS_CLI_MARKER=loaded\n", encoding="utf-8")
    provider = _Provider(settings)
    monkeypatch.setattr(cli, "get_settings", provider)

    resolved = cli._settings(argparse.Namespace(env_file=str(env_file)))
    import os

    assert resolved is settings
    assert provider.clears == 1
    assert os.environ["VISIONOPS_CLI_MARKER"] == "loaded"
