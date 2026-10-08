"""Structured logging with mandatory secret redaction (ADR-016).

One JSON object per line in production, a compact line locally. A redacting
filter strips anything that looks like a credential, because the single most
damaging thing a log can do in this product is leak a camera password.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

#: Correlation ids, propagated implicitly through the async task tree.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
session_id_var: ContextVar[str | None] = ContextVar("session_id", default=None)

#: Field names whose values are never emitted.
REDACTED_FIELDS: frozenset[str] = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "auth",
        "credential",
        "credential_blob",
        "credentials",
        "key",
        "key_secret",
        "master_key",
        "passwd",
        "password",
        "secret",
        "token",
        "private_key",
        "cookie",
        "set-cookie",
    }
)

REDACTION = "***"

#: Extra keys promoted to the top level of every record by ``log_event``.
_context_fields = ("request_id", "session_id")


class RedactingFilter(logging.Filter):
    """Strips secret-looking values from ``record.__dict__`` before formatting."""

    def filter(self, record: logging.LogRecord) -> bool:
        for field in REDACTED_FIELDS:
            if field in record.__dict__:
                record.__dict__[field] = REDACTION
        # Arguments can carry secrets too; scrub string args of known key names.
        if record.args:
            record.args = _scrub_args(record.args)
        return True


def _scrub_args(args: Any) -> Any:
    if isinstance(args, dict):
        return {k: (REDACTION if k.lower() in REDACTED_FIELDS else v) for k, v in args.items()}
    if isinstance(args, tuple):
        return tuple(_scrub_args(a) for a in args)
    return args


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with correlation ids always present."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S") + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = request_id_var.get()
        if request_id:
            payload["request_id"] = request_id
        session_id = session_id_var.get()
        if session_id:
            payload["session_id"] = session_id
        for key, value in record.__dict__.items():
            if key in _STANDARD_ATTRS or key in _context_fields:
                continue
            if key in REDACTED_FIELDS:
                payload[key] = REDACTION
                continue
            payload[key] = _safe(value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        try:
            return json.dumps(payload, default=str, separators=(",", ":"))
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return json.dumps({"ts": payload["ts"], "level": "ERROR", "message": str(record.msg)})


class ConsoleFormatter(logging.Formatter):
    """Compact, readable local output with a dimmed prefix."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-5s %(name)-34s %(message)s", "%H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {
            k: v
            for k, v in record.__dict__.items()
            if k not in _STANDARD_ATTRS and k not in _context_fields and not k.startswith("_")
        }
        if extras:
            base += "  " + " ".join(f"{k}={v}" for k, v in sorted(extras.items()))
        return base


_STANDARD_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
)


def _safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    if isinstance(value, dict):
        return {
            k: (REDACTION if str(k).lower() in REDACTED_FIELDS else _safe(v))
            for k, v in value.items()
        }
    return str(value)


def configure_logging(
    *,
    level: str = "INFO",
    fmt: str = "console",
    log_file: Path | None = None,
) -> None:
    """Install the VisionOps logging configuration on the root logger."""
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter: logging.Formatter = JsonFormatter() if fmt == "json" else ConsoleFormatter()
    redactor = RedactingFilter()

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    stream.addFilter(redactor)
    root.addHandler(stream)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        rotating = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        rotating.setFormatter(JsonFormatter())
        rotating.addFilter(redactor)
        root.addHandler(rotating)

    # Uvicorn's own access log duplicates ours and is noisy; keep its errors.
    for noisy in ("uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(noisy)
        logger.handlers = []
        logger.propagate = True
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    # Third-party libraries that are chatty or would duplicate our output.
    for noisy in ("onnxruntime", "alembic.runtime.migration", "httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


@contextmanager
def log_context(**values: Any) -> Iterator[None]:
    """Bind correlation ids for the duration of a block."""
    tokens = []
    if (request_id := values.get("request_id")) is not None:
        tokens.append((request_id_var, request_id_var.set(str(request_id))))
    if (session_id := values.get("session_id")) is not None:
        tokens.append((session_id_var, session_id_var.set(str(session_id))))
    try:
        yield
    finally:
        for var, token in tokens:
            var.reset(token)


def current_request_id() -> str:
    return request_id_var.get() or "-"


def log_event(logger: logging.Logger, level: int, message: str, **fields: Any) -> None:
    """Emit a log line with structured fields (redacted like any other)."""
    if not logger.isEnabledFor(level):
        return
    safe = {k: v for k, v in fields.items() if v is not None}
    logger.log(level, message, extra=safe, stacklevel=2)


__all__ = [
    "REDACTED_FIELDS",
    "REDACTION",
    "ConsoleFormatter",
    "JsonFormatter",
    "RedactingFilter",
    "configure_logging",
    "current_request_id",
    "get_logger",
    "log_context",
    "log_event",
    "request_id_var",
    "session_id_var",
]
