"""Typed application settings, loaded from the environment.

Every setting has a working default for local development, except
``master_key`` which is only required when storing camera credentials. Invalid
configuration fails fast at startup with an actionable message rather than
failing later in a confusing place.
"""

from __future__ import annotations

import base64
import contextlib
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All environment configuration in one validated object."""

    model_config = SettingsConfigDict(
        env_prefix="VISIONOPS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # -- core ---------------------------------------------------------------
    env: Literal["local", "development", "staging", "production"] = "local"
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    version: str = "0.1.0"
    debug: bool = False

    # -- logging ------------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["console", "json"] = "console"
    log_file: Path | None = None

    # -- security -----------------------------------------------------------
    api_key: str | None = None
    master_key: str | None = None
    trusted_proxies: list[str] = Field(default_factory=list)
    cors_origins: list[str] = Field(default_factory=list)
    rate_limit_rps: float = Field(default=10.0, gt=0)
    auth_failure_limit: int = Field(default=10, gt=0)
    max_body_bytes: int = Field(default=1_048_576, gt=0)
    max_sessions: int = Field(default=4, gt=0)
    ws_max_frame_bytes: int = Field(default=2_097_152, gt=0)
    ws_send_queue_size: int = Field(default=2, gt=0)

    # -- data ---------------------------------------------------------------
    database_url: str = "sqlite:///./data/visionops.db"
    auto_migrate: bool = True
    data_dir: Path = Path("./data")
    snapshot_dir: Path = Path("./data/snapshots")
    media_root: Path = Path("./data/media")
    retention_days: int = Field(default=90, ge=1, le=3650)
    audit_retention_days: int = Field(default=365, ge=1, le=3650)

    # -- vision -------------------------------------------------------------
    model_path: Path = Path("./models/yolo11n.onnx")
    model_url: str = "https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n.onnx"
    model_sha256: str | None = None
    model_auto_download: bool = True
    ort_provider: str = "CPUExecutionProvider"
    ort_threads: int = Field(default=0, ge=0)
    input_width: int = Field(default=640, ge=64, le=4096)
    input_height: int = Field(default=640, ge=64, le=4096)
    confidence_threshold: float = Field(default=0.35, ge=0.0, le=1.0)
    nms_iou_threshold: float = Field(default=0.45, ge=0.0, le=1.0)
    class_filter: str = "person"
    target_fps: float = Field(default=5.0, gt=0, le=60)
    publish_fps: float = Field(default=10.0, gt=0, le=60)
    tracker: str = "kalman-iou"
    tracker_max_age: int = Field(default=30, ge=1, le=1000)
    tracker_min_hits: int = Field(default=3, ge=1, le=50)
    tracker_match_iou: float = Field(default=0.3, ge=0.0, le=1.0)
    preview_fps: float = Field(default=5.0, gt=0, le=30)
    preview_width: int = Field(default=640, ge=160, le=4096)
    inter_area: bool = False

    # -- zones and sources --------------------------------------------------
    zone_cooldown_seconds: float = Field(default=10.0, ge=0, le=86_400)
    zone_min_dwell_seconds: float = Field(default=0.5, ge=0, le=3600)
    source_open_timeout_ms: int = Field(default=5000, gt=0, le=120_000)
    source_max_retries: int = Field(default=5, ge=0, le=100)
    source_backoff_seconds: float = Field(default=2.0, gt=0, le=60)

    # -- validators ---------------------------------------------------------

    @field_validator("trusted_proxies", "cors_origins", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("class_filter")
    @classmethod
    def _normalise_classes(cls, value: str) -> str:
        return ",".join(item.strip() for item in value.split(",") if item.strip())

    @model_validator(mode="after")
    def _check_consistency(self) -> Settings:
        if self.publish_fps < 1:
            raise ValueError("VISIONOPS_PUBLISH_FPS must be at least 1")
        if self.max_sessions < 1:
            raise ValueError("VISIONOPS_MAX_SESSIONS must be at least 1")
        return self

    # -- derived ------------------------------------------------------------

    @property
    def class_set(self) -> frozenset[str]:
        return frozenset(item for item in self.class_filter.split(",") if item)

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def decoded_master_key(self) -> bytes | None:
        """Decode the base64 master key, or ``None`` when unset/invalid."""
        if not self.master_key:
            return None
        try:
            raw = base64.urlsafe_b64decode(self.master_key.encode())
        except (ValueError, TypeError):
            return None
        return raw if len(raw) >= 32 else None

    def ensure_directories(self) -> None:
        """Create runtime directories with restrictive permissions."""
        for path, mode in (
            (self.data_dir, 0o750),
            (self.snapshot_dir, 0o700),
            (self.media_root, 0o750),
        ):
            path.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):  # pragma: no cover - platform dependent
                path.chmod(mode)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()


def reset_settings_cache() -> None:
    """Clear the cached settings (used by tests that mutate the environment)."""
    get_settings.cache_clear()


__all__ = ["Settings", "get_settings", "reset_settings_cache"]
