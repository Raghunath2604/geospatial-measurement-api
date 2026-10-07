"""Application factory and entrypoint."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.api.schemas import FileInfo
from app.config import Settings
from app.domain import FileRecord
from app.errors import GeoServiceError
from app.services.ingest import IngestService
from app.storage.repository import Repository


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create and configure a FastAPI application instance."""
    app_settings = settings or Settings.from_env()

    app = FastAPI(
        title="Geospatial File Measurement API",
        description="Ingests KML and Shapefile ZIP archives and computes metric measurements.",
        version="1.0.0",
    )

    # State initialization
    repository = Repository(app_settings.db_path)
    ingest_service = IngestService(repository=repository, settings=app_settings)

    app.state.settings = app_settings
    app.state.repository = repository
    app.state.ingest_service = ingest_service

    # Exception handler for GeoServiceError hierarchy
    @app.exception_handler(GeoServiceError)
    def geo_service_error_handler(
        request: Request, exc: GeoServiceError
    ) -> JSONResponse:
        content: dict[str, object] = {"detail": exc.detail}
        file_obj = exc.extra.get("file")
        if file_obj is not None and isinstance(file_obj, FileRecord):
            content["file"] = FileInfo.from_domain(file_obj).model_dump()
        return JSONResponse(status_code=exc.status_code, content=content)

    # Health check endpoint
    @app.get(
        "/health",
        tags=["System"],
        summary="Service health check",
    )
    def health_check() -> dict[str, str]:
        return {"status": "ok", "service": "geo-measure-api"}

    # Include routes
    app.include_router(router)

    return app


# Default ASGI application instance
app = create_app()
