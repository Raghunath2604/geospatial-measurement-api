"""Application factory and entrypoint with production hardening and observability."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import measure_router, router
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

    # Interactive Web Dashboard
    static_dir = Path(__file__).parent / "static"
    static_html = static_dir / "index.html"

    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/", tags=["Dashboard"], include_in_schema=False)
    def index_dashboard() -> FileResponse:
        if static_html.exists():
            return FileResponse(static_html, media_type="text/html")
        return FileResponse(static_html)

    # Sample download endpoints for instant demo testing
    sample_kml = Path(__file__).parent.parent / "tests" / "sample_data" / "survey.kml"
    sample_shp = (
        Path(__file__).parent.parent / "tests" / "sample_data" / "survey_shapefile.zip"
    )

    @app.get("/sample/survey.kml", tags=["Samples"], include_in_schema=False)
    def download_sample_kml() -> FileResponse:
        return FileResponse(
            sample_kml, media_type="application/vnd.google-earth.kml+xml"
        )

    @app.get("/sample/survey_shapefile.zip", tags=["Samples"], include_in_schema=False)
    def download_sample_shapefile() -> FileResponse:
        return FileResponse(sample_shp, media_type="application/zip")

    # Include routes
    app.include_router(router)
    app.include_router(measure_router)

    return app


# Default ASGI application instance
app = create_app()
