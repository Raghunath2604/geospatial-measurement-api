"""Application factory and entrypoint with production hardening and observability."""

from __future__ import annotations

import logging
import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.api.schemas import FileInfo
from app.config import Settings
from app.domain import FileRecord
from app.errors import GeoServiceError
from app.services.ingest import IngestService
from app.storage.repository import Repository

logger = logging.getLogger("geomeasure")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create and configure a production-ready FastAPI application instance."""
    app_settings = settings or Settings.from_env()

    # Configure structured logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )

    app = FastAPI(
        title="Geospatial File Measurement API",
        description=(
            "High-performance geospatial ingestion and measurement engine. "
            "Parses KML and Shapefile ZIP archives, reprojects features to local "
            "metric UTM/UPS coordinate reference systems, and calculates survey-grade metrics."
        ),
        version="1.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # Enable CORS for web GIS clients
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # State initialization
    repository = Repository(app_settings.db_path)
    ingest_service = IngestService(repository=repository, settings=app_settings)

    app.state.settings = app_settings
    app.state.repository = repository
    app.state.ingest_service = ingest_service

    # HTTP timing middleware for observability
    @app.middleware("http")
    async def add_process_time_header(request: Request, call_next):  # type: ignore[no-untyped-def]
        start_time = time.perf_counter()
        response = await call_next(request)
        process_time = time.perf_counter() - start_time
        response.headers["X-Process-Time"] = f"{process_time:.4f}s"
        return response

    # Exception handler for domain GeoServiceError hierarchy
    @app.exception_handler(GeoServiceError)
    def geo_service_error_handler(
        request: Request, exc: GeoServiceError
    ) -> JSONResponse:
        content: dict[str, object] = {"detail": exc.detail}
        file_obj = exc.extra.get("file")
        if file_obj is not None and isinstance(file_obj, FileRecord):
            content["file"] = FileInfo.from_domain(file_obj).model_dump()
        return JSONResponse(status_code=exc.status_code, content=content)

    # Global unhandled exception handler: never leak tracebacks to clients
    @app.exception_handler(Exception)
    def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "Unhandled server error processing %s: %s", request.url.path, exc
        )
        return JSONResponse(
            status_code=500,
            content={
                "detail": "An internal server error occurred while processing the request."
            },
        )

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
