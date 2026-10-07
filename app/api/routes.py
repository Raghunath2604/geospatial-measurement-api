"""API route handlers.

All CPU-bound routes are defined as standard synchronous functions (`def`),
allowing FastAPI to execute them in its threadpool without blocking the asyncio event loop.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile, status

from app.api.deps import get_ingest_service, get_repository, get_settings
from app.api.schemas import (
    ElevationRequest,
    ElevationResponse,
    ErrorResponse,
    FeatureItem,
    FeaturesResponse,
    FileInfo,
    GeometryMeasurementRequest,
    GeometryMeasurementResponse,
    MeasurementItem,
    MeasurementsResponse,
    MeasurementSummarySchema,
    ReverseGeocodeRequest,
    ReverseGeocodeResponse,
    SystemConfigResponse,
)
from app.config import Settings
from app.domain import FileStatus
from app.errors import ConflictError, NotFoundError
from app.services.geo_lookup import get_elevation, reverse_geocode
from app.services.ingest import IngestService
from app.services.measurement import measure_geometry_live
from app.storage.repository import Repository

router = APIRouter(prefix="/api/files", tags=["Files & Ingestion"])
measure_router = APIRouter(prefix="/api/measure", tags=["Real-Time Geometry Engine"])


@router.get(
    "/config/",
    response_model=SystemConfigResponse,
    summary="Get upload size and feature limits",
)
def get_system_limits(
    settings: Settings = Depends(get_settings),
) -> SystemConfigResponse:
    """Retrieve runtime file limits and supported extensions."""
    return SystemConfigResponse(
        max_upload_bytes=settings.max_upload_bytes,
        max_upload_mb=round(settings.max_upload_bytes / (1024 * 1024), 2),
        max_uncompressed_bytes=settings.max_uncompressed_bytes,
        max_features=settings.max_features,
        allowed_extensions=[".kml", ".kmz", ".zip"],
        version="1.0.0",
    )


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


@router.get(
    "/{file_id}/export/geojson/",
    summary="Export features with measurements as GeoJSON FeatureCollection",
)
def export_file_geojson(
    file_id: str,
    repo: Repository = Depends(get_repository),
) -> Response:
    """Export complete dataset as a standard GeoJSON FeatureCollection."""
    import json

    record = repo.get_file(file_id)
    if not record:
        raise NotFoundError(f"File '{file_id}' not found")
    if record.status != FileStatus.COMPLETED:
        raise ConflictError("Export requires COMPLETED status")

    features, _ = repo.get_features(file_id=file_id, limit=10_000, offset=0)
    _, stored_features, _ = repo.get_measurements(file_id=file_id, limit=10_000, offset=0)

    meas_by_idx = {sf.feature_index: sf for sf in stored_features}

    fc_features = []
    for f in features:
        props = dict(f.properties)
        meas = meas_by_idx.get(f.feature_index)
        if meas:
            props["_measurement_status"] = meas.measurement_status.value
            props["_area_sq_m"] = meas.area_sq_m
            props["_length_m"] = meas.length_m
            props["_measurement_crs"] = meas.measurement_crs
            if meas.measurement_message:
                props["_measurement_notice"] = meas.measurement_message

        fc_features.append(
            {
                "type": "Feature",
                "id": f.feature_index,
                "geometry": f.geometry,
                "properties": props,
            }
        )

    geojson_doc = {
        "type": "FeatureCollection",
        "name": record.filename,
        "crs": {
            "type": "name",
            "properties": {"name": record.crs or "urn:ogc:def:crs:OGC:1.3:CRS84"},
        },
        "features": fc_features,
    }

    body = json.dumps(geojson_doc, indent=2)
    filename_clean = record.filename.rsplit(".", 1)[0]
    return Response(
        content=body,
        media_type="application/geo+json",
        headers={"Content-Disposition": f'attachment; filename="{filename_clean}_measured.geojson"'},
    )


@router.get(
    "/{file_id}/export/csv/",
    summary="Export feature measurements as CSV",
)
def export_file_csv(
    file_id: str,
    repo: Repository = Depends(get_repository),
) -> Response:
    """Export complete tabular measurements as standard CSV."""
    import csv
    import io

    record = repo.get_file(file_id)
    if not record:
        raise NotFoundError(f"File '{file_id}' not found")
    if record.status != FileStatus.COMPLETED:
        raise ConflictError("Export requires COMPLETED status")

    _, stored_features, _ = repo.get_measurements(file_id=file_id, limit=10_000, offset=0)

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow([
        "feature_index",
        "geometry_type",
        "status",
        "area_sq_m",
        "length_m",
        "measurement_crs",
        "notice",
    ])

    for sf in stored_features:
        writer.writerow([
            sf.feature_index,
            sf.geometry_type,
            sf.measurement_status.value,
            sf.area_sq_m if sf.area_sq_m is not None else "",
            sf.length_m if sf.length_m is not None else "",
            sf.measurement_crs or "",
            sf.measurement_message or "",
        ])

    csv_data = out.getvalue()
    filename_clean = record.filename.rsplit(".", 1)[0]
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename_clean}_measurements.csv"'},
    )


# ---------------------------------------------------------------------------
# Real-Time Geometry Engine Endpoints
# ---------------------------------------------------------------------------


@measure_router.post(
    "/geometry/",
    response_model=GeometryMeasurementResponse,
    summary="Real-time arbitrary GeoJSON geometry measurement",
)
def measure_arbitrary_geometry(
    req: GeometryMeasurementRequest,
) -> GeometryMeasurementResponse:
    """Calculate survey-grade planar and geodesic metrics for an arbitrary geometry in real-time."""
    result = measure_geometry_live(req.geometry, source_crs=req.source_crs)
    return GeometryMeasurementResponse(**result)


@measure_router.post(
    "/reverse-geocode/",
    response_model=ReverseGeocodeResponse,
    summary="Real-time reverse geocoding to administrative location",
)
def api_reverse_geocode(
    req: ReverseGeocodeRequest,
) -> ReverseGeocodeResponse:
    """Query administrative location context (country, city, region) for a coordinate."""
    res = reverse_geocode(req.latitude, req.longitude)
    return ReverseGeocodeResponse(**res)


@measure_router.post(
    "/elevation/",
    response_model=ElevationResponse,
    summary="Real-time terrain elevation query",
)
def api_elevation(
    req: ElevationRequest,
) -> ElevationResponse:
    """Query terrain elevation in metres above sea level for a coordinate."""
    res = get_elevation(req.latitude, req.longitude)
    return ElevationResponse(**res)

