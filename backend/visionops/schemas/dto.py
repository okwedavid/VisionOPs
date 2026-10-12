"""Pydantic DTOs. These are the public API contract (docs/API.md).

Validation is strict and messages are actionable, because a 422 that says
"points[2][1] must be between 0 and 1" saves an integrator an hour.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
Severity = Literal["info", "warning", "critical", "error"]
ZoneSeverity = Literal["info", "warning", "critical"]
SourceKindLiteral = Literal["browser", "device", "file", "rtsp"]
ZoneKind = Literal["polygon", "line"]


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ---------------------------------------------------------------------------
# Organisation, sites, cameras
# ---------------------------------------------------------------------------


class OrganizationOut(ApiModel):
    id: str
    name: str
    slug: str
    timezone: str
    retention_days: int
    snapshots_enabled: bool
    min_dwell_seconds: float
    event_cooldown_seconds: float
    settings: dict[str, Any]
    created_at: datetime


class SiteCreate(ApiModel):
    name: str = Field(min_length=1, max_length=120)
    timezone: str | None = Field(default=None, max_length=64)
    address: str | None = Field(default=None, max_length=255)
    description: str | None = None


class SitePatch(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    timezone: str | None = Field(default=None, max_length=64)
    address: str | None = Field(default=None, max_length=255)
    description: str | None = None


class SiteOut(ApiModel):
    id: str
    name: str
    timezone: str | None
    address: str | None
    description: str | None
    created_at: datetime
    updated_at: datetime


class CameraCreate(ApiModel):
    name: str = Field(min_length=1, max_length=120)
    site_id: str = Field(min_length=1)
    description: str | None = None
    target_fps: float | None = Field(default=None, gt=0, le=60)
    settings: dict[str, Any] | None = None


class CameraPatch(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None
    site_id: str | None = None
    status: Literal["online", "offline", "degraded", "disabled"] | None = None
    target_fps: float | None = Field(default=None, gt=0, le=60)
    settings: dict[str, Any] | None = None


class CameraOut(ApiModel):
    id: str
    name: str
    description: str | None
    site_id: str
    site_name: str | None = None
    status: str
    target_fps: float
    settings: dict[str, Any]
    last_seen_at: datetime | None
    zone_count: int = 0
    source_count: int = 0
    created_at: datetime


class CredentialsIn(ApiModel):
    """Write-only. Never returned by any endpoint."""

    username: str = Field(default="", max_length=128)
    password: str = Field(default="", max_length=256)


class SourceCreate(ApiModel):
    name: str = Field(min_length=1, max_length=120)
    kind: SourceKindLiteral
    target: str = Field(default="", max_length=2048)
    is_primary: bool = False
    options: dict[str, Any] | None = None
    credentials: CredentialsIn | None = None


class SourceOut(ApiModel):
    id: str
    camera_id: str
    name: str
    kind: SourceKindLiteral
    target: str
    is_primary: bool
    options: dict[str, Any]
    credentials: dict[str, bool] = Field(default_factory=lambda: {"configured": False})
    created_at: datetime


# ---------------------------------------------------------------------------
# Zones
# ---------------------------------------------------------------------------


class ZoneCreate(ApiModel):
    camera_id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=120)
    description: str | None = None
    kind: ZoneKind = "polygon"
    points: list[list[float]] = Field(min_length=2, max_length=256)
    color: str = "#f97316"
    severity: ZoneSeverity = "warning"
    class_filter: list[str] = Field(default_factory=list)
    cooldown_seconds: float = Field(default=10.0, ge=0, le=86400)
    min_dwell_seconds: float = Field(default=0.5, ge=0, le=3600)
    enabled: bool = True

    @field_validator("color")
    @classmethod
    def _color(cls, value: str) -> str:
        if not HEX_COLOR.match(value):
            raise ValueError("color must be a hex value like #f97316")
        return value.lower()


class ZonePatch(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None
    kind: ZoneKind | None = None
    points: list[list[float]] | None = Field(default=None, min_length=2, max_length=256)
    color: str | None = None
    severity: ZoneSeverity | None = None
    class_filter: list[str] | None = None
    cooldown_seconds: float | None = Field(default=None, ge=0, le=86400)
    min_dwell_seconds: float | None = Field(default=None, ge=0, le=3600)
    enabled: bool | None = None


class ZoneOut(ApiModel):
    id: str
    camera_id: str
    name: str
    description: str | None
    kind: ZoneKind
    points: list[list[float]]
    color: str
    severity: ZoneSeverity
    class_filter: list[str]
    cooldown_seconds: float
    min_dwell_seconds: float
    enabled: bool
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


class SessionStart(ApiModel):
    source_id: str | None = None
    target_fps: float | None = Field(default=None, gt=0, le=60)
    zones: list[str] | None = None


class SessionOut(ApiModel):
    session_id: str
    camera_id: str
    camera_name: str
    state: str
    started_at: float
    uptime_seconds: float
    target_fps: float
    source_kind: str
    source_id: str
    reason_code: str | None = None
    detail: str | None = None
    processed_fps: float
    frame_age_ms: float | None = None
    active_tracks: int
    in_zone: dict[str, int] = Field(default_factory=dict)
    events_emitted: int
    events_suppressed: int
    frames_processed: int
    frames_dropped: int
    inference_ms: float | None = None
    inference_errors: int
    ai: dict[str, Any] = Field(default_factory=dict)
    source: dict[str, Any] = Field(default_factory=dict)
    ingest_ws_url: str | None = None


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


class EventOut(ApiModel):
    id: str
    schema_version: int
    type: str
    severity: Severity
    occurred_at: datetime
    received_at: datetime
    organization_id: str
    camera_id: str | None
    camera_name: str
    zone_id: str | None
    zone_name: str | None
    track_id: int | None
    session_id: str | None
    confidence: float | None
    metadata: dict[str, Any]
    snapshot_path: str | None


class EventPage(ApiModel):
    items: list[EventOut]
    next_cursor: str | None = None
    has_more: bool = False


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------


class ApiKeyCreate(ApiModel):
    name: str = Field(min_length=1, max_length=120)
    permissions: list[str] = Field(default_factory=lambda: ["*"])
    rate_limit_rps: float | None = Field(default=None, gt=0)


class ApiKeyOut(ApiModel):
    id: str
    name: str
    key_prefix: str
    permissions: list[str]
    rate_limit_rps: float | None
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked: bool
    created_at: datetime


class ApiKeyCreated(ApiModel):
    id: str
    name: str
    key: str
    key_prefix: str
    warning: str = "Store this now. It cannot be retrieved later."


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


class HealthOut(ApiModel):
    status: str
    version: str
    uptime_seconds: float


class ReadyOut(ApiModel):
    status: str
    timestamp: str
    checks: dict[str, Any]


class ProblemOut(BaseModel):
    """RFC 9457 problem document."""

    model_config = ConfigDict(extra="allow")

    type: str
    title: str
    status: int
    detail: str
    instance: str
    code: str
    request_id: str
    errors: list[dict[str, Any]] | None = None


ApiKeyHeader = Annotated[str, Field(description="API key: vo_<id>_<secret>")]


__all__ = [
    "ApiKeyCreate",
    "ApiKeyCreated",
    "ApiKeyOut",
    "CameraCreate",
    "CameraOut",
    "CameraPatch",
    "CredentialsIn",
    "EventOut",
    "EventPage",
    "HealthOut",
    "OrganizationOut",
    "ProblemOut",
    "ReadyOut",
    "SessionOut",
    "SessionStart",
    "SiteCreate",
    "SiteOut",
    "SitePatch",
    "SourceCreate",
    "SourceOut",
    "ZoneCreate",
    "ZoneOut",
    "ZonePatch",
]
