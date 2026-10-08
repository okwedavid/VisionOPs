"""Domain error hierarchy and its mapping to HTTP.

Services raise these; the API layer converts them into RFC 9457 problem
documents. Internal exception details never reach a client.
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class for every expected failure."""

    status_code: int = 500
    code: str = "internal_error"
    title: str = "Internal error"

    def __init__(
        self,
        detail: str,
        *,
        errors: list[dict[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.errors = errors or []
        self.headers = headers or {}

    def to_problem(self, instance: str, request_id: str) -> dict[str, Any]:
        problem: dict[str, Any] = {
            "type": f"https://visionops.dev/problems/{self.code}",
            "title": self.title,
            "status": self.status_code,
            "detail": self.detail,
            "instance": instance,
            "code": self.code,
            "request_id": request_id,
        }
        if self.errors:
            problem["errors"] = self.errors
        return problem


class BadRequest(AppError):
    status_code = 400
    code = "bad_request"
    title = "Bad request"


class Unauthorized(AppError):
    """Uniform 401: unknown, revoked and malformed keys are indistinguishable."""

    status_code = 401
    code = "unauthorized"
    title = "Unauthorized"

    def __init__(self, detail: str = "A valid API key is required.") -> None:
        super().__init__(detail, headers={"WWW-Authenticate": "Bearer"})


class Forbidden(AppError):
    status_code = 403
    code = "forbidden"
    title = "Forbidden"


class NotFound(AppError):
    """Also returned for cross-organisation references (no existence oracle)."""

    status_code = 404
    code = "not_found"
    title = "Not found"

    def __init__(self, resource: str, resource_id: str | None = None) -> None:
        detail = (
            f"{resource} not found"
            if resource_id is None
            else f"{resource} {resource_id} not found"
        )
        super().__init__(detail)


class Conflict(AppError):
    status_code = 409
    code = "conflict"
    title = "Conflict"


class ValidationError(AppError):
    status_code = 422
    code = "validation_error"
    title = "Validation error"

    def __init__(self, detail: str, errors: list[dict[str, Any]] | None = None) -> None:
        super().__init__(detail, errors=errors)


class RateLimited(AppError):
    status_code = 429
    code = "rate_limited"
    title = "Rate limited"

    def __init__(self, retry_after: int) -> None:
        super().__init__(
            "Rate limit exceeded. Retry after the indicated delay.",
            headers={"Retry-After": str(max(1, retry_after))},
        )
        self.retry_after = max(1, retry_after)


class PayloadTooLarge(AppError):
    """413 for a body that exceeded the limit *while it was being read*.

    A declared ``Content-Length`` is refused before a byte is read; a chunked
    body declares nothing, so the ceiling has to be applied as the bytes
    arrive — which is this exception, raised from the receive path.
    """

    status_code = 413
    code = "payload_too_large"
    title = "Payload too large"

    def __init__(self, max_bytes: int) -> None:
        super().__init__(f"Request body exceeds the {max_bytes} byte limit.")
        self.max_bytes = max_bytes


class CapacityExceeded(AppError):
    status_code = 503
    code = "capacity_exceeded"
    title = "Capacity exceeded"

    def __init__(self, detail: str) -> None:
        super().__init__(detail, headers={"Retry-After": "5"})


class SourceError(AppError):
    status_code = 502
    code = "source_error"
    title = "Source error"

    def __init__(self, reason_code: str, detail: str) -> None:
        super().__init__(detail)
        self.reason_code = reason_code


class ServiceUnavailable(AppError):
    status_code = 503
    code = "service_unavailable"
    title = "Service unavailable"


__all__ = [
    "AppError",
    "BadRequest",
    "CapacityExceeded",
    "Conflict",
    "Forbidden",
    "NotFound",
    "RateLimited",
    "ServiceUnavailable",
    "SourceError",
    "Unauthorized",
    "ValidationError",
]
