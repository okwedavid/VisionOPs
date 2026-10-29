"""ORM entities for the V0.1 schema (the full design is in docs/DATABASE.md)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..core.ids import new_id
from ..db.base import Base, TimestampMixin, utcnow


class Organization(TimestampMixin, Base):
    """The tenant root. Every domain row hangs off one organisation."""

    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    retention_days: Mapped[int] = mapped_column(Integer, nullable=False, default=90)
    snapshots_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    min_dwell_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    event_cooldown_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=10.0)
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    __table_args__ = (
        CheckConstraint("retention_days BETWEEN 1 AND 3650", name="retention_days_range"),
        CheckConstraint("min_dwell_seconds >= 0", name="min_dwell_positive"),
        CheckConstraint("event_cooldown_seconds >= 0", name="cooldown_positive"),
        CheckConstraint("length(trim(name)) > 0", name="name_not_blank"),
    )


class ApiKey(TimestampMixin, Base):
    """Machine credential. The secret is stored only as a salted SHA-256 hash."""

    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    key_salt: Mapped[str] = mapped_column(String(64), nullable=False)
    permissions: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    rate_limit_rps: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    organization: Mapped[Organization] = relationship(lazy="joined")

    __table_args__ = (Index("ix_api_keys_organization_id", "organization_id"),)


class Site(TimestampMixin, Base):
    """A physical location grouping cameras."""

    __tablename__ = "sites"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    address: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_sites_organization_id_name"),
        CheckConstraint("length(trim(name)) > 0", name="name_not_blank"),
        Index("ix_sites_organization_id", "organization_id"),
    )


class Camera(TimestampMixin, Base):
    """A logical camera. Owns sources, zones and events."""

    __tablename__ = "cameras"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    site_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sites.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="offline")
    target_fps: Mapped[float] = mapped_column(Float, nullable=False, default=5.0)
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    site: Mapped[Site] = relationship(lazy="joined")
    # ``joined`` rather than ``selectin``: the API serialises a camera after its
    # session closes, and a detached lazy load would raise. A camera has a
    # handful of sources and zones, so eager loading them is also the right plan.
    sources: Mapped[list[VideoSource]] = relationship(
        back_populates="camera", cascade="all, delete-orphan", lazy="joined"
    )
    zones: Mapped[list[Zone]] = relationship(
        back_populates="camera", cascade="all, delete-orphan", lazy="joined"
    )

    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_cameras_organization_id_name"),
        CheckConstraint(
            "status IN ('online','offline','degraded','disabled')", name="status_allowed"
        ),
        CheckConstraint("target_fps > 0 AND target_fps <= 60", name="target_fps_range"),
        Index("ix_cameras_organization_id_site_id", "organization_id", "site_id"),
        Index(
            "ix_cameras_organization_id_status",
            "organization_id",
            "status",
            sqlite_where=text("deleted_at IS NULL"),
        ),
    )


class VideoSource(TimestampMixin, Base):
    """An ingest configuration. Credentials live here, encrypted."""

    __tablename__ = "video_sources"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    camera_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    #: For ``rtsp``: a URL **without** credentials. For ``file``: a path under
    #: the media root. For ``device``: the device index.
    target: Mapped[str] = mapped_column(String(2048), nullable=False)
    credential_blob: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    options: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    camera: Mapped[Camera] = relationship(back_populates="sources")

    __table_args__ = (
        CheckConstraint("kind IN ('browser','device','file','rtsp')", name="kind_allowed"),
        CheckConstraint("length(trim(name)) > 0", name="name_not_blank"),
        CheckConstraint("length(trim(target)) > 0", name="target_not_blank"),
        Index("ix_video_sources_organization_id_camera_id", "organization_id", "camera_id"),
        Index(
            "uq_video_sources_primary_per_camera",
            "camera_id",
            unique=True,
            sqlite_where=text("is_primary = 1"),
        ),
    )


class Zone(TimestampMixin, Base):
    """A spatial rule attached to one camera. Points are normalised ``[[x,y],…]``."""

    __tablename__ = "zones"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    camera_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="polygon")
    points: Mapped[list[list[float]]] = mapped_column(JSON, nullable=False, default=list)
    color: Mapped[str] = mapped_column(String(9), nullable=False, default="#f97316")
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="warning")
    class_filter: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    cooldown_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=10.0)
    min_dwell_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    camera: Mapped[Camera] = relationship(back_populates="zones")

    __table_args__ = (
        UniqueConstraint("camera_id", "name", name="uq_zones_camera_id_name"),
        CheckConstraint("kind IN ('polygon','line')", name="kind_allowed"),
        CheckConstraint("severity IN ('info','warning','critical')", name="severity_allowed"),
        CheckConstraint("length(trim(name)) > 0", name="name_not_blank"),
        CheckConstraint(
            "cooldown_seconds >= 0 AND cooldown_seconds <= 86400", name="cooldown_range"
        ),
        CheckConstraint("min_dwell_seconds >= 0 AND min_dwell_seconds <= 3600", name="dwell_range"),
        Index("ix_zones_organization_id_camera_id", "organization_id", "camera_id"),
    )


class Event(Base):
    """The durable record. Append-only, with denormalised names for history."""

    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(String(48), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    camera_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("cameras.id", ondelete="SET NULL"), nullable=True
    )
    camera_name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    zone_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("zones.id", ondelete="SET NULL"), nullable=True
    )
    zone_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    track_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, nullable=False, default=dict
    )
    snapshot_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_by: Mapped[str | None] = mapped_column(String(36), nullable=True)

    __table_args__ = (
        CheckConstraint("track_id IS NULL OR track_id > 0", name="track_id_positive"),
        CheckConstraint("schema_version >= 1", name="schema_version_positive"),
        CheckConstraint(
            "severity IN ('info','warning','critical','error')", name="severity_allowed"
        ),
        Index("ix_events_org_occurred", "organization_id", "occurred_at"),
        Index("ix_events_org_camera_occurred", "organization_id", "camera_id", "occurred_at"),
        Index("ix_events_org_zone_occurred", "organization_id", "zone_id", "occurred_at"),
        Index("ix_events_org_type_occurred", "organization_id", "type", "occurred_at"),
        Index("ix_events_org_severity_occurred", "organization_id", "severity", "occurred_at"),
        Index("ix_events_occurred_at", "occurred_at"),
    )


class AuditLog(Base):
    """Append-only security trail. There is no update or delete path."""

    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(256), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    __table_args__ = (
        CheckConstraint("actor_type IN ('api_key','user','system')", name="actor_type_allowed"),
        CheckConstraint("outcome IN ('success','denied','failure')", name="outcome_allowed"),
        CheckConstraint("length(trim(action)) > 0", name="action_not_blank"),
        Index("ix_audit_logs_org_created_at", "organization_id", "created_at"),
        Index("ix_audit_logs_org_action_created_at", "organization_id", "action", "created_at"),
        Index("ix_audit_logs_org_outcome_created_at", "organization_id", "outcome", "created_at"),
    )


__all__ = [
    "ApiKey",
    "AuditLog",
    "Base",
    "Camera",
    "Event",
    "Organization",
    "Site",
    "TimestampMixin",
    "VideoSource",
    "Zone",
]
