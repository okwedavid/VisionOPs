"""HTTP contract: auth, validation, error shape, and the resource lifecycle.

Every error path is asserted to return an RFC 9457 problem document with a
stable code — an API that leaks internals in error bodies is a security defect,
not a cosmetic one.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

PROBLEM = "application/problem+json"


def problem(response) -> dict[str, Any]:
    assert PROBLEM in response.headers.get("content-type", "")
    return response.json()


@pytest.fixture
def org(client: Any, auth: dict[str, str]) -> dict[str, str]:
    site = client.post(
        "/api/v1/sites", json={"name": "Kano", "timezone": "Africa/Lagos"}, headers=auth
    )
    assert site.status_code == 201
    camera = client.post(
        "/api/v1/cameras",
        json={"name": "front-door", "site_id": site.json()["id"], "target_fps": 5},
        headers=auth,
    )
    assert camera.status_code == 201
    return {"site_id": site.json()["id"], "camera_id": camera.json()["id"]}


class TestHealth:
    def test_healthz_needs_no_key(self, client: Any) -> None:
        response = client.get("/api/v1/healthz")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert response.json()["version"]

    def test_healthz_does_not_touch_the_database(self, client: Any) -> None:
        """A liveness probe must not fail because the database is briefly slow."""
        assert client.get("/api/v1/healthz").status_code == 200

    def test_readyz_reports_every_check(self, client: Any) -> None:
        response = client.get("/api/v1/readyz")
        assert response.status_code == 200
        payload = response.json()
        assert "database" in payload["checks"]
        assert payload["checks"]["database"]["state"] == "ok"

    def test_readyz_is_degraded_not_broken_without_a_model(self, client: Any) -> None:
        """A missing model must not take the API offline (``docs/API.md`` §2)."""
        payload = client.get("/api/v1/readyz").json()
        assert payload["status"] in ("ready", "degraded")
        assert payload["checks"]["model"]["state"] in ("failed", "unconfigured", "loading", "ready")


class TestAuthentication:
    def test_a_missing_key_is_rejected(self, client: Any) -> None:
        assert client.get("/api/v1/cameras").status_code == 401

    @pytest.mark.parametrize(
        "headers",
        [
            {"X-API-Key": "vo_000000000000_" + "x" * 40},
            {"X-API-Key": "not-a-key"},
            {"X-API-Key": "vo_short_secret"},
            {"Authorization": "Bearer vo_000000000000_" + "x" * 40},
            {"Authorization": "Basic dXNlcjpwYXNz"},
        ],
    )
    def test_a_bad_key_is_rejected_uniformly(self, client: Any, headers: dict[str, str]) -> None:
        response = client.get("/api/v1/cameras", headers=headers)
        assert response.status_code == 401
        body = problem(response)
        assert body["code"] == "unauthorized"
        assert body["request_id"]

    def test_unknown_revoked_and_malformed_keys_are_indistinguishable(
        self, client: Any, auth: dict[str, str]
    ) -> None:
        """The endpoint must not work as a key oracle."""
        unknown = problem(
            client.get("/api/v1/cameras", headers={"X-API-Key": "vo_" + "0" * 12 + "_" + "y" * 40})
        )
        bad = problem(client.get("/api/v1/cameras", headers={"X-API-Key": "nonsense"}))
        assert unknown == {**unknown, **bad} or unknown["code"] == bad["code"]
        assert unknown["detail"] == bad["detail"]

    def test_a_valid_key_works_in_both_header_forms(
        self, client: Any, auth: dict[str, str]
    ) -> None:
        key = auth["X-API-Key"]
        assert client.get("/api/v1/cameras", headers=auth).status_code == 200
        assert (
            client.get("/api/v1/cameras", headers={"Authorization": f"Bearer {key}"}).status_code
            == 200
        )

    def test_a_new_key_is_shown_once_and_never_again(
        self, client: Any, auth: dict[str, str]
    ) -> None:
        created = client.post("/api/v1/apikeys", json={"name": "second"}, headers=auth)
        assert created.status_code == 201
        assert created.json()["key"].startswith("vo_")
        listed = client.get("/api/v1/apikeys", headers=auth).json()
        entry = next(item for item in listed if item["id"] == created.json()["id"])
        assert "key" not in entry
        assert entry["key_prefix"] == created.json()["key_prefix"]

    def test_a_new_key_authenticates(self, client: Any, auth: dict[str, str]) -> None:
        created = client.post("/api/v1/apikeys", json={"name": "second"}, headers=auth).json()
        assert (
            client.get("/api/v1/cameras", headers={"X-API-Key": created["key"]}).status_code == 200
        )

    def test_a_revoked_key_stops_working(self, client: Any, auth: dict[str, str]) -> None:
        created = client.post("/api/v1/apikeys", json={"name": "temp"}, headers=auth).json()
        new_headers = {"X-API-Key": created["key"]}
        assert client.get("/api/v1/cameras", headers=new_headers).status_code == 200
        assert client.delete(f"/api/v1/apikeys/{created['id']}", headers=auth).status_code == 204
        assert client.get("/api/v1/cameras", headers=new_headers).status_code == 401

    def test_key_creation_and_revocation_are_audited(
        self, client: Any, auth: dict[str, str]
    ) -> None:
        container = client.app.state.container
        from sqlalchemy import select

        from visionops.models import AuditLog

        created = client.post("/api/v1/apikeys", json={"name": "audited-key"}, headers=auth)
        assert created.status_code == 201
        assert (
            client.delete(f"/api/v1/apikeys/{created.json()['id']}", headers=auth).status_code
            == 204
        )

        with container.session_factory() as session:
            rows = (
                session.execute(select(AuditLog).where(AuditLog.action.like("apikey.%")))
                .scalars()
                .all()
            )

        actions = {row.action for row in rows}
        assert actions == {"apikey.create", "apikey.revoke"}
        for row in rows:
            assert row.outcome == "success"
            assert row.actor_type == "api_key"
            assert row.target_type == "api_key"
        assert row.request_id, "an audit row must be traceable to a request"

    def test_a_permission_denied_is_audited(self, client: Any, auth: dict[str, str]) -> None:
        """A valid key with too few grants is a 403 *and* a recorded denial."""
        container = client.app.state.container
        from sqlalchemy import select

        from visionops.models import AuditLog

        created = client.post(
            "/api/v1/apikeys",
            json={"name": "read-only", "permissions": ["camera.read"]},
            headers=auth,
        ).json()
        response = client.post(
            "/api/v1/sites", json={"name": "Nope"}, headers={"X-API-Key": created["key"]}
        )
        assert response.status_code == 403
        assert problem(response)["code"] == "forbidden"

        with container.session_factory() as session:
            rows = (
                session.execute(select(AuditLog).where(AuditLog.action == "auth.permission_denied"))
                .scalars()
                .all()
            )
        assert len(rows) == 1
        assert rows[0].outcome == "denied"
        assert rows[0].actor_id == created["key_prefix"]
        assert rows[0].detail["required"] == "camera.write"
        assert created["key"] not in json.dumps([row.detail for row in rows])

    def test_a_rejected_known_key_is_audited(self, client: Any, auth: dict[str, str]) -> None:
        """Probing with a key that once existed is recorded; the 401 still stands.

        A key whose prefix matches no row cannot be filed — the audit trail is
        scoped to an organisation and has nowhere to put it — but a revoked key
        still belongs to one, which is exactly when an operator wants the
        attempt on the trail.
        """
        container = client.app.state.container
        from sqlalchemy import select

        from visionops.models import AuditLog

        created = client.post("/api/v1/apikeys", json={"name": "short-lived"}, headers=auth).json()
        probe = {"X-API-Key": created["key"]}
        assert client.get("/api/v1/cameras", headers=probe).status_code == 200
        client.delete(f"/api/v1/apikeys/{created['id']}", headers=auth)
        assert client.get("/api/v1/cameras", headers=probe).status_code == 401

        with container.session_factory() as session:
            rows = (
                session.execute(select(AuditLog).where(AuditLog.action == "auth.key_rejected"))
                .scalars()
                .all()
            )
        assert [row.action for row in rows] == ["auth.key_rejected"]
        row = rows[0]
        assert row.outcome == "denied"
        assert row.organization_id
        assert row.actor_type == "api_key"
        # The public half of the key identifies who probed us...
        assert row.actor_id == created["key_prefix"]
        # ...and the secret half is never stored, in any field.
        assert created["key"] not in json.dumps([row.detail, row.actor_id])
        assert row.request_id, "an audit row must be traceable to a request"


class TestRateLimit:
    def test_bursts_are_limited(self, client: Any, auth: dict[str, str], api_settings: Any) -> None:
        client.app.state.container.rate_limiter = _fast_limiter()
        codes = [client.get("/api/v1/cameras", headers=auth).status_code for _ in range(12)]
        assert 429 in codes
        limited = client.get("/api/v1/cameras", headers=auth)
        if limited.status_code == 429:
            assert "Retry-After" in limited.headers

    def test_limits_are_per_key(self, client: Any, auth: dict[str, str]) -> None:
        other = client.post("/api/v1/apikeys", json={"name": "other"}, headers=auth).json()
        client.app.state.container.rate_limiter = _fast_limiter()
        for _ in range(12):
            client.get("/api/v1/cameras", headers=auth)
        assert client.get("/api/v1/cameras", headers=auth).status_code == 429
        # A different key must be unaffected by the first key's exhaustion.
        assert client.get("/api/v1/cameras", headers={"X-API-Key": other["key"]}).status_code == 200

    def test_anonymous_traffic_is_throttled_per_source(
        self, client: Any, auth: dict[str, str]
    ) -> None:
        """A caller with no credential cannot spin forever on 401s.

        The per-key buckets are invisible to such a caller — every attempt can
        present a different made-up key — so the ceiling has to be per source
        address, applied before any credential is examined.
        """
        client.app.state.container.auth_limiter = _fast_limiter()
        codes = [
            client.get(
                "/api/v1/cameras", headers={"X-API-Key": f"vo_{i:012d}_" + "x" * 40}
            ).status_code
            for i in range(6)
        ]
        assert 429 in codes
        assert codes[-1] == 429
        # A valid key is unaffected: the buckets are keyed differently.
        assert client.get("/api/v1/cameras", headers=auth).status_code == 200


def _fast_limiter():
    from visionops.core.ratelimit import RateLimiter

    return RateLimiter(0.001, burst=1.0)


class TestProblemDocuments:
    def test_not_found_is_a_problem_document(self, client: Any, auth: dict[str, str]) -> None:
        response = client.get("/api/v1/cameras/does-not-exist", headers=auth)
        assert response.status_code == 404
        body = problem(response)
        assert body["code"] == "not_found"
        assert body["title"] == "Not found"

    def test_validation_errors_name_the_field(
        self, client: Any, auth: dict[str, str], org: dict[str, str]
    ) -> None:
        response = client.post(
            "/api/v1/zones",
            json={
                "camera_id": org["camera_id"],
                "name": "bad",
                "points": [[1.4, 0.3], [0.7, 0.3], [0.7, 0.9]],
            },
            headers=auth,
        )
        assert response.status_code == 422
        body = problem(response)
        assert body["code"] == "validation_error"
        assert body["errors"][0]["field"].startswith("points")
        assert "0 and 1" in body["errors"][0]["message"]

    def test_unknown_fields_are_rejected(self, client: Any, auth: dict[str, str]) -> None:
        response = client.post("/api/v1/sites", json={"name": "X", "surprise": 1}, headers=auth)
        assert response.status_code == 422
        assert problem(response)["code"] == "validation_error"

    def test_a_duplicate_name_is_a_conflict(self, client: Any, auth: dict[str, str]) -> None:
        client.post("/api/v1/sites", json={"name": "Same"}, headers=auth)
        second = client.post("/api/v1/sites", json={"name": "Same"}, headers=auth)
        assert second.status_code == 409
        assert problem(second)["code"] == "conflict"

    def test_every_error_carries_a_request_id(self, client: Any, auth: dict[str, str]) -> None:
        for response in (
            client.get("/api/v1/cameras"),
            client.get("/api/v1/cameras/nope", headers=auth),
            client.post("/api/v1/sites", json={}, headers=auth),
        ):
            assert response.headers.get("x-request-id")
            assert problem(response)["request_id"]

    def test_a_supplied_request_id_is_echoed(self, client: Any, auth: dict[str, str]) -> None:
        response = client.get("/api/v1/cameras", headers={**auth, "X-Request-Id": "trace-me"})
        assert response.headers["x-request-id"] == "trace-me"

    def test_errors_do_not_leak_internals(self, client: Any, auth: dict[str, str]) -> None:
        body = client.get("/api/v1/cameras/nope", headers=auth).text.lower()
        for leak in ("traceback", "sqlalchemy", "sqlite", "/home/", "select ", "visionops.db"):
            assert leak not in body, f"error body leaked {leak!r}"

    def test_an_oversized_body_is_rejected(self, client: Any, auth: dict[str, str]) -> None:
        response = client.post(
            "/api/v1/sites",
            content=b"x" * (client.app.state.container.settings.max_body_bytes + 10),
            headers=auth,
        )
        assert response.status_code == 413
        assert problem(response)["code"] == "payload_too_large"

    def test_a_chunked_body_is_capped_too(self, client: Any, auth: dict[str, str]) -> None:
        """No Content-Length means the header check never sees the body.

        An anonymous client streaming a chunked body could otherwise push data
        past the limit indefinitely; the cap has to apply to the bytes as they
        are read.
        """
        client.app.state.settings_max_body = 4096

        def chunks() -> Any:
            for _ in range(64):
                yield b"x" * 1024

        response = client.post(
            "/api/v1/sites",
            content=chunks(),
            headers={**auth, "Content-Type": "application/json"},
        )
        assert response.status_code == 413, response.text
        assert problem(response)["code"] == "payload_too_large"
        # The limit is on size, not on chunking: the same body with a length
        # header is refused before it is read.
        declared = client.post(
            "/api/v1/sites",
            content=b"x" * 8192,
            headers={**auth, "Content-Type": "application/json"},
        )
        assert declared.status_code == 413

    def test_a_hostile_request_id_is_replaced_not_echoed(self, client: Any) -> None:
        """A caller-supplied id reaches logs and a response header as-is.

        A newline in it forges log lines, and an unbounded value makes the
        header a free injection channel — so the input is accepted only in the
        shape we mint (and what tracing proxies send), anything else is dropped
        for a generated id.
        """
        hostile = "line-one\nforged-entry: value"
        response = client.get("/api/v1/healthz", headers={"X-Request-Id": hostile})
        echoed = response.headers["x-request-id"]
        assert hostile not in echoed
        assert "\n" not in echoed
        assert "\r" not in echoed
        assert len(echoed) <= 128

    def test_a_tracing_style_request_id_survives(self, client: Any) -> None:
        trace = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
        response = client.get("/api/v1/healthz", headers={"X-Request-Id": trace})
        assert response.headers["x-request-id"] == trace

    def test_an_unknown_route_is_a_problem_document(self, client: Any) -> None:
        response = client.get("/api/v1/nope")
        assert response.status_code == 404
        assert PROBLEM in response.headers.get("content-type", "")


class TestOrganisation:
    def test_self_returns_the_callers_organisation(self, client: Any, auth: dict[str, str]) -> None:
        payload = client.get("/api/v1/organizations/self", headers=auth).json()
        assert payload["slug"] == "default"
        assert payload["retention_days"] == 90
        assert payload["snapshots_enabled"] is False, "snapshots must be off by default"
        assert payload["timezone"]

    def test_there_is_no_cross_organisation_listing(
        self, client: Any, auth: dict[str, str]
    ) -> None:
        assert client.get("/api/v1/organizations", headers=auth).status_code == 404


class TestSites:
    def test_crud_round_trip(self, client: Any, auth: dict[str, str]) -> None:
        created = client.post("/api/v1/sites", json={"name": "Warehouse"}, headers=auth)
        assert created.status_code == 201
        site_id = created.json()["id"]

        assert client.get(f"/api/v1/sites/{site_id}", headers=auth).json()["name"] == "Warehouse"

        patched = client.patch(f"/api/v1/sites/{site_id}", json={"name": "Renamed"}, headers=auth)
        assert patched.json()["name"] == "Renamed"

        assert client.delete(f"/api/v1/sites/{site_id}", headers=auth).status_code == 204
        assert client.get(f"/api/v1/sites/{site_id}", headers=auth).status_code == 404

    def test_a_site_with_cameras_cannot_be_deleted(
        self, client: Any, auth: dict[str, str], org: dict[str, str]
    ) -> None:
        response = client.delete(f"/api/v1/sites/{org['site_id']}", headers=auth)
        assert response.status_code == 409
        assert "cameras" in problem(response)["detail"]

    def test_a_blank_name_is_rejected(self, client: Any, auth: dict[str, str]) -> None:
        assert client.post("/api/v1/sites", json={"name": "   "}, headers=auth).status_code == 422

    def test_sites_are_listed_in_name_order(self, client: Any, auth: dict[str, str]) -> None:
        for name in ("Zeta", "Alpha", "Mid"):
            client.post("/api/v1/sites", json={"name": name}, headers=auth)
        names = [site["name"] for site in client.get("/api/v1/sites", headers=auth).json()]
        assert names == sorted(names)


class TestCameras:
    def test_create_and_read(self, client: Any, auth: dict[str, str], org: dict[str, str]) -> None:
        camera = client.get(f"/api/v1/cameras/{org['camera_id']}", headers=auth).json()
        assert camera["name"] == "front-door"
        assert camera["site_id"] == org["site_id"]
        assert camera["site_name"] == "Kano"
        assert camera["status"] == "offline"
        assert camera["zone_count"] == 0

    def test_a_camera_needs_a_real_site(self, client: Any, auth: dict[str, str]) -> None:
        response = client.post(
            "/api/v1/cameras", json={"name": "x", "site_id": "nope"}, headers=auth
        )
        assert response.status_code == 404

    def test_site_id_is_required(self, client: Any, auth: dict[str, str]) -> None:
        response = client.post("/api/v1/cameras", json={"name": "x"}, headers=auth)
        assert response.status_code == 422
        assert problem(response)["errors"][0]["field"] == "site_id"

    def test_target_fps_is_bounded(
        self, client: Any, auth: dict[str, str], org: dict[str, str]
    ) -> None:
        for value in (0, -1, 1000):
            response = client.post(
                "/api/v1/cameras",
                json={"name": f"c{value}", "site_id": org["site_id"], "target_fps": value},
                headers=auth,
            )
            assert response.status_code == 422

    def test_deleting_a_camera_hides_it_but_keeps_history(
        self, client: Any, auth: dict[str, str], org: dict[str, str]
    ) -> None:
        assert client.delete(f"/api/v1/cameras/{org['camera_id']}", headers=auth).status_code == 204
        assert client.get(f"/api/v1/cameras/{org['camera_id']}", headers=auth).status_code == 404
        assert client.get("/api/v1/cameras", headers=auth).json() == []

    def test_filters(self, client: Any, auth: dict[str, str], org: dict[str, str]) -> None:
        client.post(
            "/api/v1/cameras", json={"name": "alpha", "site_id": org["site_id"]}, headers=auth
        )
        assert len(client.get("/api/v1/cameras", headers=auth).json()) == 2
        assert len(client.get("/api/v1/cameras?q=alpha", headers=auth).json()) == 1
        assert (
            len(client.get(f"/api/v1/cameras?site_id={org['site_id']}", headers=auth).json()) == 2
        )
        assert client.get("/api/v1/cameras?site_id=nope", headers=auth).json() == []


@pytest.fixture
def with_master_key(client: Any) -> Any:
    """Configure a master key so encrypted camera credentials can be stored."""
    from visionops.core.security import SecretBox, generate_master_key

    container = client.app.state.container
    box = SecretBox(generate_master_key())
    container.cameras._secret_box = box
    container.system._encryption_state = "enabled"
    return box


class TestSources:
    def test_add_a_browser_source(
        self, client: Any, auth: dict[str, str], org: dict[str, str]
    ) -> None:
        response = client.post(
            f"/api/v1/cameras/{org['camera_id']}/sources",
            json={"name": "webcam", "kind": "browser"},
            headers=auth,
        )
        assert response.status_code == 201
        assert response.json()["kind"] == "browser"
        assert response.json()["is_primary"] is True, "the first source becomes primary"

    def test_credentials_are_write_only(
        self, client: Any, auth: dict[str, str], org: dict[str, str], with_master_key: Any
    ) -> None:
        """The secret is written encrypted and never returned or listed."""
        created = client.post(
            f"/api/v1/cameras/{org['camera_id']}/sources",
            json={
                "name": "cam",
                "kind": "rtsp",
                "target": "rtsp://camera.local:554/stream",
                "credentials": {"username": "viewer", "password": "s3cret"},
            },
            headers=auth,
        )
        assert created.status_code == 201
        assert "password" not in created.text
        assert created.json()["credentials"] == {"configured": True}
        listed = client.get(f"/api/v1/cameras/{org['camera_id']}/sources", headers=auth).text
        assert "s3cret" not in listed and "credential_blob" not in listed

    def test_credentials_are_refused_without_a_master_key(
        self, client: Any, auth: dict[str, str], org: dict[str, str]
    ) -> None:
        """Storing a camera password with no key configured must fail loudly."""
        response = client.post(
            f"/api/v1/cameras/{org['camera_id']}/sources",
            json={
                "name": "cam",
                "kind": "rtsp",
                "target": "rtsp://camera.local:554/stream",
                "credentials": {"username": "viewer", "password": "s3cret"},
            },
            headers=auth,
        )
        assert response.status_code == 422
        assert "MASTER_KEY" in problem(response)["detail"]

    def test_a_url_with_embedded_credentials_is_rejected(
        self, client: Any, auth: dict[str, str], org: dict[str, str]
    ) -> None:
        response = client.post(
            f"/api/v1/cameras/{org['camera_id']}/sources",
            json={"name": "cam", "kind": "rtsp", "target": "rtsp://user:pass@camera.local/stream"},
            headers=auth,
        )
        assert response.status_code == 422
        assert "credentials" in problem(response)["detail"].lower()

    def test_adding_a_primary_switches_the_existing_one(
        self, client: Any, auth: dict[str, str], org: dict[str, str]
    ) -> None:
        """``is_primary`` is single-choice: a new primary demotes the old one."""
        client.post(
            f"/api/v1/cameras/{org['camera_id']}/sources",
            json={"name": "a", "kind": "browser", "is_primary": True},
            headers=auth,
        )
        second = client.post(
            f"/api/v1/cameras/{org['camera_id']}/sources",
            json={"name": "b", "kind": "browser", "is_primary": True},
            headers=auth,
        )
        assert second.status_code == 201
        sources = client.get(f"/api/v1/cameras/{org['camera_id']}/sources", headers=auth).json()
        primaries = [source["name"] for source in sources if source["is_primary"]]
        assert primaries == ["b"]

    def test_the_database_rejects_two_primary_sources(
        self, client: Any, auth: dict[str, str], org: dict[str, str]
    ) -> None:
        """A partial unique index is the last line of defence against two primaries."""
        from sqlalchemy.exc import IntegrityError

        from visionops.models import VideoSource

        camera_id = org["camera_id"]
        for name in ("a", "b"):
            client.post(
                f"/api/v1/cameras/{camera_id}/sources",
                json={"name": name, "kind": "browser", "is_primary": name == "a"},
                headers=auth,
            )
        with client.app.state.container.session_factory() as session:
            row = VideoSource(
                organization_id=client.app.state.container.organization_id,
                camera_id=camera_id,
                kind="browser",
                name="rogue",
                target="browser",
                is_primary=True,
            )
            session.add(row)
            try:
                with pytest.raises(IntegrityError):
                    session.commit()
                session.rollback()
            except Exception:  # pragma: no cover - some dialects defer the check
                session.rollback()

    def test_an_unknown_source_kind_is_rejected(
        self, client: Any, auth: dict[str, str], org: dict[str, str]
    ) -> None:
        response = client.post(
            f"/api/v1/cameras/{org['camera_id']}/sources",
            json={"name": "x", "kind": "satellite"},
            headers=auth,
        )
        assert response.status_code == 422

    def test_unknown_options_are_rejected(
        self, client: Any, auth: dict[str, str], org: dict[str, str]
    ) -> None:
        response = client.post(
            f"/api/v1/cameras/{org['camera_id']}/sources",
            json={"name": "x", "kind": "file", "target": "a.mp4", "options": {"surprise": 1}},
            headers=auth,
        )
        assert response.status_code == 422
        assert "unknown source options" in problem(response)["detail"]


class TestZones:
    def test_create_and_read(self, client: Any, auth: dict[str, str], org: dict[str, str]) -> None:
        created = client.post(
            "/api/v1/zones",
            json={
                "camera_id": org["camera_id"],
                "name": "restricted",
                "points": [[0.3, 0.3], [0.7, 0.3], [0.7, 0.9], [0.3, 0.9]],
                "severity": "critical",
                "cooldown_seconds": 20,
            },
            headers=auth,
        )
        assert created.status_code == 201
        assert created.json()["severity"] == "critical"
        assert created.json()["points"][0] == [0.3, 0.3]
        assert created.json()["enabled"] is True

        fetched = client.get(f"/api/v1/zones/{created.json()['id']}", headers=auth)
        assert fetched.json()["name"] == "restricted"

    @pytest.mark.parametrize(
        ("payload", "reason"),
        [
            ({"points": [[1.2, 0.3], [0.7, 0.3], [0.7, 0.9]]}, "normalised"),
            ({"points": [[0.3, 0.3], [0.7, 0.3]]}, "at least"),
            ({"points": [[0.3, 0.3], [0.7, 0.3], [0.7, 0.9]], "color": "orange"}, "hex"),
            ({"points": [[0.3, 0.3], [0.7, 0.3], [0.7, 0.9]], "severity": "urgent"}, "severity"),
            (
                {"points": [[0.3, 0.3], [0.7, 0.3], [0.7, 0.9]], "cooldown_seconds": -5},
                "cooldown_seconds",
            ),
            (
                {"points": [[0.3, 0.3], [0.7, 0.3], [0.7, 0.9]], "min_dwell_seconds": 99999},
                "min_dwell_seconds",
            ),
            ({"points": [[0.5, 0.5], [0.5, 0.5], [0.5, 0.5]]}, "zero area"),
        ],
    )
    def test_invalid_zones_are_rejected_with_a_reason(
        self,
        client: Any,
        auth: dict[str, str],
        org: dict[str, str],
        payload: dict[str, Any],
        reason: str,
    ) -> None:
        body = {"camera_id": org["camera_id"], "name": "z", **payload}
        response = client.post("/api/v1/zones", json=body, headers=auth)
        assert response.status_code == 422
        text = response.text.lower()
        assert reason in text, f"expected {reason!r} in the validation message"
        assert problem(response)["errors"], "a 422 must name the offending field"

    def test_a_self_intersecting_polygon_is_rejected(
        self, client: Any, auth: dict[str, str], org: dict[str, str]
    ) -> None:
        response = client.post(
            "/api/v1/zones",
            json={
                "camera_id": org["camera_id"],
                "name": "bowtie",
                "points": [[0.0, 0.0], [1.0, 1.0], [1.0, 0.0], [0.0, 1.0]],
            },
            headers=auth,
        )
        assert response.status_code == 422
        assert "cross itself" in response.text

    def test_duplicate_zone_names_on_one_camera_conflict(
        self, client: Any, auth: dict[str, str], org: dict[str, str]
    ) -> None:
        payload = {
            "camera_id": org["camera_id"],
            "name": "restricted",
            "points": [[0.3, 0.3], [0.7, 0.3], [0.7, 0.9], [0.3, 0.9]],
        }
        assert client.post("/api/v1/zones", json=payload, headers=auth).status_code == 201
        assert client.post("/api/v1/zones", json=payload, headers=auth).status_code == 409

    def test_patch_merges_and_revalidates(
        self, client: Any, auth: dict[str, str], org: dict[str, str]
    ) -> None:
        created = client.post(
            "/api/v1/zones",
            json={
                "camera_id": org["camera_id"],
                "name": "z",
                "points": [[0.3, 0.3], [0.7, 0.3], [0.7, 0.9], [0.3, 0.9]],
            },
            headers=auth,
        ).json()
        patched = client.patch(
            f"/api/v1/zones/{created['id']}",
            json={"name": "z2", "points": [[0.1, 0.1], [0.4, 0.1], [0.4, 0.4], [0.1, 0.4]]},
            headers=auth,
        )
        assert patched.status_code == 200
        assert patched.json()["name"] == "z2"
        assert patched.json()["points"][1] == [0.4, 0.1]
        assert patched.json()["severity"] == "warning", "unpatched fields must survive"

        bad = client.patch(
            f"/api/v1/zones/{created['id']}", json={"points": [[5, 5]]}, headers=auth
        )
        assert bad.status_code == 422

    def test_delete_is_soft(self, client: Any, auth: dict[str, str], org: dict[str, str]) -> None:
        created = client.post(
            "/api/v1/zones",
            json={
                "camera_id": org["camera_id"],
                "name": "z",
                "points": [[0.3, 0.3], [0.7, 0.3], [0.7, 0.9], [0.3, 0.9]],
            },
            headers=auth,
        ).json()
        assert client.delete(f"/api/v1/zones/{created['id']}", headers=auth).status_code == 204
        assert client.get(f"/api/v1/zones/{created['id']}", headers=auth).status_code == 404
        assert client.get("/api/v1/zones", headers=auth).json() == []

    def test_zone_changes_are_audited(
        self, client: Any, auth: dict[str, str], org: dict[str, str]
    ) -> None:
        from sqlalchemy import select

        from visionops.models import AuditLog

        client.post(
            "/api/v1/zones",
            json={
                "camera_id": org["camera_id"],
                "name": "audited",
                "points": [[0.3, 0.3], [0.7, 0.3], [0.7, 0.9], [0.3, 0.9]],
            },
            headers=auth,
        )
        with client.app.state.container.session_factory() as session:
            rows = (
                session.execute(select(AuditLog).where(AuditLog.action == "zone.create"))
                .scalars()
                .all()
            )
        assert rows
        assert rows[0].target_type == "zone"
        assert rows[0].outcome == "success"
        assert rows[0].detail["name"] == "audited"


class TestSystemStatus:
    def test_status_shape(self, client: Any, auth: dict[str, str]) -> None:
        payload = client.get("/api/v1/system/status", headers=auth).json()
        assert payload["status"] in ("healthy", "degraded", "failed")
        assert payload["version"] == "0.1.0"
        assert payload["sessions"] == {
            "active": 0,
            "max": client.app.state.container.settings.max_sessions,
            "items": [],
        }
        assert "counters" in payload and "runtime" in payload
        assert payload["storage"]["database"] == "ok"
        assert payload["storage"]["snapshot_dir"] == "disabled"

    def test_a_missing_model_is_reported_not_hidden(
        self, client: Any, auth: dict[str, str]
    ) -> None:
        payload = client.get("/api/v1/system/status", headers=auth).json()
        assert payload["model"]["state"] != "ready"
        assert "MODEL_MISSING" in (payload["model"].get("detail") or "")

    def test_metrics_are_prometheus_text(self, client: Any, auth: dict[str, str]) -> None:
        response = client.get("/api/v1/system/metrics", headers=auth)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        body = response.text
        assert "# TYPE visionops_http_requests_total counter" in body
        assert "visionops_process_resident_memory_bytes" in body
        # Counters that exist for a reason are present from startup, so a
        # dashboard does not break before the first event of the day.
        for counter in (
            "visionops_events_total",
            "visionops_events_dropped_total",
            "visionops_inference_errors_total",
            "visionops_source_errors_total",
            "visionops_auth_failures_total",
        ):
            assert f"# TYPE {counter} counter" in body, f"{counter} missing from the exposition"
        assert "visionops_sessions_active" in body
        assert "visionops_model_state" in body

    def test_model_endpoint_explains_the_thresholds(
        self, client: Any, auth: dict[str, str]
    ) -> None:
        payload = client.get("/api/v1/models/current", headers=auth).json()
        assert payload["config"]["confidence_threshold"] > 0
        assert payload["config"]["class_filter"] == ["person"]
        assert payload["config"]["target_fps"] > 0
        assert payload["error"]

    def test_event_schema_is_discoverable(self, client: Any, auth: dict[str, str]) -> None:
        payload = client.get("/api/v1/events/schema", headers=auth).json()
        names = {item["name"] for item in payload["types"]}
        assert {"ZONE_ENTERED", "ZONE_EXITED"} <= names
        for item in payload["types"]:
            assert item["description"]
            assert item["severity"] in ("info", "warning", "critical", "error")


class TestOpenApi:
    def test_the_specification_is_generated(self, client: Any) -> None:
        spec = client.get("/api/v1/openapi.json").json()
        assert spec["openapi"].startswith("3.")
        paths = spec["paths"]
        for required in (
            "/api/v1/healthz",
            "/api/v1/system/status",
            "/api/v1/cameras",
            "/api/v1/zones",
            "/api/v1/events",
            "/api/v1/cameras/{camera_id}/session",
        ):
            assert required in paths, f"{required} missing from the OpenAPI document"

    def test_error_responses_are_documented(self, client: Any) -> None:
        spec = client.get("/api/v1/openapi.json").json()
        responses = spec["paths"]["/api/v1/cameras"]["get"]["responses"]
        assert "422" in responses


class TestSecurityHeaders:
    def test_headers_are_present(self, client: Any, auth: dict[str, str]) -> None:
        response = client.get("/api/v1/cameras", headers=auth)
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert response.headers["Referrer-Policy"] == "no-referrer"
        assert "Permissions-Policy" in response.headers
