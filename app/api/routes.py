"""API route handlers.

All CPU-bound routes are defined as standard synchronous functions (`def`),
allowing FastAPI to execute them in its threadpool without blocking the asyncio event loop.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Query, UploadFile, status

from app.api.deps import get_ingest_service, get_repository
from app.api.schemas import (
    ErrorResponse,
    FeatureItem,
    FeaturesResponse,
    FileInfo,
    MeasurementItem,
    MeasurementsResponse,
    MeasurementSummarySchema,
)
from app.domain import FileStatus
from app.errors import ConflictError, NotFoundError
from app.services.ingest import IngestService
from app.storage.repository import Repository

router = APIRouter(prefix="/api/files", tags=["Files & Measurements"])


@router.post(
    "/",
    status_code=status.HTTP_201_CREATED,
    response_model=FileInfo,
    responses={
        413: {"model": ErrorResponse, "description": "File too large"},
        415: {"model": ErrorResponse, "description": "Unsupported media type"},
        422: {
            "model": ErrorResponse,
            "description": "Unprocessable geospatial content",
        },
    },
    summary="Upload and process geospatial file",
)
def upload_file(
    file: UploadFile = File(..., description="Geospatial .kml or Shapefile .zip"),
    ingest_service: IngestService = Depends(get_ingest_service),
) -> FileInfo:
    """Upload and process a geospatial file (.kml or Shapefile .zip).

    Streamed and validated synchronously in the threadpool.
    """
    record = ingest_service.ingest_stream(file.file, file.filename)
    return FileInfo.from_domain(record)


@router.get(
    "/{file_id}/",
    response_model=FileInfo,
    responses={
        404: {"model": ErrorResponse, "description": "File not found"},
    },
    summary="Get file status and metadata",
)
def get_file(
    file_id: str,
    repo: Repository = Depends(get_repository),
) -> FileInfo:
    """Retrieve metadata and ingestion status for a given file ID."""
    record = repo.get_file(file_id)
    if not record:
        raise NotFoundError(f"File '{file_id}' not found")
    return FileInfo.from_domain(record)


@router.get(
    "/{file_id}/measurements/",
    response_model=MeasurementsResponse,
    responses={
        404: {"model": ErrorResponse, "description": "File not found"},
        409: {"model": ErrorResponse, "description": "File not in COMPLETED status"},
    },
    summary="Get paginated measurements and whole-file summary",
)
def get_file_measurements(
    file_id: str,
    limit: int = Query(100, ge=1, le=1000, description="Page limit"),
    offset: int = Query(0, ge=0, description="Page offset"),
    repo: Repository = Depends(get_repository),
) -> MeasurementsResponse:
    """Retrieve paginated feature measurements along with a whole-file summary."""
    record = repo.get_file(file_id)
    if not record:
        raise NotFoundError(f"File '{file_id}' not found")

    if record.status != FileStatus.COMPLETED:
        raise ConflictError(
            f"File '{file_id}' is in status '{record.status.value}'. "
            "Measurements are only accessible for COMPLETED files."
        )

    summary, stored_features, total = repo.get_measurements(
        file_id=file_id, limit=limit, offset=offset
    )

    items = [
        MeasurementItem(
            feature_index=sf.feature_index,
            geometry_type=sf.geometry_type,
            status=sf.measurement_status,
            area_sq_m=sf.area_sq_m,
            length_m=sf.length_m,
            measurement_crs=sf.measurement_crs,
            message=sf.measurement_message,
        )
        for sf in stored_features
    ]

    summary_schema = MeasurementSummarySchema(
        feature_count=summary.feature_count,
        measured_count=summary.measured_count,
        not_applicable_count=summary.not_applicable_count,
        unsupported_count=summary.unsupported_count,
        error_count=summary.error_count,
        total_area_sq_m=summary.total_area_sq_m,
        total_length_m=summary.total_length_m,
    )

    return MeasurementsResponse(
        file_id=file_id,
        source_crs=record.crs,
        units={"area": "sq_m", "length": "m"},
        summary=summary_schema,
        total=total,
        limit=limit,
        offset=offset,
        measurements=items,
    )


@router.get(
    "/{file_id}/features/",
    response_model=FeaturesResponse,
    responses={
        404: {"model": ErrorResponse, "description": "File not found"},
        409: {"model": ErrorResponse, "description": "File not in COMPLETED status"},
    },
    summary="Get paginated features with GeoJSON geometries",
)
def get_file_features(
    file_id: str,
    limit: int = Query(100, ge=1, le=1000, description="Page limit"),
    offset: int = Query(0, ge=0, description="Page offset"),
    repo: Repository = Depends(get_repository),
) -> FeaturesResponse:
    """Retrieve paginated GeoJSON features in their native source CRS."""
    record = repo.get_file(file_id)
    if not record:
        raise NotFoundError(f"File '{file_id}' not found")

    if record.status != FileStatus.COMPLETED:
        raise ConflictError(
            f"File '{file_id}' is in status '{record.status.value}'. "
            "Features are only accessible for COMPLETED files."
        )

    features, total = repo.get_features(file_id=file_id, limit=limit, offset=offset)

    return FeaturesResponse(
        file_id=file_id,
        total=total,
        limit=limit,
        offset=offset,
        features=[FeatureItem.from_domain(f) for f in features],
    )
