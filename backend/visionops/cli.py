"""Operator CLI: ``serve``, ``init``, ``key``, ``prune``, ``model``, ``bench``, ``demo``.

The Makefile is the documented entrypoint, but every command works standalone so
the tool is useful in a cron line or a container entrypoint.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .bootstrap import ensure_bootstrap
from .config import Settings, get_settings
from .core.ids import new_id
from .core.logging import configure_logging
from .core.security import generate_api_key, generate_master_key, hash_secret
from .db.engine import build_engine, build_session_factory
from .db.migrations import current_revision, downgrade_to, run_migrations
from .db.scoped import Principal
from .models import ApiKey, AuditLog, Camera, Event, Organization, Site, VideoSource, Zone
from .services.snapshot import SnapshotWriter

logger = logging.getLogger("visionops.cli")

BANNER = "VisionOps {version}"


def _settings(args: argparse.Namespace) -> Settings:
    if getattr(args, "env_file", None):
        _load_env_file(args.env_file)
        get_settings.cache_clear()
    return get_settings()


def _load_env_file(path: str) -> None:
    """Load a ``.env`` file without adding a dependency."""
    import os

    target = Path(path)
    if not target.exists():
        return
    for line in target.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the API (and, if built, the web app) with uvicorn."""
    import uvicorn

    settings = _settings(args)
    host = args.host or settings.host
    port = args.port or settings.port
    configure_logging(level=settings.log_level, fmt=settings.log_format, log_file=settings.log_file)
    print(f"{BANNER.format(version=settings.version)} → http://{host}:{port}", file=sys.stderr)
    uvicorn.run(
        "visionops.main:create_app",
        factory=True,
        host=host,
        port=port,
        reload=bool(args.reload),
        log_level=settings.log_level.lower(),
        access_log=False,
    )
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    """Create directories, apply migrations, and ensure an organisation + API key."""
    settings = _settings(args)
    configure_logging(level=settings.log_level, fmt="console")
    settings.ensure_directories()
    print(f"data directory: {settings.data_dir.resolve()}")
    print(f"database:       {settings.database_url}")
    if settings.auto_migrate:
        run_migrations(settings.database_url)
        print(f"schema:         {current_revision(settings.database_url)}")
    organization_id, name = ensure_bootstrap(settings)
    print(f"organisation:   {name} ({organization_id})")
    if not settings.decoded_master_key:
        key = generate_master_key()
        print("")
        print("  Camera credentials cannot be stored without a master key.")
        print("  Add this line to your .env (keep it secret and back it up):")
        print("")
        print(f"    VISIONOPS_MASTER_KEY={key}")
        print("")
    print("Next: `visionops key add --name ops-laptop`  then  `make dev`")
    return 0


def cmd_key(args: argparse.Namespace) -> int:
    """Create or revoke API keys."""
    settings = _settings(args)
    if settings.auto_migrate:
        run_migrations(settings.database_url)
    ensure_bootstrap(settings)
    engine = build_engine(settings.database_url)
    factory = build_session_factory(engine)
    try:
        with factory() as session:
            organization = session.query(Organization).first()
            if organization is None:  # pragma: no cover - bootstrap guarantees one
                print("no organisation found; run `visionops init` first", file=sys.stderr)
                return 1
            if args.revoke:
                record = session.query(ApiKey).filter(ApiKey.id == args.revoke).one_or_none()
                if record is None:
                    print(f"no key with id {args.revoke}", file=sys.stderr)
                    return 1
                record.revoked = True
                session.commit()
                print(f"revoked {record.name} ({record.key_prefix})")
                return 0
            if args.list:
                rows = session.query(ApiKey).order_by(ApiKey.created_at).all()
                if not rows:
                    print("no API keys yet — create one with `visionops key add`")
                    return 0
                print(f"{'ID':38} {'PREFIX':18} {'NAME':20} {'REVOKED':8} LAST USED")
                for row in rows:
                    used = row.last_used_at.isoformat() if row.last_used_at else "never"
                    print(f"{row.id:38} {row.key_prefix:18} {row.name:20} {row.revoked!s:8} {used}")
                return 0

            generated = generate_api_key()
            session.add(
                ApiKey(
                    id=new_id(),
                    organization_id=organization.id,
                    name=args.name,
                    key_prefix=generated.key_prefix,
                    key_hash=hash_secret(generated.secret, generated.salt),
                    key_salt=generated.salt,
                    permissions=list(args.permission or ["*"]),
                )
            )
            session.commit()
            print("")
            print("  API key created. It is shown once and cannot be retrieved later.")
            print("")
            print(f"    {generated.full_key}")
            print("")
            return 0
    finally:
        engine.dispose()


def cmd_prune(args: argparse.Namespace) -> int:
    """Delete events and audit rows past their retention window, plus orphan snapshots."""
    settings = _settings(args)
    engine = build_engine(settings.database_url)
    factory = build_session_factory(engine)
    deleted_events = 0
    deleted_snapshots = 0
    deleted_audit = 0
    try:
        with factory() as session:
            organizations = session.query(Organization).all()
            writer = SnapshotWriter(root=settings.snapshot_dir)
            now = datetime.now(UTC)
            for organization in organizations:
                cutoff = now - timedelta(days=organization.retention_days)
                rows = (
                    session.query(Event)
                    .filter(Event.organization_id == organization.id, Event.occurred_at < cutoff)
                    .all()
                )
                snapshot_paths = [row.snapshot_path for row in rows if row.snapshot_path]
                for row in rows:
                    session.delete(row)
                deleted_events += len(rows)
                if not args.dry_run and snapshot_paths:
                    deleted_snapshots += writer.delete(snapshot_paths)
                if args.dry_run:
                    print(
                        f"  would delete {len(rows)} event(s) for {organization.slug} "
                        f"older than {cutoff:%Y-%m-%d} and {len(snapshot_paths)} snapshot(s)"
                    )

            audit_cutoff = now - timedelta(days=settings.audit_retention_days)
            audit_rows = session.query(AuditLog).filter(AuditLog.created_at < audit_cutoff).all()
            for audit_row in audit_rows:
                session.delete(audit_row)
            deleted_audit = len(audit_rows)
            if args.dry_run and audit_rows:
                print(
                    f"  would delete {len(audit_rows)} audit row(s) "
                    f"older than {audit_cutoff:%Y-%m-%d}"
                )
            # The audit trail is per-organisation: a database with no
            # organisation yet (prune run before init) has nowhere to record the
            # result, and by definition had nothing to prune.
            if not args.dry_run and organizations:
                session.add(
                    AuditLog(
                        id=new_id(),
                        organization_id=organizations[0].id,
                        actor_type="system",
                        actor_id="prune",
                        action="retention.prune",
                        outcome="success",
                        detail={
                            "deleted_events": deleted_events,
                            "deleted_snapshots": deleted_snapshots,
                            "deleted_audit_rows": deleted_audit,
                        },
                    )
                )
                session.commit()
    finally:
        engine.dispose()

    prefix = "would delete" if args.dry_run else "deleted"
    print(
        f"{prefix}: {deleted_events} event(s), {deleted_snapshots} snapshot file(s), "
        f"{deleted_audit} audit row(s)"
    )
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    """Apply or roll back schema migrations."""
    settings = _settings(args)
    if args.downgrade:
        downgrade_to(settings.database_url, args.downgrade)
        print(f"downgraded to {args.downgrade}")
        return 0
    run_migrations(settings.database_url, args.revision)
    print(f"schema at {current_revision(settings.database_url)}")
    return 0


def cmd_model(args: argparse.Namespace) -> int:
    """Fetch, verify or report the detection model."""
    from .vision.onnx_model import DetectorConfig, ModelError, OnnxYoloModel, sha256_file

    settings = _settings(args)
    path = settings.model_path
    if args.info:
        if not path.exists():
            print(f"no model at {path}")
            return 1
        print(
            f"path:   {path}\n"
            f"size:   {path.stat().st_size / 1e6:.1f} MB\n"
            f"sha256: {sha256_file(path)}"
        )
        return 0

    if path.exists() and not args.force:
        print(f"model already present at {path} (use --force to re-download)")
        return 0
    if not settings.model_auto_download and not args.force:
        print(
            "model auto-download is disabled (VISIONOPS_MODEL_AUTO_DOWNLOAD=false). "
            f"Download {settings.model_url} and place it at {path}."
        )
        return 1

    path.parent.mkdir(parents=True, exist_ok=True)
    print(f"downloading {settings.model_url}")
    try:
        import urllib.request

        temporary = path.with_suffix(path.suffix + ".part")
        with urllib.request.urlopen(settings.model_url, timeout=120) as response:  # noqa: S310
            temporary.write_bytes(response.read())
        temporary.replace(path)
    except Exception as exc:
        print(f"download failed: {exc}", file=sys.stderr)
        print(f"place the model file manually at {path}", file=sys.stderr)
        return 1

    model = OnnxYoloModel(DetectorConfig(model_path=path))
    try:
        info = model.load()
    except ModelError as exc:
        print(f"the downloaded file is not a usable model: {exc.detail}", file=sys.stderr)
        return 1
    print(f"model ready: {info.name} ({len(info.classes)} classes, sha256 {info.sha256[:16]}…)")
    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    """Measure inference latency, pipeline FPS and memory on this host."""
    from .bench import run_benchmark

    settings = _settings(args)
    results = run_benchmark(settings, frames=args.frames, include_file=args.file)
    print(json.dumps(results, indent=2, default=str))
    if args.output:
        Path(args.output).write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
        print(f"wrote {args.output}")
    return 0


def _stage_fixture(fixture: Path, media_root: Path) -> str:
    """Return a media-root-relative target for the demo fixture.

    ``file`` sources may only read from ``VISIONOPS_MEDIA_ROOT`` (path-traversal
    defence), and the committed fixture lives outside it. Copy it in once,
    idempotently, and refer to it by a relative path so the demo works on any
    machine with the default ``./data/media`` and stays portable.
    """
    import shutil

    root = media_root.resolve()
    try:
        return fixture.resolve().relative_to(root).as_posix()
    except ValueError:
        pass
    staged = root / "fixtures" / fixture.name
    staged.parent.mkdir(parents=True, exist_ok=True)
    if not staged.exists() or staged.stat().st_size != fixture.stat().st_size:
        shutil.copy2(fixture, staged)
    return staged.relative_to(root).as_posix()


def cmd_demo(args: argparse.Namespace) -> int:
    """Seed a demo organisation wired to the synthetic fixture video.

    This is the **hardware-free demo path**: no camera, no model download
    needed to see the product working. The fixture is clearly labelled synthetic
    and exercises the pipeline, not detection accuracy.
    """
    settings = _settings(args)
    configure_logging(level=settings.log_level, fmt="console")
    if settings.auto_migrate:
        run_migrations(settings.database_url)
    organization_id, _name = ensure_bootstrap(settings)

    fixture = Path(args.fixture or "samples/fixtures/zone-walkthrough.mp4")
    if not fixture.exists():
        print(
            f"fixture not found at {fixture}.\nRegenerate it with: python scripts/make_fixture.py",
            file=sys.stderr,
        )
        return 1

    try:
        target = _stage_fixture(fixture, settings.media_root)
    except OSError as exc:
        print(f"could not stage the fixture into {settings.media_root}: {exc}", file=sys.stderr)
        return 1

    engine = build_engine(settings.database_url)
    factory = build_session_factory(engine)
    principal = Principal(organization_id=organization_id)
    try:
        from .services.resource_service import CameraService

        cameras = CameraService(factory, media_root=settings.media_root)
        with factory() as session:
            site = session.query(Site).filter(Site.name == "Demo Site").one_or_none()
            if site is None:
                site = cameras.create_site(principal, {"name": "Demo Site"})
            camera = session.query(Camera).filter(Camera.name == "Demo Camera").one_or_none()
            if camera is None:
                camera = cameras.create_camera(
                    principal, {"name": "Demo Camera", "site_id": site.id}
                )
            source = (
                session.query(VideoSource).filter(VideoSource.camera_id == camera.id).one_or_none()
            )
            if source is None:
                cameras.add_source(
                    principal,
                    camera.id,
                    {
                        "name": "synthetic fixture",
                        "kind": "file",
                        "target": target,
                        "is_primary": True,
                        "options": {"loop": True},
                    },
                )
                session.expire_all()
            zone = session.query(Zone).filter(Zone.camera_id == camera.id).one_or_none()
            if zone is None:
                from .services.resource_service import ZoneService

                zones = ZoneService(factory)
                zones.create(
                    principal,
                    {
                        "camera_id": camera.id,
                        "name": "restricted",
                        "kind": "polygon",
                        "points": [[0.34, 0.30], [0.70, 0.30], [0.70, 0.92], [0.34, 0.92]],
                        "color": "#f97316",
                        "severity": "warning",
                    },
                )
        print("demo data ready:")
        print(f"  site    {site.name}")
        print(f"  camera  {camera.name}")
        print(f"  source  synthetic fixture ({target} under {settings.media_root})")
        print("  zone    restricted")
        print("")
        print("Start it:  make dev   then open the Camera page and press Start session")
        return 0
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="visionops",
        description="VisionOps — computer-vision operations platform",
    )
    parser.add_argument("--env-file", help="path to a .env file to load before running")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the API server")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--reload", action="store_true", help="development auto-reload")
    serve.set_defaults(func=cmd_serve)

    init = sub.add_parser("init", help="create directories, migrate and bootstrap")
    init.set_defaults(func=cmd_init)

    key = sub.add_parser("key", help="manage API keys")
    key_sub = key.add_subparsers(dest="key_command")
    key.set_defaults(func=cmd_key)
    add = key_sub.add_parser("add", help="create a key")
    add.add_argument("--name", default="ops-console")
    add.add_argument("--permission", action="append", help="repeatable; defaults to *")
    key_list = key_sub.add_parser("list", help="list keys")
    key_list.set_defaults(list=True)
    revoke = key_sub.add_parser("revoke", help="revoke a key by id")
    revoke.add_argument("revoke")

    prune = sub.add_parser("prune", help="apply retention")
    prune.add_argument("--dry-run", action="store_true", help="show what would be deleted")
    prune.set_defaults(func=cmd_prune)

    migrate = sub.add_parser("migrate", help="apply schema migrations")
    migrate.add_argument("--revision", default="head")
    migrate.add_argument("--downgrade", help="roll back to this revision")
    migrate.set_defaults(func=cmd_migrate)

    model = sub.add_parser("model", help="fetch or verify the detection model")
    model.add_argument("--force", action="store_true")
    model.add_argument("--info", action="store_true", help="report the local model instead")
    model.set_defaults(func=cmd_model)

    bench = sub.add_parser("bench", help="measure performance on this host")
    bench.add_argument("--frames", type=int, default=60)
    bench.add_argument("--file", help="also benchmark a video file end to end")
    bench.add_argument("--output", help="write the JSON result here")
    bench.set_defaults(func=cmd_bench)

    demo = sub.add_parser("demo", help="seed a hardware-free demo with the fixture video")
    demo.add_argument("--fixture", help="path to a video file")
    demo.set_defaults(func=cmd_demo)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # `key` requires a subcommand parser to have run to set all its attributes;
    # normalise them here so `key`, `key list` and `key revoke <id>` all work.
    if args.command == "key":
        args.list = getattr(args, "list", False)
        args.revoke = getattr(args, "revoke", None)
        args.name = getattr(args, "name", "ops-console")
        args.permission = getattr(args, "permission", None)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:  # pragma: no cover - interactive
        print("\ninterrupted", file=sys.stderr)
        return 130


__all__ = ["build_parser", "main"]


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
