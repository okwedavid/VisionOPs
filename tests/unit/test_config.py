"""Settings parsing for the list-valued env vars (CORS allow-list, proxies)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from visionops.config import Settings


def _settings(monkeypatch: pytest.MonkeyPatch, value: str | None) -> Settings:
    monkeypatch.delenv("VISIONOPS_CORS_ORIGINS", raising=False)
    monkeypatch.delenv("VISIONOPS_TRUSTED_PROXIES", raising=False)
    if value is not None:
        monkeypatch.setenv("VISIONOPS_CORS_ORIGINS", value)
    return Settings(_env_file=None)


def test_cors_origins_default_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _settings(monkeypatch, None).cors_origins == []


def test_cors_origins_empty_value_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    # The same-origin default, and what .env.example ships: this used to raise
    # SettingsError because pydantic-settings JSON-decoded the empty string.
    assert _settings(monkeypatch, "").cors_origins == []


def test_cors_origins_json_empty_array(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _settings(monkeypatch, "[]").cors_origins == []


def test_cors_origins_valid_json_array(monkeypatch: pytest.MonkeyPatch) -> None:
    value = '["https://app.example", "https://admin.example"]'
    assert _settings(monkeypatch, value).cors_origins == [
        "https://app.example",
        "https://admin.example",
    ]


def test_cors_origins_comma_separated(monkeypatch: pytest.MonkeyPatch) -> None:
    value = "https://a.example, https://b.example"
    assert _settings(monkeypatch, value).cors_origins == [
        "https://a.example",
        "https://b.example",
    ]


@pytest.mark.parametrize(
    "value",
    [
        "[not json",
        '{"https://app.example": true}',
        "[] trailing",
        "[1, 2]",
    ],
)
def test_cors_origins_malformed_is_rejected(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    with pytest.raises(ValidationError):
        _settings(monkeypatch, value)


def test_empty_lists_in_dotenv_are_accepted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # .env.example sets both to an empty value; `make env` copies it verbatim.
    monkeypatch.delenv("VISIONOPS_CORS_ORIGINS", raising=False)
    monkeypatch.delenv("VISIONOPS_TRUSTED_PROXIES", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("VISIONOPS_CORS_ORIGINS=\nVISIONOPS_TRUSTED_PROXIES=\n", encoding="utf-8")
    settings = Settings(_env_file=env_file)
    assert settings.cors_origins == []
    assert settings.trusted_proxies == []


def test_trusted_proxies_accepts_empty_and_csv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VISIONOPS_TRUSTED_PROXIES", raising=False)
    monkeypatch.setenv("VISIONOPS_TRUSTED_PROXIES", "")
    assert Settings(_env_file=None).trusted_proxies == []

    monkeypatch.setenv("VISIONOPS_TRUSTED_PROXIES", "10.0.0.0/8, 192.168.0.0/16")
    assert Settings(_env_file=None).trusted_proxies == ["10.0.0.0/8", "192.168.0.0/16"]
