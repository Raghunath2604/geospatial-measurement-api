"""Domain and HTTP error hierarchy for GeoService."""

from __future__ import annotations

from typing import Any


class GeoServiceError(Exception):
    """Base exception for all domain and service errors in GeoService."""

    def __init__(
        self,
        detail: str,
        status_code: int = 400,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code
        self.extra = extra or {}


class UnsupportedMediaTypeError(GeoServiceError):
    """Raised when an uploaded file extension or mime type is not supported (HTTP 415)."""

    def __init__(self, detail: str, extra: dict[str, Any] | None = None) -> None:
        super().__init__(detail=detail, status_code=415, extra=extra)


class PayloadTooLargeError(GeoServiceError):
    """Raised when an uploaded file exceeds the configured byte size limit (HTTP 413)."""

    def __init__(self, detail: str, extra: dict[str, Any] | None = None) -> None:
        super().__init__(detail=detail, status_code=413, extra=extra)


class UnprocessableEntityError(GeoServiceError):
    """Raised when file content or geometry is semantically unprocessable (HTTP 422)."""

    def __init__(self, detail: str, extra: dict[str, Any] | None = None) -> None:
        super().__init__(detail=detail, status_code=422, extra=extra)


class NotFoundError(GeoServiceError):
    """Raised when an entity or resource is not found (HTTP 404)."""

    def __init__(self, detail: str, extra: dict[str, Any] | None = None) -> None:
        super().__init__(detail=detail, status_code=404, extra=extra)


class ConflictError(GeoServiceError):
    """Raised when a resource state conflicts with the requested operation (HTTP 409)."""

    def __init__(self, detail: str, extra: dict[str, Any] | None = None) -> None:
        super().__init__(detail=detail, status_code=409, extra=extra)
