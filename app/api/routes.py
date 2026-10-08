"""API route handlers.

All CPU-bound routes are defined as standard synchronous functions (`def`),
allowing FastAPI to execute them in its threadpool without blocking the asyncio event loop.
Async endpoints use async def for non-blocking queue submission.
"""

from __future__ import annotations

import httpx
from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)

from app.api.auth import is_rate_limiting_available, require_api_key
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
    TaskResponse,
)
from app.config import Settings
from app.domain import FileStatus
from app.errors import ConflictError, NotFoundError
from app.monitoring.metrics import get_metrics_output, is_prometheus_available
from app.services.geo_lookup import get_elevation, reverse_geocode
from app.services.ingest import IngestService
from app.services.measurement import measure_geometry_live
from app.storage.repository import Repository
from app.workers.task_queue import task_queue

router = APIRouter(prefix="/api/files", tags=["Files & Ingestion"])
measure_router = APIRouter(prefix="/api/measure", tags=["Real-Time Geometry Engine"])
async_router = APIRouter(prefix="/api/async", tags=["Async Background Processing"])
task_router = APIRouter(prefix="/api/tasks", tags=["Task Queue"])



@router.get(
    "/config/",
    response_model=SystemConfigResponse,
    summary="Get upload size and feature limits",
)
def get_system_limits(
    settings: Settings = Depends(get_settings),
    _auth: bool = Depends(require_api_key),
) -> SystemConfigResponse:
    """Retrieve runtime file limits and supported extensions."""
    return SystemConfigResponse(
        max_upload_bytes=settings.max_upload_bytes,
        max_upload_mb=round(settings.max_upload_bytes / (1024 * 1024), 2),
        max_uncompressed_bytes=settings.max_uncompressed_bytes,
        max_features=settings.max_features,
        allowed_extensions=[".kml", ".kmz", ".zip", ".geojson", ".json", ".gpkg"],
        version="1.1.0",
        has_mapbox=bool(settings.mapbox_access_token),
        auth_required=bool(settings.api_key),
        rate_limiting=is_rate_limiting_available(),
        async_processing=True,
        metrics_enabled=is_prometheus_available(),
        author={
            "name": settings.author_name,
            "github": settings.author_github,
            "repo": settings.repository_url,
        },
    )



@router.get(
    "/tiles/mapbox/{z}/{x}/{y}.png",
    summary="Secure Server-Side Mapbox Satellite Tile Proxy",
)
def get_mapbox_tile(
    z: int,
    x: int,
    y: int,
    settings: Settings = Depends(get_settings),
) -> Response:
    """Stream Mapbox Satellite Streets tiles. Falls back to Esri Satellite if token is invalid or unauthorized."""
    if settings.mapbox_access_token:
        tile_url = (
            f"https://api.mapbox.com/styles/v1/mapbox/satellite-streets-v12/tiles/{z}/{x}/{y}@2x"
            f"?access_token={settings.mapbox_access_token}"
        )
        try:
            with httpx.Client(timeout=4.0) as client:
                resp = client.get(tile_url)
                if resp.status_code == 200:
                    media_type = resp.headers.get("content-type", "image/jpeg")
                    return Response(
                        content=resp.content,
                        media_type=media_type,
                        headers={"Cache-Control": "public, max-age=86400"},
                    )
        except Exception:
            pass

    # Seamless fallback: Esri World Imagery (high-resolution optical satellite)
    fallback_url = f"https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
    try:
        with httpx.Client(timeout=4.0) as client:
            fb_resp = client.get(fallback_url)
            if fb_resp.status_code == 200:
                return Response(
                    content=fb_resp.content,
                    media_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=86400"},
                )
    except Exception:
        pass

    return Response(status_code=404, content=b"", media_type="image/png")


@router.get(
    "/tiles/carto/{z}/{x}/{y}.png",
    summary="Adblocker-Immune Server-Side Carto Dark Tile Proxy",
)
def get_carto_tile(
    z: int,
    x: int,
    y: int,
) -> Response:
    """Proxy Carto Dark Matter tiles through backend to prevent browser adblocker interception."""
    subdomains = ["a", "b", "c", "d"]
    subdomain = subdomains[(x + y) % len(subdomains)]
    carto_url = f"https://cartodb-basemaps-{subdomain}.global.ssl.fastly.net/dark_all/{z}/{x}/{y}.png"
    try:
        with httpx.Client(timeout=4.0) as client:
            resp = client.get(carto_url)
            if resp.status_code == 200:
                return Response(
                    content=resp.content,
                    media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"},
                )
    except Exception:
        pass
    return Response(status_code=404, content=b"", media_type="image/png")


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
    summary="Upload and process geospatial file (synchronous)",
)
def upload_file(
    file: UploadFile = File(..., description="Geospatial .kml, .kmz, .geojson, .gpkg, or Shapefile .zip"),
    ingest_service: IngestService = Depends(get_ingest_service),
    _auth: bool = Depends(require_api_key),
) -> FileInfo:
    """Upload and process a geospatial file synchronously in the threadpool.

    Supported formats: KML, KMZ, Shapefile ZIP, GeoJSON, GeoPackage.
    Returns 201 with complete measurements on success.
    """
    record = ingest_service.ingest_stream(file.file, file.filename)
    return FileInfo.from_domain(record)



@router.get(
    "/",
    response_model=list[FileInfo],
    summary="List all ingested geospatial files",
)
def list_files(
    limit: int = Query(50, ge=1, le=500, description="Page limit"),
    offset: int = Query(0, ge=0, description="Page offset"),
    repo: Repository = Depends(get_repository),
) -> list[FileInfo]:
    """Retrieve all ingested geospatial files in descending order of upload."""
    records = repo.list_files(limit=limit, offset=offset)
    return [FileInfo.from_domain(r) for r in records]


@router.get(
    "/latest",
    response_model=FileInfo,
    responses={
        404: {"model": ErrorResponse, "description": "No completed files available"},
    },
    summary="Get the most recently ingested completed file",
)
def get_latest_file(
    repo: Repository = Depends(get_repository),
) -> FileInfo:
    """Retrieve the latest completed geospatial dataset for instant visualization."""
    record = repo.get_latest_completed_file()
    if not record:
        raise NotFoundError("No completed geospatial files found in repository")
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


@router.get(
    "/{file_id}/export/kml/",
    summary="Export features with measurements as KML 2.2",
)
def export_file_kml(
    file_id: str,
    repo: Repository = Depends(get_repository),
) -> Response:
    """Export complete dataset as KML 2.2 with measurement attributes embedded as ExtendedData.

    Output includes Placemark entries with GeoJSON-derived coordinates converted to
    KML coordinate tuples, plus measurement results as key-value ExtendedData pairs.
    """
    import json
    import xml.etree.ElementTree as ET

    record = repo.get_file(file_id)
    if not record:
        raise NotFoundError(f"File '{file_id}' not found")
    if record.status != FileStatus.COMPLETED:
        raise ConflictError("Export requires COMPLETED status")

    features, _ = repo.get_features(file_id=file_id, limit=10_000, offset=0)
    _, stored_features, _ = repo.get_measurements(file_id=file_id, limit=10_000, offset=0)
    meas_by_idx = {sf.feature_index: sf for sf in stored_features}

    KML_NS = "http://www.opengis.net/kml/2.2"
    root = ET.Element(f"{{{KML_NS}}}kml")
    doc = ET.SubElement(root, f"{{{KML_NS}}}Document")
    name_el = ET.SubElement(doc, f"{{{KML_NS}}}name")
    name_el.text = record.filename

    def coords_to_kml(coords: list) -> str:
        """Convert GeoJSON [lon, lat] or [lon, lat, alt] coordinate array to KML string."""
        parts = []
        for c in coords:
            if len(c) >= 3:
                parts.append(f"{c[0]},{c[1]},{c[2]}")
            else:
                parts.append(f"{c[0]},{c[1]},0")
        return " ".join(parts)

    for feat in features:
        pm = ET.SubElement(doc, f"{{{KML_NS}}}Placemark")

        # Name from properties or index
        pm_name = ET.SubElement(pm, f"{{{KML_NS}}}name")
        props = feat.properties or {}
        pm_name.text = str(props.get("name", f"Feature #{feat.feature_index}"))

        # ExtendedData with measurement values
        meas = meas_by_idx.get(feat.feature_index)
        if meas:
            ext_data = ET.SubElement(pm, f"{{{KML_NS}}}ExtendedData")
            for key, value in [
                ("geometry_type", feat.geometry_type),
                ("measurement_status", meas.measurement_status.value),
                ("area_sq_m", str(meas.area_sq_m) if meas.area_sq_m is not None else ""),
                ("length_m", str(meas.length_m) if meas.length_m is not None else ""),
                ("measurement_crs", meas.measurement_crs or ""),
                ("measurement_notice", meas.measurement_message or ""),
            ]:
                data_el = ET.SubElement(ext_data, f"{{{KML_NS}}}Data", name=key)
                val_el = ET.SubElement(data_el, f"{{{KML_NS}}}value")
                val_el.text = value

        # Geometry element
        geom = feat.geometry
        geom_type = geom.get("type", "")
        geom_coords = geom.get("coordinates", [])

        if geom_type == "Point":
            pt = ET.SubElement(pm, f"{{{KML_NS}}}Point")
            coords_el = ET.SubElement(pt, f"{{{KML_NS}}}coordinates")
            c = geom_coords
            coords_el.text = f"{c[0]},{c[1]},{c[2] if len(c) > 2 else 0}"

        elif geom_type == "LineString":
            ls = ET.SubElement(pm, f"{{{KML_NS}}}LineString")
            coords_el = ET.SubElement(ls, f"{{{KML_NS}}}coordinates")
            coords_el.text = coords_to_kml(geom_coords)

        elif geom_type == "Polygon":
            poly = ET.SubElement(pm, f"{{{KML_NS}}}Polygon")
            for ring_idx, ring in enumerate(geom_coords):
                if ring_idx == 0:
                    ob = ET.SubElement(poly, f"{{{KML_NS}}}outerBoundaryIs")
                    lr = ET.SubElement(ob, f"{{{KML_NS}}}LinearRing")
                else:
                    ib = ET.SubElement(poly, f"{{{KML_NS}}}innerBoundaryIs")
                    lr = ET.SubElement(ib, f"{{{KML_NS}}}LinearRing")
                coords_el = ET.SubElement(lr, f"{{{KML_NS}}}coordinates")
                coords_el.text = coords_to_kml(ring)

        elif geom_type == "MultiPolygon":
            multi = ET.SubElement(pm, f"{{{KML_NS}}}MultiGeometry")
            for poly_coords in geom_coords:
                poly = ET.SubElement(multi, f"{{{KML_NS}}}Polygon")
                for ring_idx, ring in enumerate(poly_coords):
                    if ring_idx == 0:
                        ob = ET.SubElement(poly, f"{{{KML_NS}}}outerBoundaryIs")
                        lr = ET.SubElement(ob, f"{{{KML_NS}}}LinearRing")
                    else:
                        ib = ET.SubElement(poly, f"{{{KML_NS}}}innerBoundaryIs")
                        lr = ET.SubElement(ib, f"{{{KML_NS}}}LinearRing")
                    coords_el = ET.SubElement(lr, f"{{{KML_NS}}}coordinates")
                    coords_el.text = coords_to_kml(ring)

        elif geom_type == "MultiLineString":
            multi = ET.SubElement(pm, f"{{{KML_NS}}}MultiGeometry")
            for line_coords in geom_coords:
                ls = ET.SubElement(multi, f"{{{KML_NS}}}LineString")
                coords_el = ET.SubElement(ls, f"{{{KML_NS}}}coordinates")
                coords_el.text = coords_to_kml(line_coords)

    ET.indent(root, space="  ")
    kml_bytes = ET.tostring(root, encoding="unicode", xml_declaration=False)
    kml_output = '<?xml version="1.0" encoding="UTF-8"?>\n' + kml_bytes

    filename_clean = record.filename.rsplit(".", 1)[0]
    return Response(
        content=kml_output,
        media_type="application/vnd.google-earth.kml+xml",
        headers={"Content-Disposition": f'attachment; filename="{filename_clean}_measured.kml"'},
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
    _auth: bool = Depends(require_api_key),
) -> GeometryMeasurementResponse:
    """Calculate survey-grade planar and geodesic metrics for an arbitrary geometry in real-time."""
    result = measure_geometry_live(req.geometry, source_crs=req.source_crs)
    return GeometryMeasurementResponse(**result)


@measure_router.get(
    "/reverse-geocode/",
    response_model=ReverseGeocodeResponse,
    summary="Real-time reverse geocoding via query parameters",
)
def api_reverse_geocode_get(
    lat: float = Query(..., alias="lat", description="Latitude in decimal degrees"),
    lon: float = Query(..., alias="lon", description="Longitude in decimal degrees"),
    _auth: bool = Depends(require_api_key),
) -> ReverseGeocodeResponse:
    """Query administrative location context via GET query parameters."""
    res = reverse_geocode(lat, lon)
    return ReverseGeocodeResponse(**res)


@measure_router.post(
    "/reverse-geocode/",
    response_model=ReverseGeocodeResponse,
    summary="Real-time reverse geocoding to administrative location",
)
def api_reverse_geocode(
    req: ReverseGeocodeRequest,
    _auth: bool = Depends(require_api_key),
) -> ReverseGeocodeResponse:
    """Query administrative location context (country, city, region) for a coordinate."""
    res = reverse_geocode(req.latitude, req.longitude)
    return ReverseGeocodeResponse(**res)


@measure_router.get(
    "/elevation/",
    response_model=ElevationResponse,
    summary="Real-time terrain elevation query via query parameters",
)
def api_elevation_get(
    lat: float = Query(..., alias="lat", description="Latitude in decimal degrees"),
    lon: float = Query(..., alias="lon", description="Longitude in decimal degrees"),
    _auth: bool = Depends(require_api_key),
) -> ElevationResponse:
    """Query terrain elevation in metres above sea level via GET query parameters."""
    res = get_elevation(lat, lon)
    return ElevationResponse(**res)


@measure_router.post(
    "/elevation/",
    response_model=ElevationResponse,
    summary="Real-time terrain elevation query",
)
def api_elevation(
    req: ElevationRequest,
    _auth: bool = Depends(require_api_key),
) -> ElevationResponse:
    """Query terrain elevation in metres above sea level for a coordinate."""
    res = get_elevation(req.latitude, req.longitude)
    return ElevationResponse(**res)


# ---------------------------------------------------------------------------
# Async Background Ingestion Endpoints
# ---------------------------------------------------------------------------


@async_router.post(
    "/ingest/",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=TaskResponse,
    responses={
        413: {"model": ErrorResponse, "description": "File too large"},
        415: {"model": ErrorResponse, "description": "Unsupported media type"},
    },
    summary="Enqueue geospatial file for background ingestion (async)",
)
async def upload_file_async(
    file: UploadFile = File(
        ...,
        description="Geospatial .kml, .kmz, .geojson, .gpkg, or Shapefile .zip"
    ),
    settings: Settings = Depends(get_settings),
    _auth: bool = Depends(require_api_key),
) -> TaskResponse:
    """Enqueue a geospatial file for async background processing.

    Returns HTTP 202 Accepted immediately with a task_id.
    Poll GET /api/tasks/{task_id}/ to check processing status.
    Once COMPLETED, fetch results via GET /api/files/{file_id}/measurements/.
    """
    # Read the entire upload first (enforcing size limit)
    from app.services.ingest import _detect_format, sanitize_filename

    filename = sanitize_filename(file.filename)
    fmt = _detect_format(filename.lower())
    if fmt is None:
        raise HTTPException(
            status_code=415,
            detail="Unsupported file type. Accepted: .kml, .kmz, .zip, .geojson, .json, .gpkg",
        )

    content = await file.read()
    if len(content) > settings.max_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds maximum upload limit of {settings.max_upload_bytes} bytes.",
        )

    if not content:
        raise HTTPException(status_code=422, detail="Uploaded file is empty.")

    task = await task_queue.enqueue(content=content, filename=filename)
    return TaskResponse.from_domain(task)


# ---------------------------------------------------------------------------
# Task Polling Endpoints
# ---------------------------------------------------------------------------


@task_router.get(
    "/",
    response_model=list[TaskResponse],
    summary="List recent async ingestion tasks",
)
def list_tasks(
    limit: int = Query(50, ge=1, le=200, description="Page limit"),
    _auth: bool = Depends(require_api_key),
) -> list[TaskResponse]:
    """Return recent background ingestion tasks ordered newest first."""
    tasks = task_queue.list_tasks(limit=limit)
    return [TaskResponse.from_domain(t) for t in tasks]


@task_router.get(
    "/{task_id}/",
    response_model=TaskResponse,
    responses={
        404: {"model": ErrorResponse, "description": "Task not found"},
    },
    summary="Poll async task status",
)
def get_task(
    task_id: str,
    _auth: bool = Depends(require_api_key),
) -> TaskResponse:
    """Poll the current status of an async background ingestion task.

    Lifecycle: QUEUED → PROCESSING → COMPLETED | FAILED
    When COMPLETED, use the returned file_id to fetch full results.
    """
    task = task_queue.get_task(task_id)
    if task is None:
        raise NotFoundError(f"Task '{task_id}' not found.")
    return TaskResponse.from_domain(task)


# ---------------------------------------------------------------------------
# Prometheus Metrics Endpoint
# ---------------------------------------------------------------------------


def make_metrics_router() -> APIRouter:
    """Create the /metrics router (always public — compatible with Prometheus scrapers)."""
    metrics_router = APIRouter(tags=["Observability"])

    @metrics_router.get(
        "/metrics",
        include_in_schema=False,
        summary="Prometheus metrics exposition",
    )
    def prometheus_metrics() -> Response:
        """Expose current Prometheus metrics in text format for scraping."""
        if not is_prometheus_available():
            return Response(
                content="# prometheus_client not installed\n",
                media_type="text/plain",
                status_code=200,
            )
        content, content_type = get_metrics_output()
        return Response(content=content, media_type=content_type)

    return metrics_router


metrics_router = make_metrics_router()
