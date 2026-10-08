"""0001 — initial V0.1 schema.

Creates only the eight tables V0.1 actually needs (see docs/DATABASE.md §3).
Tables designed for later phases (detections, tracks, rules, alerts, models,
webhooks, …) are added by their own migrations, so no speculative columns or
tables exist here.

Every statement is dialect-portable: ``CHECK`` constraints instead of native
enums, ``JSON`` instead of ``JSONB``, and partial indexes only where SQLite and
PostgreSQL agree on the syntax.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("slug", sa.String(80), nullable=False, unique=True),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="UTC"),
        sa.Column("retention_days", sa.Integer(), nullable=False, server_default="90"),
        sa.Column("snapshots_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("min_dwell_seconds", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("event_cooldown_seconds", sa.Float(), nullable=False, server_default="10.0"),
        sa.Column("settings", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "retention_days BETWEEN 1 AND 3650", name="ck_organizations_retention_days_range"
        ),
        sa.CheckConstraint("min_dwell_seconds >= 0", name="ck_organizations_min_dwell_positive"),
        sa.CheckConstraint(
            "event_cooldown_seconds >= 0", name="ck_organizations_cooldown_positive"
        ),
        sa.CheckConstraint("length(trim(name)) > 0", name="ck_organizations_name_not_blank"),
    )

    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "organization_id",
            sa.String(36),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("key_prefix", sa.String(32), nullable=False, unique=True),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("key_salt", sa.String(64), nullable=False),
        sa.Column("permissions", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("rate_limit_rps", sa.Float(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_api_keys_organization_id", "api_keys", ["organization_id"])

    op.create_table(
        "sites",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "organization_id",
            sa.String(36),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=True),
        sa.Column("address", sa.String(255), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("organization_id", "name", name="uq_sites_organization_id_name"),
        sa.CheckConstraint("length(trim(name)) > 0", name="ck_sites_name_not_blank"),
    )
    op.create_index("ix_sites_organization_id", "sites", ["organization_id"])

    op.create_table(
        "cameras",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "organization_id",
            sa.String(36),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "site_id", sa.String(36), sa.ForeignKey("sites.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="offline"),
        sa.Column("target_fps", sa.Float(), nullable=False, server_default="5.0"),
        sa.Column("settings", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("organization_id", "name", name="uq_cameras_organization_id_name"),
        sa.CheckConstraint(
            "status IN ('online','offline','degraded','disabled')", name="ck_cameras_status_allowed"
        ),
        sa.CheckConstraint(
            "target_fps > 0 AND target_fps <= 60", name="ck_cameras_target_fps_range"
        ),
    )
    op.create_index("ix_cameras_organization_id_site_id", "cameras", ["organization_id", "site_id"])
    op.create_index(
        "ix_cameras_organization_id_status",
        "cameras",
        ["organization_id", "status"],
        sqlite_where=sa.text("deleted_at IS NULL"),
    )

    op.create_table(
        "video_sources",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "organization_id",
            sa.String(36),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "camera_id",
            sa.String(36),
            sa.ForeignKey("cameras.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("target", sa.String(2048), nullable=False),
        sa.Column("credential_blob", sa.Text(), nullable=True),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("options", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('browser','device','file','rtsp')", name="ck_video_sources_kind_allowed"
        ),
        sa.CheckConstraint("length(trim(name)) > 0", name="ck_video_sources_name_not_blank"),
        sa.CheckConstraint("length(trim(target)) > 0", name="ck_video_sources_target_not_blank"),
    )
    op.create_index(
        "ix_video_sources_organization_id_camera_id",
        "video_sources",
        ["organization_id", "camera_id"],
    )
    op.create_index(
        "uq_video_sources_primary_per_camera",
        "video_sources",
        ["camera_id"],
        unique=True,
        sqlite_where=sa.text("is_primary = 1"),
    )

    op.create_table(
        "zones",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "organization_id",
            sa.String(36),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "camera_id",
            sa.String(36),
            sa.ForeignKey("cameras.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False, server_default="polygon"),
        sa.Column("points", sa.JSON(), nullable=False),
        sa.Column("color", sa.String(9), nullable=False, server_default="#f97316"),
        sa.Column("severity", sa.String(16), nullable=False, server_default="warning"),
        sa.Column("class_filter", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("cooldown_seconds", sa.Float(), nullable=False, server_default="10.0"),
        sa.Column("min_dwell_seconds", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("camera_id", "name", name="uq_zones_camera_id_name"),
        sa.CheckConstraint("kind IN ('polygon','line')", name="ck_zones_kind_allowed"),
        sa.CheckConstraint(
            "severity IN ('info','warning','critical')", name="ck_zones_severity_allowed"
        ),
        sa.CheckConstraint("length(trim(name)) > 0", name="ck_zones_name_not_blank"),
        sa.CheckConstraint(
            "cooldown_seconds >= 0 AND cooldown_seconds <= 86400", name="ck_zones_cooldown_range"
        ),
        sa.CheckConstraint(
            "min_dwell_seconds >= 0 AND min_dwell_seconds <= 3600", name="ck_zones_dwell_range"
        ),
    )
    op.create_index("ix_zones_organization_id_camera_id", "zones", ["organization_id", "camera_id"])

    op.create_table(
        "events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "organization_id",
            sa.String(36),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("type", sa.String(48), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "camera_id",
            sa.String(36),
            sa.ForeignKey("cameras.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("camera_name", sa.String(120), nullable=False, server_default=""),
        sa.Column(
            "zone_id", sa.String(36), sa.ForeignKey("zones.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("zone_name", sa.String(120), nullable=True),
        sa.Column("track_id", sa.Integer(), nullable=True),
        sa.Column("session_id", sa.String(36), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("snapshot_path", sa.String(512), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_by", sa.String(36), nullable=True),
        sa.CheckConstraint("track_id IS NULL OR track_id > 0", name="ck_events_track_id_positive"),
        sa.CheckConstraint("schema_version >= 1", name="ck_events_schema_version_positive"),
        sa.CheckConstraint(
            "severity IN ('info','warning','critical','error')", name="ck_events_severity_allowed"
        ),
    )
    op.create_index("ix_events_org_occurred", "events", ["organization_id", "occurred_at"])
    op.create_index(
        "ix_events_org_camera_occurred", "events", ["organization_id", "camera_id", "occurred_at"]
    )
    op.create_index(
        "ix_events_org_zone_occurred", "events", ["organization_id", "zone_id", "occurred_at"]
    )
    op.create_index(
        "ix_events_org_type_occurred", "events", ["organization_id", "type", "occurred_at"]
    )
    op.create_index(
        "ix_events_org_severity_occurred", "events", ["organization_id", "severity", "occurred_at"]
    )
    op.create_index("ix_events_occurred_at", "events", ["occurred_at"])

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "organization_id",
            sa.String(36),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("actor_type", sa.String(16), nullable=False),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target_type", sa.String(32), nullable=True),
        sa.Column("target_id", sa.String(64), nullable=True),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(256), nullable=True),
        sa.Column("request_id", sa.String(64), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "actor_type IN ('api_key','user','system')", name="ck_audit_logs_actor_type_allowed"
        ),
        sa.CheckConstraint(
            "outcome IN ('success','denied','failure')", name="ck_audit_logs_outcome_allowed"
        ),
        sa.CheckConstraint("length(trim(action)) > 0", name="ck_audit_logs_action_not_blank"),
    )
    op.create_index("ix_audit_logs_org_created_at", "audit_logs", ["organization_id", "created_at"])
    op.create_index(
        "ix_audit_logs_org_action_created_at",
        "audit_logs",
        ["organization_id", "action", "created_at"],
    )
    op.create_index(
        "ix_audit_logs_org_outcome_created_at",
        "audit_logs",
        ["organization_id", "outcome", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("audit_logs")
    op.drop_table("events")
    op.drop_table("zones")
    op.drop_table("video_sources")
    op.drop_table("cameras")
    op.drop_table("sites")
    op.drop_table("api_keys")
    op.drop_table("organizations")
