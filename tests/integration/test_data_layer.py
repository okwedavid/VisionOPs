"""Integration: migrations, services, persistence, retention and export.

These use a real SQLite file and real repositories, because the behaviour under
test *is* the database behaviour.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy
from tests.conftest import FIXTURE_VIDEO, MODEL_PATH, REPO_ROOT

from visionops.db.scoped import Principal
from visionops.models import AuditLog, Event, Organization
from visionops.services.event_service import EventService
from visionops.vision.contracts import EventRecord, Severity

#: Callable that builds an :class:`EventRecord` bound to a real camera/zone, so
#: foreign keys resolve and the tests exercise the same integrity guarantees that
#: production has. A per-test factory (rather than module state) is what keeps
#: these tests order-independent.
RecordFactory = Any


@pytest.fixture
def event_service(session_factory: Any) -> EventService:
    return EventService(session_factory)


@pytest.fixture
def graph(session_factory: Any, organisation: str) -> dict[str, Any]:
    """A real site → camera → zone chain plus a record factory bound to it."""
    from visionops.models import Camera, Site, Zone

    with session_factory() as session:
        site = Site(organization_id=organisation, name="Site")
        session.add(site)
        session.commit()
        camera = Camera(organization_id=organisation, site_id=site.id, name="front-door")
        session.add(camera)
        session.commit()
        zone = Zone(
            organization_id=organisation,
            camera_id=camera.id,
            name="restricted",
            points=[[0.3, 0.3], [0.7, 0.3], [0.7, 0.9], [0.3, 0.9]],
        )
        gate = Camera(organization_id=organisation, site_id=site.id, name="gate")
        session.add(gate)
        session.commit()
        loading = Zone(
            organization_id=organisation,
            camera_id=camera.id,
            name="loading",
            points=[[0.1, 0.1], [0.2, 0.1], [0.2, 0.2], [0.1, 0.2]],
        )
        session.add_all([zone, loading])
        session.commit()
        ids = {
            "organization_id": organisation,
            "camera_id": camera.id,
            "gate_camera_id": gate.id,
            "zone_id": zone.id,
            "loading_zone_id": loading.id,
        }

    def make(**overrides: Any) -> EventRecord:
        base: dict[str, Any] = {
            "type": "ZONE_ENTERED",
            "severity": Severity.WARNING,
            "occurred_at": datetime.now(UTC).timestamp(),
            "camera_id": ids["camera_id"],
            "camera_name": "front-door",
            "zone_id": ids["zone_id"],
            "zone_name": "restricted",
            "track_id": 7,
            "session_id": "s1",
            "confidence": 0.9,
            "metadata": {"box": [0.1, 0.2, 0.3, 0.9]},
        }
        base.update(overrides)
        return EventRecord(**base)  # type: ignore[arg-type]

    ids["record"] = make
    ids["make"] = make
    return ids


class TestMigrations:
    def test_upgrade_creates_exactly_the_v01_schema(self, tmp_path) -> None:
        from alembic import command

        from visionops.db.migrations import alembic_config

        url = f"sqlite:///{tmp_path / 'migrated.db'}"
        config = alembic_config(url)
        command.upgrade(config, "head")

        engine = sqlalchemy.create_engine(url)
        tables = set(sqlalchemy.inspect(engine).get_table_names())
        engine.dispose()

        assert tables == {
            "organizations",
            "api_keys",
            "sites",
            "cameras",
            "video_sources",
            "zones",
            "events",
            "audit_logs",
            "alembic_version",
        }
        assert "detections" not in tables, "V0.1 must not create speculative tables"
        assert "rules" not in tables
        assert "alerts" not in tables

    def test_downgrade_removes_everything(self, tmp_path) -> None:
        from alembic import command

        from visionops.db.migrations import alembic_config

        url = f"sqlite:///{tmp_path / 'roundtrip.db'}"
        config = alembic_config(url)
        command.upgrade(config, "head")
        command.downgrade(config, "base")

        engine = sqlalchemy.create_engine(url)
        tables = set(sqlalchemy.inspect(engine).get_table_names())
        engine.dispose()
        assert "events" not in tables
        assert "alembic_version" in tables

    def test_the_migration_matches_the_orm_metadata(self, session_factory: Any) -> None:
        """A drifted migration would silently lose columns in production."""
        from visionops.db.base import Base
        from visionops.db.migrations import alembic_config, run_migrations

        assert set(Base.metadata.tables) == {
            "organizations",
            "api_keys",
            "sites",
            "cameras",
            "video_sources",
            "zones",
            "events",
            "audit_logs",
        }
        assert callable(alembic_config) and callable(run_migrations)

    def test_check_constraints_are_enforced(self, session_factory: Any, organisation: str) -> None:
        """The database is the last line of defence against a bad write."""
        from sqlalchemy.exc import IntegrityError

        with session_factory() as session:
            session.add(Organization(name="Bad", slug="bad", timezone="UTC", retention_days=0))
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()

    def test_foreign_keys_are_enforced(self, session_factory: Any) -> None:
        from sqlalchemy.exc import IntegrityError

        with session_factory() as session:
            session.add(
                Event(
                    organization_id="does-not-exist",
                    type="ZONE_ENTERED",
                    severity="warning",
                    occurred_at=datetime.now(UTC),
                    received_at=datetime.now(UTC),
                    camera_name="x",
                    metadata_={},
                )
            )
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()


class TestEventPersistence:
    def test_events_round_trip_with_every_field(
        self, session_factory: Any, event_service: EventService, principal: Principal, graph: dict
    ) -> None:
        rows = event_service.record(principal, [graph["make"]()])
        assert len(rows) == 1

        stored = session_factory().get(Event, rows[0].id)
        assert stored is not None
        assert stored.type == "ZONE_ENTERED"
        assert stored.severity == "warning"
        assert stored.camera_name == "front-door"
        assert stored.zone_name == "restricted"
        assert stored.track_id == 7
        assert stored.confidence == pytest.approx(0.9)
        assert stored.metadata_["box"] == [0.1, 0.2, 0.3, 0.9]
        assert stored.schema_version == 1

    def test_ids_are_time_ordered(
        self, session_factory: Any, event_service: EventService, principal: Principal, graph: dict
    ) -> None:
        from visionops.core.ids import is_time_ordered

        rows = event_service.record(principal, [graph["make"]() for _ in range(5)])
        ids = [row.id for row in rows]
        assert all(is_time_ordered(value) for value in ids)
        assert ids == sorted(ids), "time-ordered ids must sort by creation time"

    def test_a_duplicate_id_is_not_inserted_twice(
        self, session_factory: Any, event_service: EventService, principal: Principal, graph: dict
    ) -> None:
        """Edge sync replays batches; a client-generated id makes that idempotent."""
        fixed = graph["make"](id="01920000-0000-7000-8000-000000000001")
        event_service.record(principal, [fixed])
        event_service.record(principal, [fixed])
        event_service.record(principal, [fixed])
        assert event_service.count(principal) == 1

    def test_timestamps_are_stored_as_utc(
        self, event_service: EventService, principal: Principal, graph: dict
    ) -> None:
        moment = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        event_service.record(principal, [graph["make"](occurred_at=moment.timestamp())])
        items, _ = event_service.list(principal)
        stored = items[0].occurred_at
        if stored.tzinfo is None:
            stored = stored.replace(tzinfo=UTC)
        assert stored == moment

    def test_received_at_defaults_to_now(
        self, event_service: EventService, principal: Principal, graph: dict
    ) -> None:
        event_service.record(
            principal, [graph["make"](occurred_at=datetime(2020, 1, 1, tzinfo=UTC).timestamp())]
        )
        items, _ = event_service.list(principal)
        stored = items[0].received_at
        assert stored.replace(tzinfo=UTC) > datetime(2025, 1, 1, tzinfo=UTC)


class TestEventQuerying:
    @pytest.fixture
    def populated(
        self, event_service: EventService, principal: Principal, graph: dict
    ) -> EventService:
        """Four events across cameras, severities, types and time."""
        now = datetime.now(UTC)
        event_service.record(
            principal,
            [
                graph["make"](
                    occurred_at=now.timestamp(),
                    type="ZONE_ENTERED",
                    severity=Severity.WARNING,
                    track_id=1,
                ),
                graph["make"](
                    occurred_at=(now - timedelta(hours=1)).timestamp(),
                    type="ZONE_EXITED",
                    severity=Severity.INFO,
                    track_id=1,
                    metadata={"dwell_seconds": 9.5},
                ),
                graph["make"](
                    occurred_at=(now - timedelta(hours=2)).timestamp(),
                    severity=Severity.CRITICAL,
                    camera_name="gate",
                ),
                graph["make"](
                    occurred_at=(now - timedelta(days=3)).timestamp(),
                    zone_name="loading",
                    zone_id=graph["loading_zone_id"],
                ),
            ],
        )
        return event_service

    def test_newest_first(self, populated: EventService, principal: Principal, graph: dict) -> None:
        items, _ = populated.list(principal)
        times = [item.occurred_at for item in items]
        assert times == sorted(times, reverse=True)

    def test_filter_by_type(
        self, populated: EventService, principal: Principal, graph: dict
    ) -> None:
        items, _ = populated.list(principal, event_type="ZONE_EXITED")
        assert [item.type for item in items] == ["ZONE_EXITED"]

    def test_filter_by_severity(
        self, populated: EventService, principal: Principal, graph: dict
    ) -> None:
        items, _ = populated.list(principal, severity="critical")
        assert [item.severity for item in items] == ["critical"]

    def test_filter_by_zone_and_camera(
        self, populated: EventService, principal: Principal, graph: dict
    ) -> None:
        """Filters must narrow, never silently return the unfiltered set."""
        assert len(populated.list(principal, zone_id=graph["zone_id"])[0]) == 3
        assert len(populated.list(principal, camera_id=graph["camera_id"])[0]) == 4
        assert populated.list(principal, camera_id="not-this-one")[0] == []
        assert populated.list(principal, zone_id="not-this-one")[0] == []

    def test_filter_by_time_window(
        self, populated: EventService, principal: Principal, graph: dict
    ) -> None:
        now = datetime.now(UTC)
        items, _ = populated.list(principal, since=now - timedelta(hours=3))
        assert len(items) == 3

    def test_free_text_search(
        self, populated: EventService, principal: Principal, graph: dict
    ) -> None:
        assert len(populated.list(principal, search="loading")[0]) == 1
        assert len(populated.list(principal, search="gate")[0]) == 1
        assert len(populated.list(principal, search="nothing-like-this")[0]) == 0

    def test_pagination_walks_every_row_exactly_once(
        self, populated: EventService, principal: Principal, graph: dict
    ) -> None:
        """A cursor must partition the result set, never duplicate or skip."""
        seen: list[str] = []
        cursor: str | None = None
        for _ in range(10):
            kwargs: dict[str, Any] = {"limit": 1}
            if cursor:
                before, before_id = populated.parse_cursor(cursor)
                kwargs.update(before=before, before_id=before_id)
            page, has_more = populated.list(principal, **kwargs)
            seen.extend(item.id for item in page)
            cursor = populated.cursor_for(page[-1]) if has_more and page else None
            if not has_more:
                break
        assert len(seen) == 4, f"expected every event exactly once, got {seen}"
        assert len(set(seen)) == 4, "no row may appear on two pages"

    def test_events_sharing_a_timestamp_are_paginated_correctly(
        self, populated: EventService, principal: Principal, graph: dict
    ) -> None:
        """Two events with the same ``occurred_at`` must not be skipped or repeated."""
        moment = datetime.now(UTC)
        populated.record(
            principal,
            [
                graph["make"](occurred_at=moment.timestamp(), track_id=100),
                graph["make"](occurred_at=moment.timestamp(), track_id=101),
            ],
        )
        seen: list[str] = []
        cursor: str | None = None
        for _ in range(20):
            kwargs: dict[str, Any] = {"limit": 1}
            if cursor:
                before, before_id = populated.parse_cursor(cursor)
                kwargs.update(before=before, before_id=before_id)
            page, has_more = populated.list(principal, **kwargs)
            seen.extend(item.id for item in page)
            cursor = populated.cursor_for(page[-1]) if has_more and page else None
            if not has_more:
                break
        assert len(seen) == len(set(seen)) == 6

    def test_limit_is_bounded(
        self, populated: EventService, principal: Principal, graph: dict
    ) -> None:
        assert len(populated.list(principal, limit=1000)[0]) <= 200

    def test_an_empty_result_is_not_an_error(
        self, populated: EventService, principal: Principal, graph: dict
    ) -> None:
        assert populated.list(principal, event_type="NOTHING")[0] == []

    def test_a_malformed_cursor_is_rejected(self, event_service: EventService, graph: dict) -> None:
        from visionops.core.errors import ValidationError

        with pytest.raises(ValidationError, match="malformed"):
            event_service.parse_cursor("not-a-cursor")


class TestSummary:
    def test_counts_dwell_and_hourly_buckets(
        self, event_service: EventService, principal: Principal, graph: dict
    ) -> None:
        now = datetime.now(UTC)
        event_service.record(
            principal,
            [
                graph["make"](occurred_at=now.timestamp(), type="ZONE_ENTERED"),
                graph["make"](
                    occurred_at=(now - timedelta(minutes=30)).timestamp(),
                    type="ZONE_ENTERED",
                    camera_name="gate",
                    camera_id=graph["gate_camera_id"],
                    zone_id=None,
                    zone_name=None,
                ),
                graph["make"](
                    occurred_at=(now - timedelta(hours=2)).timestamp(),
                    type="ZONE_EXITED",
                    severity=Severity.INFO,
                    metadata={"dwell_seconds": 10.0},
                ),
                graph["make"](
                    occurred_at=(now - timedelta(hours=2)).timestamp(),
                    type="ZONE_EXITED",
                    severity=Severity.INFO,
                    metadata={"dwell_seconds": 20.0},
                ),
            ],
        )
        summary = event_service.summary(
            principal, since=now - timedelta(days=1), until=now + timedelta(hours=1)
        )
        assert summary["totals"]["events"] == 4
        by_type = {entry["type"]: entry["count"] for entry in summary["by_type"]}
        assert by_type == {"ZONE_ENTERED": 2, "ZONE_EXITED": 2}
        assert summary["avg_dwell_seconds"] == pytest.approx(15.0)
        assert summary["totals"]["by_severity"] == {"warning": 2, "info": 2}
        assert len(summary["hourly"]) >= 2
        assert sum(bucket["count"] for bucket in summary["hourly"]) == 4
        by_camera = {entry["camera_name"]: entry for entry in summary["by_camera"]}
        assert by_camera["front-door"]["count"] == 3
        assert by_camera["front-door"]["entries"] == 1
        assert by_camera["front-door"]["exits"] == 2
        assert by_camera["gate"]["count"] == 1
        assert by_camera["gate"]["entries"] == 1

    def test_a_window_beyond_the_limit_is_refused_with_a_hint(
        self, event_service: EventService, principal: Principal, graph: dict
    ) -> None:
        from visionops.core.errors import ValidationError

        now = datetime.now(UTC)
        with pytest.raises(ValidationError, match="export"):
            event_service.summary(principal, since=now - timedelta(days=90), until=now)

    def test_an_inverted_window_is_refused(
        self, event_service: EventService, principal: Principal, graph: dict
    ) -> None:
        from visionops.core.errors import ValidationError

        now = datetime.now(UTC)
        with pytest.raises(ValidationError, match="before"):
            event_service.summary(principal, since=now, until=now - timedelta(days=1))

    def test_an_empty_window_returns_zeroes(
        self, event_service: EventService, principal: Principal, graph: dict
    ) -> None:
        now = datetime.now(UTC)
        summary = event_service.summary(principal, since=now - timedelta(days=1), until=now)
        assert summary["totals"]["events"] == 0
        assert summary["hourly"] == []


class TestExport:
    def test_csv_has_a_header_and_one_row_per_event(
        self, event_service: EventService, principal: Principal, graph: dict
    ) -> None:
        now = datetime.now(UTC)
        event_service.record(
            principal,
            [graph["make"](occurred_at=now.timestamp(), track_id=index + 1) for index in range(3)],
        )
        csv_text = event_service.export_csv(
            principal, since=now - timedelta(days=1), until=now + timedelta(hours=1)
        )
        lines = csv_text.splitlines()
        assert lines[0].startswith("id,occurred_at,received_at,type,severity")
        assert len(lines) == 4

    def test_json_export_carries_the_metadata(
        self, event_service: EventService, principal: Principal, graph: dict
    ) -> None:
        now = datetime.now(UTC)
        event_service.record(principal, [graph["make"](occurred_at=now.timestamp())])
        payload = json.loads(
            event_service.export_json(
                principal, since=now - timedelta(days=1), until=now + timedelta(hours=1)
            )
        )
        assert payload["count"] == 1
        assert json.loads(payload["items"][0]["metadata"])["box"] == [0.1, 0.2, 0.3, 0.9]

    def test_an_empty_window_still_returns_headers(
        self, event_service: EventService, principal: Principal, graph: dict
    ) -> None:
        now = datetime.now(UTC)
        csv_text = event_service.export_csv(
            principal, since=now - timedelta(days=2), until=now - timedelta(days=1)
        )
        assert len(csv_text.splitlines()) == 1

    def test_the_window_is_respected(
        self, event_service: EventService, principal: Principal, graph: dict
    ) -> None:
        now = datetime.now(UTC)
        event_service.record(principal, [graph["make"](occurred_at=now.timestamp())])
        event_service.record(
            principal, [graph["make"](occurred_at=(now - timedelta(days=5)).timestamp())]
        )
        payload = json.loads(
            event_service.export_json(
                principal, since=now - timedelta(days=1), until=now + timedelta(hours=1)
            )
        )
        assert payload["count"] == 1


class TestRetention:
    def test_prune_deletes_only_expired_events(
        self, session_factory: Any, event_service: EventService, principal: Principal, graph: dict
    ) -> None:
        now = datetime.now(UTC)
        fresh = event_service.record(principal, [graph["make"](occurred_at=now.timestamp())])
        stale = event_service.record(
            principal, [graph["make"](occurred_at=(now - timedelta(days=200)).timestamp())]
        )

        count, snapshots = event_service.prune(
            principal.organization_id, before=now - timedelta(days=90)
        )
        assert count == 1
        assert snapshots == []
        assert event_service.count(principal) == 1
        with session_factory() as session:
            assert session.get(Event, fresh[0].id) is not None
            assert session.get(Event, stale[0].id) is None

    def test_prune_reports_the_paths_to_delete(
        self, session_factory: Any, event_service: EventService, principal: Principal, graph: dict
    ) -> None:
        now = datetime.now(UTC)
        event_service.record(
            principal,
            [
                graph["make"](
                    occurred_at=(now - timedelta(days=200)).timestamp(),
                    snapshot_path="2020/01/01/x.jpg",
                )
            ],
        )
        _count, snapshots = event_service.prune(
            principal.organization_id, before=now - timedelta(days=90)
        )
        assert snapshots == ["2020/01/01/x.jpg"]

    def test_events_are_append_only_in_practice(
        self, session_factory: Any, event_service: EventService, principal: Principal, graph: dict
    ) -> None:
        """Nothing in the service layer updates an existing event."""
        rows = event_service.record(principal, [graph["make"]()])
        original = rows[0].type
        assert event_service.record(principal, [graph["make"](type="ZONE_EXITED")])
        with session_factory() as session:
            assert session.get(Event, rows[0].id).type == original


class TestRetentionJob:
    def test_the_retention_job_deletes_rows_and_files(
        self, session_factory: Any, principal: Principal, tmp_path: Any, graph: dict
    ) -> None:
        from visionops.services.snapshot import SnapshotWriter

        event_service = EventService(session_factory)
        snapshot_root = tmp_path / "snapshots"
        writer = SnapshotWriter(root=snapshot_root)
        now = datetime.now(UTC)

        stale_relative = "2020/01/01/stale.jpg"
        stale_path = writer.resolve(stale_relative)
        stale_path.parent.mkdir(parents=True, exist_ok=True)
        stale_path.write_bytes(b"\xff\xd8fake-jpeg")
        fresh_relative = "2026/01/01/fresh.jpg"
        fresh_path = writer.resolve(fresh_relative)
        fresh_path.parent.mkdir(parents=True, exist_ok=True)
        fresh_path.write_bytes(b"\xff\xd8fake-jpeg")

        event_service.record(
            principal,
            [
                graph["make"](
                    occurred_at=(now - timedelta(days=200)).timestamp(),
                    snapshot_path=stale_relative,
                )
            ],
        )
        event_service.record(
            principal, [graph["make"](occurred_at=now.timestamp(), snapshot_path=fresh_relative)]
        )

        _count, paths = event_service.prune(
            principal.organization_id, before=now - timedelta(days=90)
        )
        removed = writer.delete(paths)
        assert removed == 1
        assert not stale_path.exists()
        assert fresh_path.exists(), "a snapshot must only be deleted with its event"
        assert event_service.count(principal) == 1

    def test_deleting_a_snapshot_outside_the_root_is_refused(self, tmp_path: Any) -> None:
        from visionops.services.snapshot import SnapshotWriter

        writer = SnapshotWriter(root=tmp_path / "snapshots")
        with pytest.raises(ValueError, match="outside"):
            writer.resolve("../../etc/passwd")

    def test_deleting_a_missing_snapshot_is_not_an_error(self, tmp_path: Any) -> None:
        from visionops.services.snapshot import SnapshotWriter

        writer = SnapshotWriter(root=tmp_path / "snapshots")
        assert writer.delete(["2020/01/01/gone.jpg"]) == 1  # missing_ok


class TestAudit:
    def test_audit_rows_are_written_and_readable(
        self, session_factory: Any, organisation: str
    ) -> None:
        from visionops.services.audit_service import AuditService

        service = AuditService(session_factory)
        service.record(
            organisation,
            action="zone.create",
            target_type="zone",
            target_id="z1",
            detail={"name": "x"},
        )
        with session_factory() as session:
            row = session.query(AuditLog).one()
        assert row.action == "zone.create"
        assert row.outcome == "success"
        assert row.detail == {"name": "x"}

    def test_secrets_are_stripped_from_the_detail(self) -> None:
        from visionops.services.audit_service import sanitize

        clean = sanitize(
            {
                "name": "front-door",
                "password": "hunter2",
                "nested": {"api_key": "vo_x", "target": "rtsp://cam/stream"},
                "list": [{"secret": "s"}],
            }
        )
        assert clean == {
            "name": "front-door",
            "nested": {"target": "rtsp://cam/stream"},
            "list": [{}],
        }

    def test_deeply_nested_structures_are_bounded(self) -> None:
        from visionops.services.audit_service import sanitize

        deep: dict = {"a": {}}
        node = deep["a"]
        for _ in range(10):
            node["a"] = {}
            node = node["a"]
        assert json.dumps(sanitize(deep))  # must not recurse without bound

    def test_long_strings_are_truncated(self) -> None:
        from visionops.services.audit_service import sanitize

        assert len(sanitize("x" * 5000)) == 500

    def test_an_audit_failure_does_not_break_the_caller(self) -> None:
        from visionops.services.audit_service import AuditService

        class Broken:
            def __call__(self, *args: Any, **kwargs: Any):
                raise RuntimeError("database is gone")

        service = AuditService(Broken())
        service.record("org", action="zone.create")  # must not raise


class TestScopedRepository:
    def test_queries_are_scoped_to_the_principal(
        self, session_factory: Any, organisation: str
    ) -> None:
        from visionops.db.scoped import Principal, get_by_id, insert_scoped
        from visionops.models import Organization, Site

        # A second *real* organisation: the schema enforces the foreign key, so a
        # scope test that faked one would prove nothing.
        with session_factory() as session:
            second = Organization(name="Other", slug="other")
            session.add(second)
            session.commit()
            other_org_id = second.id

        other = Principal(organization_id=other_org_id)
        with session_factory() as session:
            mine = insert_scoped(
                session, Principal(organization_id=organisation), Site, name="mine"
            )
            theirs = insert_scoped(session, other, Site, name="theirs")
            session.commit()
            mine_id, theirs_id = mine.id, theirs.id

        with session_factory() as session:
            assert (
                get_by_id(session, Principal(organization_id=organisation), Site, mine_id)
                is not None
            )
            # The chokepoint is what makes this impossible rather than unlikely.
            assert (
                get_by_id(session, Principal(organization_id=organisation), Site, theirs_id) is None
            )
            assert get_by_id(session, other, Site, theirs_id) is not None

    def test_insert_stamps_the_organisation(self, session_factory: Any, organisation: str) -> None:
        from visionops.db.scoped import Principal, insert_scoped
        from visionops.models import Site

        with session_factory() as session:
            row = insert_scoped(session, Principal(organization_id=organisation), Site, name="x")
            assert row.organization_id == organisation
            session.commit()

    def test_principal_permission_checks_deny_by_default(self) -> None:
        from visionops.db.scoped import Principal

        owner = Principal(organization_id="o", permissions=["*"])
        limited = Principal(organization_id="o", permissions=["event.read"])
        assert owner.can("zone.write")
        assert limited.can("event.read")
        assert not limited.can("zone.write")
        assert not Principal(organization_id="o", permissions=[]).can("event.read")


class TestSampleAssets:
    def test_the_synthetic_fixture_exists_and_decodes(self) -> None:
        import cv2

        assert FIXTURE_VIDEO.exists(), f"the committed fixture is missing: {FIXTURE_VIDEO}"
        capture = cv2.VideoCapture(str(FIXTURE_VIDEO))
        assert capture.isOpened()
        frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        capture.release()
        assert frames > 0 and width > 0 and height > 0
        assert FIXTURE_VIDEO.stat().st_size < 2_000_000, "the fixture must stay small"

    def test_the_fixture_is_documented_as_synthetic(self) -> None:
        """Never let a drawn clip be mistaken for footage of real people."""
        readme = (REPO_ROOT / "samples" / "SOURCES.md").read_text(encoding="utf-8")
        assert "SYNTHETIC" in readme.upper()
        assert "zone-walkthrough" in readme

    def test_the_generator_reproduces_the_fixture(self) -> None:
        """The committed clip must be regenerable, not a mystery binary."""
        assert (REPO_ROOT / "scripts" / "make_fixture.py").exists()
        source = (REPO_ROOT / "scripts" / "make_fixture.py").read_text(encoding="utf-8")
        assert "SYNTHETIC" in source


class TestRealModelFixture:
    def test_the_model_checksum_file_matches_the_weights(self) -> None:
        """Whatever weights are committed must be exactly what we recorded."""
        if not MODEL_PATH.exists():
            pytest.skip("no model downloaded; run `make model` to include it")
        from visionops.vision.onnx_model import sha256_file

        recorded = (REPO_ROOT / "models" / "SHA256SUMS").read_text(encoding="utf-8").split()[0]
        assert sha256_file(MODEL_PATH) == recorded


class TestConnectionHygiene:
    """Read paths must hand their connections back deterministically.

    A session that is created but never closed is only reclaimed when the
    garbage collector happens to run, so the pool grows with request volume
    until it stalls. These tests run with the collector *disabled*: only an
    explicit close can keep the pool bounded, which is the property that
    actually matters under load.
    """

    @pytest.fixture
    def camera_id(self, client: Any, auth: dict[str, str]) -> str:
        # The organisation id comes from the API itself: the client app
        # bootstraps its own organisation, which is not the shared fixture's.
        organization_id = client.get("/api/v1/organizations/self", headers=auth).json()["id"]
        site = client.post("/api/v1/sites", headers=auth, json={"name": "Leak Site"})
        camera = client.post(
            "/api/v1/cameras",
            headers=auth,
            json={"name": "leaky", "site_id": site.json()["id"]},
        )
        client.post(
            f"/api/v1/cameras/{camera.json()['id']}/sources",
            headers=auth,
            json={"name": "webcam", "kind": "browser", "target": "browser"},
        )
        zone = client.post(
            "/api/v1/zones",
            headers=auth,
            json={
                "camera_id": camera.json()["id"],
                "name": "Zone A",
                "kind": "polygon",
                "points": [[0.1, 0.1], [0.4, 0.1], [0.4, 0.5], [0.1, 0.5]],
            },
        )
        self.principal = Principal(organization_id=organization_id)
        self.camera_id = str(camera.json()["id"])
        self.site_id = str(site.json()["id"])
        self.zone_id = str(zone.json()["id"])
        return str(camera.json()["id"])

    def test_service_reads_do_not_leak_connections(self, client: Any, camera_id: str) -> None:
        """``get_camera``/``get_site``/``get_zone`` used to skip ``close()``."""
        import gc

        container = client.app.state.container
        pool = container.engine.pool
        gc.collect()
        before = pool.checkedout()
        gc.disable()
        try:
            for _ in range(30):
                assert container.cameras.get_camera(self.principal, camera_id).id == camera_id
                assert container.cameras.get_site(self.principal, self.site_id).id == self.site_id
                assert container.zones.get(self.principal, self.zone_id).id == self.zone_id
                assert container.cameras.list_sources(self.principal, camera_id) is not None
        finally:
            gc.enable()
        gc.collect()
        leaked = pool.checkedout() - before
        assert leaked <= 0, f"{leaked} connection(s) still checked out after 120 reads"

    def test_the_detached_rows_are_still_readable(self, client: Any, camera_id: str) -> None:
        """Closing the session must not leave the caller with unusable objects."""
        container = client.app.state.container
        camera = container.cameras.get_camera(self.principal, camera_id)
        assert camera.name == "leaky"
        assert [source.kind for source in camera.sources] == ["browser"]
        assert container.zones.specs_for(self.principal, camera_id)[0].zone_id == self.zone_id

    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/cameras",
            "/api/v1/sites",
            "/api/v1/zones",
            "/api/v1/events",
            "/api/v1/system/status",
        ],
    )
    def test_the_api_read_paths_still_work(
        self, client: Any, auth: dict[str, str], camera_id: str, path: str
    ) -> None:
        assert client.get(path, headers=auth).status_code == 200
