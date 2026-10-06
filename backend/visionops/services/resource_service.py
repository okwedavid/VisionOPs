"""Camera, source and zone use-cases, plus geometry validation."""

from __future__ import annotations

import builtins
import json
import logging
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from ..core.errors import Conflict, NotFound, ValidationError
from ..core.ids import new_id
from ..core.security import SecretBox, resolve_within
from ..db.base import utcnow
from ..db.scoped import Principal, get_by_id
from ..ingest.device import validate_remote_url
from ..ingest.factory import encode_credentials
from ..models import Camera, Site, VideoSource, Zone
from ..vision.contracts import Severity, ZoneSpec
from ..vision.geometry import is_simple_polygon, polygon_area

logger = logging.getLogger(__name__)

MAX_POLYGON_POINTS = 256
VALID_SEVERITIES = {"info", "warning", "critical"}
VALID_HEX_COLORS = set("0123456789abcdefABCDEF")


class CameraService:
    """CRUD for sites, cameras and sources, with credential handling."""

    def __init__(
        self,
        session_factory: Any,
        *,
        secret_box: SecretBox | None = None,
        media_root: Path | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._secret_box = secret_box
        self._media_root = media_root

    # -- sites --------------------------------------------------------------

    def create_site(self, principal: Principal, values: dict[str, Any]) -> Site:
        name = _require_name(values.get("name"), "site")
        with self._session_factory() as session:
            existing = session.execute(
                select(Site).where(
                    Site.organization_id == principal.organization_id, Site.name == name
                )
            ).scalar_one_or_none()
            if existing is not None:
                raise Conflict(f"a site named {name!r} already exists")
            site = Site(
                id=new_id(),
                organization_id=principal.organization_id,
                name=name,
                timezone=values.get("timezone"),
                address=values.get("address"),
                description=values.get("description"),
            )
            session.add(site)
            session.commit()
            return site

    def list_sites(self, principal: Principal) -> list[Site]:
        with self._session_factory() as session:
            return list(
                session.execute(
                    select(Site)
                    .where(Site.organization_id == principal.organization_id)
                    .order_by(Site.name)
                ).scalars()
            )

    def get_site(self, principal: Principal, site_id: str) -> Site:
        with self._session_factory() as session:
            site = get_by_id(session, principal, Site, site_id)
        if site is None:
            raise NotFound("site", site_id)
        return site

    def update_site(self, principal: Principal, site_id: str, values: dict[str, Any]) -> Site:
        with self._session_factory() as session:
            site = get_by_id(session, principal, Site, site_id)
            if site is None:
                raise NotFound("site", site_id)
            _apply(site, values, ("name", "timezone", "address", "description"))
            site.updated_at = utcnow()
            try:
                session.commit()
            except IntegrityError as exc:
                raise Conflict(f"a site named {site.name!r} already exists") from exc
            return site

    def delete_site(self, principal: Principal, site_id: str) -> None:
        with self._session_factory() as session:
            site = get_by_id(session, principal, Site, site_id)
            if site is None:
                raise NotFound("site", site_id)
            cameras = session.execute(
                select(Camera.id).where(Camera.site_id == site_id, Camera.deleted_at.is_(None))
            ).first()
            if cameras is not None:
                raise Conflict("this site still has cameras; delete or move them first")
            session.delete(site)
            session.commit()

    # -- cameras ------------------------------------------------------------

    def create_camera(self, principal: Principal, values: dict[str, Any]) -> Camera:
        name = _require_name(values.get("name"), "camera")
        site_id = values.get("site_id")
        if not site_id:
            raise ValidationError(
                "site_id is required", [{"field": "site_id", "message": "required"}]
            )
        with self._session_factory() as session:
            site = get_by_id(session, principal, Site, site_id)
            if site is None:
                raise NotFound("site", site_id)
            target_fps = _positive_number(values.get("target_fps"), 5.0, "target_fps", 0.1, 60.0)
            camera = Camera(
                id=new_id(),
                organization_id=principal.organization_id,
                site_id=site_id,
                name=name,
                description=values.get("description"),
                status="offline",
                target_fps=target_fps,
                settings=_validate_settings(values.get("settings")),
            )
            session.add(camera)
            try:
                session.commit()
            except IntegrityError as exc:
                raise Conflict(f"a camera named {name!r} already exists") from exc
            return self._get(session=session, principal=principal, camera_id=camera.id)

    def list_cameras(
        self,
        principal: Principal,
        *,
        site_id: str | None = None,
        status: str | None = None,
        search: str | None = None,
    ) -> list[Camera]:
        with self._session_factory() as session:
            statement = select(Camera).where(
                Camera.organization_id == principal.organization_id, Camera.deleted_at.is_(None)
            )
            if site_id:
                statement = statement.where(Camera.site_id == site_id)
            if status:
                statement = statement.where(Camera.status == status)
            if search:
                statement = statement.where(Camera.name.like(f"%{search.strip()}%"))
            return list(session.execute(statement.order_by(Camera.name)).unique().scalars())

    def get_camera(self, principal: Principal, camera_id: str) -> Camera:
        # The session is closed before returning on purpose: the row is fully
        # loaded (joined eager loads for zones and sources) and the engine uses
        # ``expire_on_commit=False``, so it stays readable once detached. Leaving
        # it open instead hands the connection back to the garbage collector and
        # holds a read transaction open until it runs.
        with self._session_factory() as session:
            return self._get(session=session, principal=principal, camera_id=camera_id)

    def update_camera(self, principal: Principal, camera_id: str, values: dict[str, Any]) -> Camera:
        with self._session_factory() as session:
            camera = self._get(session=session, principal=principal, camera_id=camera_id)
            _apply(
                camera,
                values,
                ("name", "description", "status", "site_id"),
            )
            if "target_fps" in values and values["target_fps"] is not None:
                camera.target_fps = _positive_number(
                    values["target_fps"], camera.target_fps, "target_fps", 0.1, 60.0
                )
            if "settings" in values and values["settings"] is not None:
                camera.settings = {**camera.settings, **_validate_settings(values["settings"])}
            camera.updated_at = utcnow()
            try:
                session.commit()
            except IntegrityError as exc:
                raise Conflict(f"a camera named {camera.name!r} already exists") from exc
            return camera

    def delete_camera(self, principal: Principal, camera_id: str) -> None:
        """Soft delete: historical events must keep referring to something real."""
        with self._session_factory() as session:
            camera = self._get(session=session, principal=principal, camera_id=camera_id)
            camera.deleted_at = utcnow()
            camera.status = "disabled"
            for zone in camera.zones:
                zone.deleted_at = utcnow()
                zone.enabled = False
            session.commit()

    def _get(self, *, session: Session, principal: Principal, camera_id: str) -> Camera:
        """Fetch a live camera with its relationships eagerly loaded.

        Eager loading is required, not an optimisation: the API serialises a
        camera after its session closes, and a detached lazy load would raise.
        """
        statement = (
            select(Camera)
            .options(joinedload(Camera.site), joinedload(Camera.sources), joinedload(Camera.zones))
            .where(
                Camera.id == camera_id,
                Camera.organization_id == principal.organization_id,
                Camera.deleted_at.is_(None),
            )
        )
        camera = session.execute(statement).unique().scalar_one_or_none()
        if camera is None:
            raise NotFound("camera", camera_id)
        return camera

    # -- sources ------------------------------------------------------------

    def add_source(
        self,
        principal: Principal,
        camera_id: str,
        values: dict[str, Any],
    ) -> VideoSource:
        kind = str(values.get("kind", "")).strip()
        target = str(values.get("target", "")).strip()
        name = _require_name(values.get("name"), "source")
        if not target and kind != "browser":
            raise ValidationError(
                "target is required", [{"field": "target", "message": "required"}]
            )
        if kind in ("rtsp", "http", "https"):
            try:
                target = validate_remote_url(target)
            except Exception as exc:  # SourceOpenError carries the reason code
                raise ValidationError(str(exc), [{"field": "target", "message": str(exc)}]) from exc
        if kind == "file":
            self._check_media_containment(target)

        with self._session_factory() as session:
            camera = self._get(session=session, principal=principal, camera_id=camera_id)
            source_id = new_id()
            options = dict(values.get("options") or {})
            username = str((values.get("credentials") or {}).get("username", ""))
            password = str((values.get("credentials") or {}).get("password", ""))
            blob = None
            if username or password:
                if self._secret_box is None:
                    raise ValidationError(
                        "VISIONOPS_MASTER_KEY is not configured, so camera credentials cannot be "
                        "stored securely. Set it and retry."
                    )
                blob = encode_credentials(username, password, self._secret_box, aad=source_id)
                options["credential_blob"] = blob
            is_primary = bool(values.get("is_primary")) or not camera.sources
            if is_primary:
                for existing in camera.sources:
                    existing.is_primary = False
            source = VideoSource(
                id=source_id,
                organization_id=principal.organization_id,
                camera_id=camera_id,
                kind=kind,
                name=name,
                target=target or "browser",
                credential_blob=blob,
                is_primary=is_primary,
                options=_validate_source_options(kind, options),
            )
            session.add(source)
            try:
                session.commit()
            except IntegrityError as exc:
                raise Conflict("this camera already has a primary source") from exc
            return source

    def _check_media_containment(self, target: str) -> None:
        """Refuse a ``file`` target that escapes the media root, at write time.

        The reader refuses it again when a session opens
        (:meth:`FileSource._resolve_path`); validating here means the operator
        gets a ``422`` on the request they made instead of a degraded camera
        twenty minutes later.
        """
        if self._media_root is None:
            raise ValidationError(
                "file sources are disabled because no media root is configured. "
                "Set VISIONOPS_MEDIA_ROOT to a directory that holds your videos.",
                [{"field": "target", "message": "media root not configured"}],
            )
        try:
            resolve_within(self._media_root, target)
        except ValueError as exc:
            raise ValidationError(
                str(exc),
                [{"field": "target", "message": "outside VISIONOPS_MEDIA_ROOT"}],
            ) from exc

    def list_sources(self, principal: Principal, camera_id: str) -> list[VideoSource]:
        with self._session_factory() as session:
            camera = self._get(session=session, principal=principal, camera_id=camera_id)
            return sorted(camera.sources, key=lambda s: (not s.is_primary, s.name))

    def primary_source(self, camera: Camera) -> VideoSource | None:
        for source in camera.sources:
            if source.is_primary:
                return source
        return camera.sources[0] if camera.sources else None


class ZoneService:
    """Zone CRUD plus the geometry validation that keeps bad data out."""

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    def create(self, principal: Principal, values: dict[str, Any]) -> Zone:
        camera_id = values.get("camera_id")
        if not camera_id:
            raise ValidationError(
                "camera_id is required", [{"field": "camera_id", "message": "required"}]
            )
        name = _require_name(values.get("name"), "zone")
        kind = str(values.get("kind", "polygon"))
        points = _validate_points(values.get("points"), kind)

        with self._session_factory() as session:
            camera = get_by_id(session, principal, Camera, camera_id)
            if camera is None or camera.deleted_at is not None:
                raise NotFound("camera", camera_id)
            if session.execute(
                select(Zone).where(Zone.camera_id == camera_id, Zone.name == name)
            ).scalar_one_or_none():
                raise Conflict(f"a zone named {name!r} already exists on this camera")
            zone = Zone(
                id=new_id(),
                organization_id=principal.organization_id,
                camera_id=camera_id,
                name=name,
                description=values.get("description"),
                kind=kind,
                points=points,
                color=_validate_color(values.get("color")),
                severity=_validate_severity(values.get("severity")),
                class_filter=_validate_classes(values.get("class_filter")),
                cooldown_seconds=_bounded(
                    values.get("cooldown_seconds"), 10.0, "cooldown_seconds", 0.0, 86400.0
                ),
                min_dwell_seconds=_bounded(
                    values.get("min_dwell_seconds"), 0.5, "min_dwell_seconds", 0.0, 3600.0
                ),
                enabled=bool(values.get("enabled", True)),
            )
            session.add(zone)
            try:
                session.commit()
            except IntegrityError as exc:
                raise Conflict(f"a zone named {name!r} already exists on this camera") from exc
            return zone

    def list(self, principal: Principal, camera_id: str | None = None) -> list[Zone]:
        with self._session_factory() as session:
            statement = select(Zone).where(
                Zone.organization_id == principal.organization_id, Zone.deleted_at.is_(None)
            )
            if camera_id:
                statement = statement.where(Zone.camera_id == camera_id)
            return list(
                session.execute(statement.order_by(Zone.created_at.desc())).unique().scalars()
            )

    def get(self, principal: Principal, zone_id: str) -> Zone:
        with self._session_factory() as session:
            zone = get_by_id(session, principal, Zone, zone_id)
        if zone is None or zone.deleted_at is not None:
            raise NotFound("zone", zone_id)
        return zone

    def update(self, principal: Principal, zone_id: str, values: dict[str, Any]) -> Zone:
        with self._session_factory() as session:
            zone = get_by_id(session, principal, Zone, zone_id)
            if zone is None or zone.deleted_at is not None:
                raise NotFound("zone", zone_id)
            if values.get("name") is not None:
                zone.name = _require_name(values["name"], "zone")
            if values.get("description") is not None:
                zone.description = values["description"]
            if values.get("kind") is not None:
                zone.kind = str(values["kind"])
            if values.get("points") is not None:
                zone.points = _validate_points(values["points"], zone.kind)
            if values.get("color") is not None:
                zone.color = _validate_color(values["color"])
            if values.get("severity") is not None:
                zone.severity = _validate_severity(values["severity"])
            if values.get("class_filter") is not None:
                zone.class_filter = _validate_classes(values["class_filter"])
            if values.get("cooldown_seconds") is not None:
                zone.cooldown_seconds = _bounded(
                    values["cooldown_seconds"],
                    zone.cooldown_seconds,
                    "cooldown_seconds",
                    0.0,
                    86400.0,
                )
            if values.get("min_dwell_seconds") is not None:
                zone.min_dwell_seconds = _bounded(
                    values["min_dwell_seconds"],
                    zone.min_dwell_seconds,
                    "min_dwell_seconds",
                    0.0,
                    3600.0,
                )
            if values.get("enabled") is not None:
                zone.enabled = bool(values["enabled"])
            zone.updated_at = utcnow()
            try:
                session.commit()
            except IntegrityError as exc:
                raise Conflict(f"a zone named {zone.name!r} already exists on this camera") from exc
            return zone

    def delete(self, principal: Principal, zone_id: str) -> None:
        """Stop future events; past events keep their denormalised zone name."""
        with self._session_factory() as session:
            zone = get_by_id(session, principal, Zone, zone_id)
            if zone is None:
                raise NotFound("zone", zone_id)
            zone.deleted_at = utcnow()
            zone.enabled = False
            session.commit()

    def specs_for(self, principal: Principal, camera_id: str) -> builtins.list[ZoneSpec]:
        """Convert persisted zones into the vision engine's value type."""
        specs: list[ZoneSpec] = []
        for zone in self.list(principal, camera_id):
            points = tuple((float(x), float(y)) for x, y in zone.points)
            specs.append(
                ZoneSpec(
                    zone_id=zone.id,
                    name=zone.name,
                    kind=zone.kind,
                    points=points,
                    severity=Severity(zone.severity),
                    class_filter=frozenset(zone.class_filter or ()),
                    cooldown_seconds=zone.cooldown_seconds,
                    min_dwell_seconds=zone.min_dwell_seconds,
                    enabled=zone.enabled,
                )
            )
        return specs


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _require_name(value: Any, resource: str) -> str:
    name = str(value or "").strip()
    if not name:
        raise ValidationError(
            f"{resource} name is required", [{"field": "name", "message": "must not be blank"}]
        )
    if len(name) > 120:
        raise ValidationError(
            f"{resource} name is too long", [{"field": "name", "message": "at most 120 characters"}]
        )
    return name


def _apply(entity: Any, values: dict[str, Any], allowed: tuple[str, ...]) -> None:
    for key in allowed:
        if key in values and values[key] is not None:
            setattr(entity, key, values[key])


def _positive_number(value: Any, default: float, field: str, low: float, high: float) -> float:
    if value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            f"{field} must be a number", [{"field": field, "message": "must be a number"}]
        ) from exc
    return _bounded(number, default, field, low, high)


def _bounded(value: Any, default: float, field: str, low: float, high: float) -> float:
    if value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            f"{field} must be a number", [{"field": field, "message": "must be a number"}]
        ) from exc
    if not low <= number <= high:
        raise ValidationError(
            f"{field} must be between {low} and {high}",
            [{"field": field, "message": f"must be between {low} and {high}"}],
        )
    return number


def _validate_color(value: Any) -> str:
    color = str(value or "#f97316").strip()
    if not color.startswith("#") or len(color) != 7 or set(color[1:]) - VALID_HEX_COLORS:
        raise ValidationError(
            "color must be a hex value like #f97316",
            [{"field": "color", "message": "must be a #RRGGBB hex colour"}],
        )
    return color.lower()


def _validate_severity(value: Any) -> str:
    severity = str(value or "warning").strip().lower()
    if severity not in VALID_SEVERITIES:
        raise ValidationError(
            f"severity must be one of {sorted(VALID_SEVERITIES)}",
            [{"field": "severity", "message": f"must be one of {sorted(VALID_SEVERITIES)}"}],
        )
    return severity


def _validate_classes(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValidationError(
            "class_filter must be a list of class names",
            [{"field": "class_filter", "message": "must be a list of strings"}],
        )
    classes = [str(item).strip() for item in value if str(item).strip()]
    if len(classes) > 32:
        raise ValidationError(
            "class_filter may contain at most 32 entries",
            [{"field": "class_filter", "message": "at most 32 entries"}],
        )
    return classes


def _validate_points(value: Any, kind: str) -> list[list[float]]:
    """Validate normalised polygon coordinates.

    Rejects out-of-range values rather than clamping them: a client sending
    pixels by mistake should be told, not silently trusted.
    """
    expected = 2 if kind == "line" else 3
    field = "points"
    if not isinstance(value, list) or len(value) < expected:
        raise ValidationError(
            f"a {kind} zone needs at least {expected} point(s)",
            [{"field": field, "message": f"at least {expected} [[x, y]] points are required"}],
        )
    if len(value) > MAX_POLYGON_POINTS:
        raise ValidationError(
            f"a zone may have at most {MAX_POLYGON_POINTS} points",
            [{"field": field, "message": f"at most {MAX_POLYGON_POINTS} points"}],
        )
    points: list[list[float]] = []
    for index, item in enumerate(value):
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValidationError(
                "each point must be an [x, y] pair",
                [{"field": f"{field}[{index}]", "message": "must be [x, y]"}],
            )
        try:
            x, y = float(item[0]), float(item[1])
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                "point coordinates must be numbers",
                [{"field": f"{field}[{index}]", "message": "coordinates must be numbers"}],
            ) from exc
        if not (0.0 <= x <= 1.0) or not (0.0 <= y <= 1.0):
            raise ValidationError(
                "point coordinates must be normalised between 0 and 1",
                [
                    {
                        "field": f"{field}[{index}]",
                        "message": "must be between 0 and 1 (normalised coordinate)",
                    }
                ],
            )
        points.append([round(x, 6), round(y, 6)])
    polygon = [(x, y) for x, y in points]
    if kind == "polygon" and not is_simple_polygon(polygon):
        # Checked before the area test: a bow tie has cancelling area, so the
        # area check would report a confusing "zero area" for what is really a
        # self-intersection.
        raise ValidationError(
            "a polygon zone must not cross itself",
            [{"field": field, "message": "polygon edges intersect"}],
        )
    if kind == "polygon" and abs(polygon_area(polygon)) < 1e-6:
        raise ValidationError(
            "a polygon zone must enclose a non-zero area",
            [{"field": field, "message": "polygon has zero area"}],
        )
    return points


def _validate_settings(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValidationError(
            "settings must be an object", [{"field": "settings", "message": "must be an object"}]
        )
    allowed = {
        "input_width",
        "input_height",
        "confidence_threshold",
        "nms_iou_threshold",
        "class_filter",
    }
    unknown = set(value) - allowed
    if unknown:
        raise ValidationError(
            f"unknown camera settings: {sorted(unknown)}",
            [{"field": f"settings.{key}", "message": "unknown setting"} for key in sorted(unknown)],
        )
    clean = dict(value)
    for numeric in ("input_width", "input_height"):
        if numeric in clean:
            clean[numeric] = int(_bounded(clean[numeric], 640, f"settings.{numeric}", 64, 4096))
    for numeric, default in (("confidence_threshold", 0.35), ("nms_iou_threshold", 0.45)):
        if numeric in clean:
            clean[numeric] = _bounded(clean[numeric], default, f"settings.{numeric}", 0.0, 1.0)
    if "class_filter" in clean:
        clean["class_filter"] = _validate_classes(clean["class_filter"])
    return clean


def _validate_source_options(kind: str, options: dict[str, Any]) -> dict[str, Any]:
    allowed_by_kind = {
        "browser": {"note"},
        "device": {"note"},
        "file": {"loop", "note"},
        "rtsp": {"transport", "open_timeout_ms", "read_timeout_ms", "note", "credential_blob"},
    }
    allowed = allowed_by_kind.get(kind, {"note"})
    unknown = set(options) - allowed
    if unknown:
        raise ValidationError(
            f"unknown source options for a {kind} source: {sorted(unknown)}",
            [{"field": f"options.{key}", "message": "unknown option"} for key in sorted(unknown)],
        )
    if kind == "rtsp":
        transport = str(options.get("transport", "tcp")).lower()
        if transport not in ("tcp", "udp"):
            raise ValidationError(
                "transport must be 'tcp' or 'udp'",
                [{"field": "options.transport", "message": "must be 'tcp' or 'udp'"}],
            )
        options["transport"] = transport
        for numeric in ("open_timeout_ms", "read_timeout_ms"):
            if numeric in options:
                options[numeric] = int(
                    _bounded(options[numeric], 5000, f"options.{numeric}", 500, 120_000)
                )
    if kind == "file" and "loop" in options:
        options["loop"] = bool(options["loop"])
    return options


def decode_json(value: Any, default: Any = None) -> Any:
    """Tolerant JSON decode for CLI/environment-sourced configuration."""
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except ValueError:
        return default


def snapshot_root_for(settings: Any) -> Path:
    return Path(getattr(settings, "snapshot_dir", "./data/snapshots"))


__all__ = [
    "MAX_POLYGON_POINTS",
    "VALID_SEVERITIES",
    "CameraService",
    "ZoneService",
    "decode_json",
]
