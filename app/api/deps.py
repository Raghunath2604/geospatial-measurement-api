"""FastAPI route dependencies."""

from __future__ import annotations

from fastapi import Request

from app.config import Settings
from app.services.ingest import IngestService
from app.storage.repository import Repository


def get_settings(request: Request) -> Settings:
    """Retrieve Settings from application state."""
    return request.app.state.settings  # type: ignore[no-any-return]


def get_repository(request: Request) -> Repository:
    """Retrieve Repository from application state."""
    return request.app.state.repository  # type: ignore[no-any-return]


def get_ingest_service(request: Request) -> IngestService:
    """Retrieve IngestService from application state."""
    return request.app.state.ingest_service  # type: ignore[no-any-return]
