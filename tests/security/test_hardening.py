"""Security properties that must not regress.

These are deliberately behavioural rather than structural: each test states an
attack or an operational hazard and asserts the observable outcome.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from visionops.core import security
from visionops.core.auth import _permissions, rate_limit_key
from visionops.core.ratelimit import RateLimiter
from visionops.core.security import (
    DecryptionError,
    SecretBox,
    derive_key_from_passphrase,
    fingerprint,
    generate_api_key,
    generate_master_key,
    hash_secret,
    load_master_key,
    parse_api_key,
    resolve_within,
    verify_secret,
)
from visionops.db.scoped import Principal

REPO_ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------


class TestApiKeyFormat:
    def test_a_generated_key_round_trips(self) -> None:
        generated = generate_api_key()
        parsed = parse_api_key(generated.full_key)
        assert parsed is not None
        key_id, _secret = parsed
        assert key_id == generated.key_id

    def test_the_secret_never_appears_in_the_public_id(self) -> None:
        generated = generate_api_key()
        assert generated.secret not in generated.key_id

    def test_keys_are_unpredictable(self) -> None:
        secrets = {generate_api_key().secret for _ in range(50)}
        assert len(secrets) == 50

    @pytest.mark.parametrize(
        "candidate",
        [
            "",
            "not-a-key",
            "vo_short_secret",
            "vo_",
            "_secret",
            "vo_onlyid_",
            "vo_dashes_in_id_secret",
        ],
    )
    def test_malformed_keys_are_rejected_without_raising(self, candidate: str) -> None:
        assert parse_api_key(candidate) is None

    def test_the_documented_prefix_is_used(self) -> None:
        assert generate_api_key().full_key.startswith("vo_")


class TestSecretStorage:
    def test_hashing_is_salted_so_equal_secrets_differ(self) -> None:
        first = hash_secret("s3cret", "salt-a")
        second = hash_secret("s3cret", "salt-b")
        assert first != second

    def test_verification_accepts_the_right_secret(self) -> None:
        stored = hash_secret("s3cret", "salt-a")
        assert verify_secret("s3cret", "salt-a", stored) is True

    def test_verification_rejects_the_wrong_secret(self) -> None:
        stored = hash_secret("s3cret", "salt-a")
        assert verify_secret("s3cret-", "salt-a", stored) is False

    def test_verification_rejects_the_wrong_salt(self) -> None:
        stored = hash_secret("s3cret", "salt-a")
        assert verify_secret("s3cret", "salt-b", stored) is False

    def test_an_empty_candidate_never_verifies(self) -> None:
        stored = hash_secret("s3cret", "salt-a")
        assert verify_secret("", "salt-a", stored) is False

    def test_the_fingerprint_is_stable_and_hides_the_secret(self) -> None:
        generated = generate_api_key()
        assert fingerprint(generated.full_key) == fingerprint(generated.full_key)
        assert generated.secret not in fingerprint(generated.full_key)


# ---------------------------------------------------------------------------
# Credential encryption
# ---------------------------------------------------------------------------


class TestSecretBox:
    def test_a_round_trip_returns_the_plaintext(self) -> None:
        box = SecretBox(generate_master_key())
        envelope = box.encrypt("rtsp://user:pass@cam/stream", aad="camera:1")
        assert box.decrypt(envelope, aad="camera:1") == "rtsp://user:pass@cam/stream"

    def test_ciphertext_never_contains_the_plaintext(self) -> None:
        box = SecretBox(generate_master_key())
        envelope = box.encrypt("super-secret-rtsp-password", aad="camera:1")
        assert "super-secret-rtsp-password" not in envelope

    def test_equal_plaintexts_produce_different_ciphertext(self) -> None:
        box = SecretBox(generate_master_key())
        first = box.encrypt("same", aad="camera:1")
        second = box.encrypt("same", aad="camera:1")
        assert first != second, "a random nonce is required; identical ciphertext leaks equality"

    def test_the_aad_binds_the_ciphertext_to_its_owner(self) -> None:
        """Swapping an envelope onto another row must fail, not decrypt."""
        box = SecretBox(generate_master_key())
        envelope = box.encrypt("camera-one-password", aad="camera:1")
        with pytest.raises(DecryptionError):
            box.decrypt(envelope, aad="camera:2")

    def test_a_tampered_ciphertext_is_rejected(self) -> None:
        box = SecretBox(generate_master_key())
        envelope = box.encrypt("value", aad="camera:1")
        tampered = envelope[:-4] + ("AAAA" if not envelope.endswith("AAAA") else "BBBB")
        with pytest.raises(DecryptionError):
            box.decrypt(tampered, aad="camera:1")

    def test_a_different_master_key_cannot_decrypt(self) -> None:
        envelope = SecretBox(generate_master_key()).encrypt("value", aad="camera:1")
        with pytest.raises(DecryptionError):
            SecretBox(generate_master_key()).decrypt(envelope, aad="camera:1")

    def test_unicode_survives_the_round_trip(self) -> None:
        box = SecretBox(generate_master_key())
        text = "pässwörd-日本語-🔐"
        assert box.decrypt(box.encrypt(text, aad="a"), aad="a") == text


class TestMasterKey:
    def test_a_generated_key_loads(self) -> None:
        assert len(load_master_key(generate_master_key())) == security._KEY_BYTES

    def test_a_passphrase_derived_key_loads(self) -> None:
        encoded = derive_key_from_passphrase("correct horse battery staple")
        assert len(load_master_key(encoded)) == security._KEY_BYTES

    def test_the_same_passphrase_and_salt_give_the_same_key(self) -> None:
        salt = b"0123456789abcdef"
        assert derive_key_from_passphrase("pw", salt) == derive_key_from_passphrase("pw", salt)

    def test_different_passphrases_give_different_keys(self) -> None:
        assert derive_key_from_passphrase("a") != derive_key_from_passphrase("b")

    def test_each_call_without_a_salt_uses_a_fresh_salt(self) -> None:
        assert derive_key_from_passphrase("same") != derive_key_from_passphrase("same")

    @pytest.mark.parametrize("value", ["", "not base64!!", "c2hvcnQ=", "c2hvcnQ"])
    def test_a_short_or_malformed_key_is_refused(self, value: str) -> None:
        with pytest.raises(DecryptionError):
            load_master_key(value)


# ---------------------------------------------------------------------------
# Path traversal
# ---------------------------------------------------------------------------


class TestPathSafety:
    @pytest.fixture
    def root(self, tmp_path: Any) -> Any:
        root = tmp_path / "media"
        root.mkdir()
        (root / "real.jpg").write_bytes(b"x")
        return root

    def test_a_plain_name_resolves_inside_the_root(self, root: Any) -> None:
        assert resolve_within(root, "real.jpg") == root / "real.jpg"

    @pytest.mark.parametrize(
        "candidate",
        [
            "../escape.jpg",
            "../../etc/passwd",
            "nested/../../escape.jpg",
            "/etc/passwd",
        ],
    )
    def test_traversal_attempts_are_refused(self, root: Any, candidate: str) -> None:
        with pytest.raises(ValueError):
            resolve_within(root, candidate)

    def test_an_absolute_path_inside_the_root_is_accepted(self, root: Any) -> None:
        inside = root / "real.jpg"
        assert resolve_within(root, inside) == inside

    def test_a_symlink_escaping_the_root_is_refused(self, root: Any, tmp_path: Any) -> None:
        """A symlink is the bypass that string checks miss."""
        outside = tmp_path / "outside.jpg"
        outside.write_bytes(b"secret")
        link = root / "link.jpg"
        link.symlink_to(outside)
        with pytest.raises(ValueError):
            resolve_within(root, "link.jpg")

    def test_a_dot_dot_that_lands_back_inside_resolves(self, root: Any) -> None:
        """The contract is "cannot escape the root", not "no dot-dot allowed".

        ``sub/../real.jpg`` normalises to a file inside the root, so it is
        served; only an escape raises. Rejecting it would break legitimate
        stored relative paths for no security gain.
        """
        assert resolve_within(root, "sub/../real.jpg") == root / "real.jpg"

    def test_a_dot_dot_that_escapes_is_refused(self, root: Any) -> None:
        with pytest.raises(ValueError):
            resolve_within(root, "sub/../../outside.jpg")


class TestFileSourceContainment:
    """A ``file`` source must never read outside the media root.

    ``resolve_within`` is only as good as its callers: the reader used to skip
    the check for absolute targets, which turned a containment boundary into
    decoration for exactly the case that needs it.
    """

    def test_an_absolute_target_outside_the_root_never_opens(self, tmp_path: Any) -> None:
        from visionops.ingest.base import SourceOpenError
        from visionops.ingest.file import FileSource

        media = tmp_path / "media"
        media.mkdir()
        secret = tmp_path / "private.mp4"
        secret.write_bytes(b"\x00" * 64)

        source = FileSource(secret, media_root=media)
        with pytest.raises(SourceOpenError) as raised:
            source.open()
        assert raised.value.reason_code == "MEDIA_ROOT_VIOLATION"

    def test_a_relative_escape_never_opens(self, tmp_path: Any) -> None:
        from visionops.ingest.base import SourceOpenError
        from visionops.ingest.file import FileSource

        media = tmp_path / "media"
        media.mkdir()
        source = FileSource(Path("../../etc/passwd"), media_root=media)
        with pytest.raises(SourceOpenError) as raised:
            source.open()
        assert raised.value.reason_code == "MEDIA_ROOT_VIOLATION"

    def test_a_source_without_a_media_root_refuses_to_open(self, tmp_path: Any) -> None:
        """No boundary configured means no boundary to enforce, so refuse."""
        from visionops.ingest.base import SourceOpenError
        from visionops.ingest.file import FileSource

        target = tmp_path / "clip.mp4"
        target.write_bytes(b"\x00" * 8)
        with pytest.raises(SourceOpenError) as raised:
            FileSource(target, media_root=None).open()
        assert raised.value.reason_code == "MEDIA_ROOT_NOT_CONFIGURED"

    def test_the_bundled_fixture_still_opens(self, tmp_path: Any) -> None:
        """The containment check must not have broken the happy path."""
        from visionops.ingest.file import FileSource

        source = FileSource(Path("fixtures/zone-walkthrough.mp4"), media_root=REPO_ROOT / "samples")
        source.open()
        try:
            assert source.read() is not None
        finally:
            source.close()

    @pytest.mark.parametrize(
        "target",
        ["/etc/passwd", "../../etc/passwd", "sub/../../outside.mp4"],
    )
    def test_the_api_refuses_an_escaping_target_at_write_time(
        self, client: Any, auth: dict[str, str], target: str
    ) -> None:
        """Reject on the request, not later as a degraded camera."""
        site = client.post("/api/v1/sites", headers=auth, json={"name": "Containment Site"})
        camera = client.post(
            "/api/v1/cameras",
            headers=auth,
            json={"name": "cam", "site_id": site.json()["id"]},
        )
        response = client.post(
            f"/api/v1/cameras/{camera.json()['id']}/sources",
            headers=auth,
            json={"name": "escape", "kind": "file", "target": target},
        )
        assert response.status_code == 422, response.text
        assert response.json()["errors"][0]["field"] == "target"

    def test_the_api_accepts_a_target_inside_the_media_root(
        self, client: Any, auth: dict[str, str]
    ) -> None:
        site = client.post("/api/v1/sites", headers=auth, json={"name": "Fixture Site"})
        camera = client.post(
            "/api/v1/cameras",
            headers=auth,
            json={"name": "cam", "site_id": site.json()["id"]},
        )
        response = client.post(
            f"/api/v1/cameras/{camera.json()['id']}/sources",
            headers=auth,
            json={
                "name": "fixture",
                "kind": "file",
                "target": "fixtures/zone-walkthrough.mp4",
            },
        )
        assert response.status_code == 201, response.text


# ---------------------------------------------------------------------------
# Authorisation and rate limiting
# ---------------------------------------------------------------------------


class TestPermissions:
    def test_absent_permissions_mean_no_permissions(self) -> None:
        """Deny by default: a key with no scope must not inherit 'everything'."""
        assert list(_permissions(None)) == []
        assert list(_permissions([])) == []

    def test_an_unscoped_key_cannot_do_anything(self) -> None:
        """A key minted without grants must not be a full-privilege key."""
        assert (
            Principal(organization_id="o", permissions=_permissions([])).can("camera.write")
            is False
        )

    def test_permissions_are_passed_through(self) -> None:
        assert list(_permissions(["cameras:read"])) == ["cameras:read"]


class TestRateLimitKeying:
    def test_the_key_is_a_fingerprint_not_the_credential(self) -> None:
        """Otherwise every API key would land in logs and metrics labels."""
        credential = generate_api_key().full_key
        key = rate_limit_key(credential)
        assert key == fingerprint(credential)
        assert credential not in key

    def test_distinct_credentials_get_distinct_buckets(self) -> None:
        assert rate_limit_key(generate_api_key().full_key) != rate_limit_key(
            generate_api_key().full_key
        )

    def test_an_anonymous_caller_still_gets_a_bucket(self) -> None:
        assert rate_limit_key("")


class TestRateLimiting:
    def test_the_burst_is_enforced_then_refilled_over_time(self) -> None:
        limiter = RateLimiter(rate_per_second=10.0, burst=3)
        assert [limiter.allow("k") for _ in range(3)] == [True, True, True]
        assert limiter.allow("k") is False
        time.sleep(0.12)
        assert limiter.allow("k") is True

    def test_buckets_are_isolated_per_key(self) -> None:
        limiter = RateLimiter(rate_per_second=1.0, burst=1)
        assert limiter.allow("a") is True
        assert limiter.allow("a") is False
        assert limiter.allow("b") is True

    def test_retry_after_accounts_for_time_already_served(self) -> None:
        """Backing off must actually shorten the advised wait."""
        limiter = RateLimiter(rate_per_second=10.0, burst=1)
        limiter.allow("k")
        immediate = limiter.retry_after("k")
        time.sleep(0.15)
        later = limiter.retry_after("k")
        assert later <= immediate

    def test_a_full_bucket_needs_no_wait(self) -> None:
        limiter = RateLimiter(rate_per_second=1.0, burst=5)
        assert limiter.retry_after("k") == 1

    def test_reset_clears_one_bucket_or_all_of_them(self) -> None:
        limiter = RateLimiter(rate_per_second=1.0, burst=1)
        limiter.allow("a")
        limiter.allow("b")
        limiter.reset("a")
        assert limiter.allow("a") is True
        assert limiter.allow("b") is False
        limiter.reset()
        assert limiter.allow("b") is True

    def test_prune_drops_idle_buckets(self) -> None:
        limiter = RateLimiter(rate_per_second=1.0, burst=1)
        limiter.allow("k")
        limiter.prune(max_age_seconds=-1.0)
        assert limiter.allow("k") is True

    def test_allow_prunes_without_being_asked(self) -> None:
        """The sweep only ever ran when someone called ``prune()`` — nobody did.

        Buckets are created for every key the limiter is shown, so without the
        automatic sweep an unauthenticated client can grow the map without
        bound. The clock is moved by hand rather than slept through.
        """
        import time as _time

        limiter = RateLimiter(rate_per_second=1.0, burst=1)
        for i in range(50):
            limiter.allow(f"noise-{i}")
        assert len(limiter._buckets) == 50

        stale = _time.monotonic() - (RateLimiter.IDLE_BUCKET_SECONDS + 1.0)
        for bucket in limiter._buckets.values():
            bucket.updated_at = stale
        limiter._last_prune = _time.monotonic() - (RateLimiter.PRUNE_INTERVAL_SECONDS + 1.0)

        limiter.allow("live")
        assert list(limiter._buckets) == ["live"]
        # A busy interval must not sweep on every request: the sweep is the
        # thing being bounded, not just the map.
        assert limiter._last_prune == pytest.approx(_time.monotonic(), abs=1.0)


# ---------------------------------------------------------------------------
# Route authorisation
# ---------------------------------------------------------------------------


class TestRoutePermissions:
    """Every authenticated route must map to a permission.

    ``required_permission`` fails closed, so a new endpoint with no mapping
    would 403 rather than leak. These tests make that visible at build time
    instead of at runtime.
    """

    @staticmethod
    def _routes() -> list[tuple[str, str]]:
        from visionops.main import create_app

        app = create_app()
        found: list[tuple[str, str]] = []
        for path, operations in app.openapi()["paths"].items():
            for method in operations:
                found.append((method.upper(), path))
        return found

    def test_the_app_exposes_routes(self) -> None:
        assert len(self._routes()) > 20, "the OpenAPI document should list the V0.1 surface"

    def test_no_authenticated_route_is_unmapped(self) -> None:
        from visionops.api.deps import UNMAPPED, required_permission

        unmapped = sorted(
            f"{method} {path}"
            for method, path in self._routes()
            if required_permission(path, method) == UNMAPPED
        )
        assert not unmapped, (
            "these routes have no permission mapping and would be refused; add them to "
            f"ROUTE_PERMISSIONS: {unmapped}"
        )

    @pytest.mark.parametrize(
        ("path", "method", "expected"),
        [
            ("/api/v1/cameras", "GET", "camera.read"),
            ("/api/v1/cameras", "POST", "camera.write"),
            ("/api/v1/cameras/abc/session", "DELETE", "camera.write"),
            ("/api/v1/zones", "GET", "zone.read"),
            ("/api/v1/events", "GET", "event.read"),
            ("/api/v1/events/export", "GET", "event.export"),
            ("/api/v1/apikeys", "POST", "apikeys.manage"),
            ("/api/v1/system/status", "GET", "org.manage"),
            ("/api/v1/healthz", "GET", None),
            ("/api/v1/readyz", "GET", None),
        ],
    )
    def test_the_mapping_is_what_the_prd_specifies(
        self, path: str, method: str, expected: str | None
    ) -> None:
        from visionops.api.deps import required_permission

        assert required_permission(path, method) == expected

    def test_an_unmapped_api_path_is_refused_not_public(self) -> None:
        """Failing closed is the whole point: an unknown API route must 403."""
        from visionops.api.deps import UNMAPPED, required_permission

        assert required_permission("/api/v1/not-a-real-resource", "GET") == UNMAPPED

    def test_the_spa_is_public_so_the_key_prompt_can_render(self) -> None:
        from visionops.api.deps import is_public_path, required_permission

        assert is_public_path("/cameras/abc") is True
        assert is_public_path("/api/v1/cameras") is False
        assert required_permission("/cameras/abc", "GET") is None

    def test_a_wildcard_grant_satisfies_every_permission(self) -> None:
        principal = Principal(organization_id="o", permissions=("*",))
        for permission in ("camera.write", "apikeys.manage", "anything.at.all"):
            assert principal.can(permission) is True

    def test_a_narrow_grant_satisfies_only_itself(self) -> None:
        principal = Principal(organization_id="o", permissions=("camera.read",))
        assert principal.can("camera.read") is True
        assert principal.can("camera.write") is False


# ---------------------------------------------------------------------------
# Enforcement, end to end
# ---------------------------------------------------------------------------


class TestPermissionEnforcement:
    """The API boundary must honour the permission strings.

    ``docs/SECURITY_PRIVACY.md`` promises "deny-by-default, enforced at the API
    boundary". These tests are the proof; the unit tests above only prove the
    pieces exist.
    """

    def test_a_read_only_key_can_read_but_not_write(self, client: Any, mint_key: Any) -> None:
        read_only = mint_key(["camera.read", "zone.read", "event.read"], name="reader")
        headers = {"X-API-Key": read_only}

        assert client.get("/api/v1/cameras", headers=headers).status_code == 200

        denied = client.post("/api/v1/sites", headers=headers, json={"name": "should-not-exist"})
        assert denied.status_code == 403
        assert denied.json()["code"] == "forbidden"

    def test_a_read_only_key_cannot_export_events(self, client: Any, mint_key: Any) -> None:
        """Export is a distinct, higher-risk permission than read."""
        read_only = mint_key(["event.read"], name="reader2")
        response = client.get("/api/v1/events/export", headers={"X-API-Key": read_only})
        assert response.status_code == 403

    def test_a_key_with_export_permission_can_export(self, client: Any, mint_key: Any) -> None:
        exporter = mint_key(["event.read", "event.export"], name="exporter")
        assert (
            client.get("/api/v1/events/export", headers={"X-API-Key": exporter}).status_code == 200
        )

    def test_an_unscoped_key_can_do_nothing(self, client: Any, mint_key: Any) -> None:
        unscoped = mint_key([], name="empty")
        assert client.get("/api/v1/cameras", headers={"X-API-Key": unscoped}).status_code == 403
        assert client.get("/api/v1/zones", headers={"X-API-Key": unscoped}).status_code == 403

    def test_a_camera_only_key_cannot_manage_api_keys(self, client: Any, mint_key: Any) -> None:
        """Privilege escalation guard: managing keys must need its own grant."""
        limited = mint_key(["camera.read", "camera.write"], name="cameras-only")
        response = client.post(
            "/api/v1/apikeys", headers={"X-API-Key": limited}, json={"name": "escalate"}
        )
        assert response.status_code == 403

    def test_a_wildcard_key_still_works_everywhere(self, client: Any, api_key: str) -> None:
        headers = {"X-API-Key": api_key}
        assert client.get("/api/v1/cameras", headers=headers).status_code == 200
        assert client.get("/api/v1/zones", headers=headers).status_code == 200
        assert client.get("/api/v1/system/status", headers=headers).status_code == 200

    def test_health_endpoints_stay_public(self, client: Any) -> None:
        """A fresh install must be probeable before any key exists."""
        assert client.get("/api/v1/healthz").status_code == 200
        assert client.get("/api/v1/readyz").status_code in (200, 503)
