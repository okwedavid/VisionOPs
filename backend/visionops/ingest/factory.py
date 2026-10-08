"""Build a :class:`VideoSource` from configuration.

Credentials are decrypted here and never travel further than the source object.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..core.errors import ValidationError
from ..core.security import DecryptionError, SecretBox
from ..vision.contracts import SourceKind
from .base import SourceOpenError, VideoSource
from .browser import BrowserFrameSource
from .device import DeviceSource, RtspSource
from .file import FileSource

logger = logging.getLogger(__name__)


def decode_credentials(
    blob: str | None, secret_box: SecretBox | None, *, aad: str
) -> tuple[str, str]:
    """Decrypt a stored credential envelope into ``(username, password)``."""
    if not blob:
        return "", ""
    if secret_box is None:
        raise ValidationError(
            "this source has stored credentials but VISIONOPS_MASTER_KEY is not configured; "
            "re-enter the credentials or set the master key"
        )
    try:
        import json

        payload = json.loads(secret_box.decrypt(blob, aad=aad))
    except DecryptionError as exc:
        raise ValidationError(
            f"stored credentials could not be decrypted ({exc}); re-enter them for this source"
        ) from exc
    except ValueError as exc:
        raise ValidationError("stored credentials are malformed; re-enter them") from exc
    return str(payload.get("username", "")), str(payload.get("password", ""))


def encode_credentials(username: str, password: str, secret_box: SecretBox, *, aad: str) -> str:
    import json

    return secret_box.encrypt(json.dumps({"username": username, "password": password}), aad=aad)


def build_source(
    *,
    kind: str,
    target: str,
    source_id: str,
    options: dict[str, Any] | None = None,
    secret_box: SecretBox | None = None,
    media_root: Path | None = None,
    open_timeout_ms: int = 5000,
    session_id: str = "",
    target_fps: float = 5.0,
) -> VideoSource:
    """Construct the source implementation for ``kind``.

    ``target`` must already be free of credentials for remote kinds; the caller
    decrypts credentials and this function reassembles the URL.
    """
    options = options or {}
    source_kind = _coerce_kind(kind)

    if source_kind is SourceKind.BROWSER:
        return BrowserFrameSource(
            session_id=session_id or source_id, target_fps=target_fps, name=target or "browser"
        )

    if source_kind is SourceKind.DEVICE:
        try:
            index = int(str(target).strip())
        except ValueError as exc:
            raise ValidationError(
                f"device source target must be an integer index, got {target!r}"
            ) from exc
        if index < 0:
            raise ValidationError("device source index must be zero or greater")
        return DeviceSource(index, open_timeout_ms=open_timeout_ms, name=source_id)

    if source_kind is SourceKind.FILE:
        return FileSource(
            Path(target),
            loop=bool(options.get("loop", False)),
            media_root=media_root,
            name=source_id,
        )

    username, password = decode_credentials(
        options.get("credential_blob"), secret_box, aad=source_id
    )
    return RtspSource(
        target,
        username=username,
        password=password,
        transport=str(options.get("transport", "tcp")),
        open_timeout_ms=int(options.get("open_timeout_ms", open_timeout_ms)),
        read_timeout_ms=int(options.get("read_timeout_ms", open_timeout_ms)),
        name=source_id,
    )


def _coerce_kind(kind: str) -> SourceKind:
    try:
        return SourceKind(kind)
    except ValueError as exc:
        allowed = ", ".join(k.value for k in SourceKind)
        raise ValidationError(f"unsupported source kind {kind!r}; allowed: {allowed}") from exc


__all__ = ["SourceOpenError", "build_source", "decode_credentials", "encode_credentials"]
