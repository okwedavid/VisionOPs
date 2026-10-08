"""RFC 9457 problem documents (``application/problem+json``)."""

from __future__ import annotations

import json
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..core.errors import AppError, RateLimited, Unauthorized
from ..core.logging import current_request_id

PROBLEM_CONTENT_TYPE = "application/problem+json"
PROBLEM_BASE_URI = "https://visionops.dev/problems"


def problem_response(
    *,
    status: int,
    code: str,
    title: str,
    detail: str,
    instance: str,
    errors: list[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": f"{PROBLEM_BASE_URI}/{code}",
        "title": title,
        "status": status,
        "detail": detail,
        "instance": instance,
        "code": code,
        "request_id": current_request_id(),
    }
    if errors:
        body["errors"] = errors
    return JSONResponse(
        status_code=status,
        content=body,
        headers={"Content-Type": PROBLEM_CONTENT_TYPE, **(headers or {})},
    )


def from_app_error(error: AppError, instance: str) -> JSONResponse:
    return problem_response(
        status=error.status_code,
        code=error.code,
        title=error.title,
        detail=error.detail,
        instance=instance,
        errors=error.errors or None,
        headers=error.headers,
    )


def from_validation_error(exc: RequestValidationError, instance: str) -> JSONResponse:
    """Flatten FastAPI validation errors into ``field``/``message`` pairs."""
    errors: list[dict[str, Any]] = []
    for item in exc.errors():
        location = [
            str(part) for part in item.get("loc", []) if part not in ("body", "query", "path")
        ]
        errors.append(
            {
                "field": ".".join(location) or "body",
                "message": str(item.get("msg", "invalid value")),
                "type": str(item.get("type", "value_error")),
            }
        )
    return problem_response(
        status=422,
        code="validation_error",
        title="Validation error",
        detail="The request failed validation.",
        instance=instance,
        errors=errors,
    )


def from_http_exception(exc: StarletteHTTPException, instance: str) -> JSONResponse:
    mapping = {
        400: ("bad_request", "Bad request"),
        401: ("unauthorized", "Unauthorized"),
        403: ("forbidden", "Forbidden"),
        404: ("not_found", "Not found"),
        405: ("method_not_allowed", "Method not allowed"),
        409: ("conflict", "Conflict"),
        413: ("payload_too_large", "Payload too large"),
        415: ("unsupported_media_type", "Unsupported media type"),
        422: ("validation_error", "Validation error"),
        429: ("rate_limited", "Rate limited"),
    }
    code, title = mapping.get(exc.status_code, ("internal_error", "Error"))
    detail = exc.detail if isinstance(exc.detail, str) else title
    return problem_response(
        status=exc.status_code,
        code=code,
        title=title,
        detail=detail,
        instance=instance,
        headers=dict(exc.headers or {}) or None,
    )


def from_unexpected(exc: Exception, instance: str) -> JSONResponse:
    """A 500 that leaks nothing: the operator gets a request id, not internals."""
    return problem_response(
        status=500,
        code="internal_error",
        title="Internal error",
        detail=(
            f"An unexpected error occurred. "
            f"Quote request id {current_request_id()} when reporting it."
        ),
        instance=instance,
    )


def install_exception_handlers(app: Any) -> None:
    """Register the handlers that guarantee every error is a problem document."""
    from fastapi.exceptions import RequestValidationError

    @app.exception_handler(AppError)
    async def _app_error(_request: Request, exc: AppError) -> JSONResponse:
        return from_app_error(exc, _instance(_request))

    @app.exception_handler(RequestValidationError)
    async def _validation(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return from_validation_error(exc, _instance(_request))

    @app.exception_handler(StarletteHTTPException)
    async def _http(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return from_http_exception(exc, _instance(_request))

    @app.exception_handler(Exception)
    async def _unexpected(_request: Request, exc: Exception) -> JSONResponse:
        import logging

        logging.getLogger("visionops.api").exception("unhandled error", exc_info=exc)
        return from_unexpected(exc, _instance(_request))


def _instance(request: Request) -> str:
    return request.url.path


__all__ = [
    "PROBLEM_CONTENT_TYPE",
    "RateLimited",
    "Unauthorized",
    "from_app_error",
    "from_http_exception",
    "from_unexpected",
    "from_validation_error",
    "install_exception_handlers",
    "problem_response",
    "problem_text",
]


def problem_text(body: dict[str, Any]) -> str:
    """Serialise a problem body (used by tests and the CLI)."""
    return json.dumps(body, default=str)
