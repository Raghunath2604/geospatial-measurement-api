"""Pydantic v2 schemas for request and response models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.domain import (
    AsyncTask,
    FileRecord,
    FileStatus,
    MeasurementStatus,
    StoredFeature,
)


class FileInfo(BaseModel):
    """Metadata response for an uploaded and ingested geospatial file."""

    id: str = Field(..., description="Unique file identifier (UUID4 hex)")
    filename: str = Field(..., description="Sanitized original filename")
    feature_count: int = Field(..., description="Total number of features in the file")
    crs: str | None = Field(
        None, description="Resolved source Coordinate Reference System"
    )
    status: FileStatus = Field(
        ..., description="Ingestion status (PROCESSING, COMPLETED, FAILED)"
    )
    created_at: str = Field(..., description="ISO-8601 creation timestamp")
    warnings: list[str] = Field(
        default_factory=list, description="Parser warnings encountered"
    )
    error: str | None = Field(None, description="Error explanation if status is FAILED")

    @classmethod
    def from_domain(cls, record: FileRecord) -> FileInfo:
        return cls(
            id=record.id,
            filename=record.filename,
            feature_count=record.feature_count,
            crs=record.crs,
            status=record.status,
            created_at=record.created_at,
            warnings=record.warnings,
            error=record.error,
        )


class MeasurementSummarySchema(BaseModel):
    """Aggregate measurements summary over the entire file."""

    feature_count: int = Field(..., description="Total features in the file")
    measured_count: int = Field(
        ..., description="Number of successfully measured features"
    )
    not_applicable_count: int = Field(..., description="Point / MultiPoint features")
    unsupported_count: int = Field(..., description="Unsupported or empty geometries")
    error_count: int = Field(
        ..., description="Features that encountered calculation errors"
    )
    total_area_sq_m: float = Field(
        ..., description="Sum of polygon areas in square metres"
    )
    total_length_m: float = Field(..., description="Sum of line lengths in metres")


class MeasurementItem(BaseModel):
    """Measurement outcome for an individual feature."""

    feature_index: int = Field(
        ..., description="0-based index of feature in source file"
    )
    geometry_type: str = Field(..., description="GeoJSON geometry type")
    status: MeasurementStatus = Field(..., description="Feature measurement status")
    area_sq_m: float | None = Field(
        None, description="Area in square metres if Polygon"
    )
    length_m: float | None = Field(None, description="Length in metres if LineString")
    measurement_crs: str | None = Field(None, description="Projected metric CRS used")
    message: str | None = Field(
        None, description="Notice or warning (e.g. self-intersection)"
    )


class MeasurementsResponse(BaseModel):
    """Paginated measurements response with whole-file summary."""

    file_id: str
    source_crs: str | None
    units: dict[str, str] = Field(
        default_factory=lambda: {"area": "sq_m", "length": "m"}
    )
    summary: MeasurementSummarySchema
    total: int
    limit: int
    offset: int
    measurements: list[MeasurementItem]


class FeatureItem(BaseModel):
    """GeoJSON-compatible feature representation in the source CRS."""

    feature_index: int
    geometry_type: str
    geometry: dict[str, Any]
    properties: dict[str, Any]
    source_crs: str
    created_at: str

    @classmethod
    def from_domain(cls, feat: StoredFeature) -> FeatureItem:
        return cls(
            feature_index=feat.feature_index,
            geometry_type=feat.geometry_type,
            geometry=feat.geometry,
            properties=feat.properties,
            source_crs=feat.source_crs,
            created_at=feat.created_at,
        )


class FeaturesResponse(BaseModel):
    """Paginated features response."""

    file_id: str
    total: int
    limit: int
    offset: int
    features: list[FeatureItem]


class ErrorResponse(BaseModel):
    """Standardized error response model."""

    detail: str
    file: FileInfo | None = None


class TaskResponse(BaseModel):
    """Async ingestion task status response."""

    task_id: str = Field(..., description="Unique task identifier")
    file_id: str = Field(..., description="Associated file ID (available after COMPLETED)")
    status: FileStatus = Field(..., description="Task lifecycle status")
    filename: str = Field(..., description="Uploaded filename")
    created_at: str = Field(..., description="ISO-8601 task creation timestamp")
    error: str | None = Field(None, description="Error detail if status is FAILED")

    @classmethod
    def from_domain(cls, task: AsyncTask) -> TaskResponse:
        return cls(
            task_id=task.task_id,
            file_id=task.file_id,
            status=task.status,
            filename=task.filename,
            created_at=task.created_at,
            error=task.error,
        )


class SystemConfigResponse(BaseModel):
    """Runtime limits and system configuration."""

    max_upload_bytes: int = Field(..., description="Max upload file size in bytes")
    max_upload_mb: float = Field(..., description="Max upload size in megabytes")
    max_uncompressed_bytes: int = Field(
        ..., description="Max uncompressed zip size in bytes"
    )
    max_features: int = Field(..., description="Max features allowed per file")
    allowed_extensions: list[str] = Field(
        default_factory=lambda: [".kml", ".kmz", ".zip", ".geojson", ".json", ".gpkg"],
        description="Supported geospatial file extensions",
    )
    version: str = Field(default="1.1.0", description="API version")
    has_mapbox: bool = Field(
        default=False, description="Whether server-side Mapbox Satellite proxy is active"
    )
    auth_required: bool = Field(
        default=False, description="Whether API key authentication is enforced"
    )
    rate_limiting: bool = Field(
        default=False, description="Whether request rate limiting is active"
    )
    async_processing: bool = Field(
        default=True, description="Whether async background ingestion queue is available"
    )
    metrics_enabled: bool = Field(
        default=False, description="Whether Prometheus metrics endpoint is active"
    )
    author: dict[str, str] = Field(
        default_factory=lambda: {
            "name": "Raghunath",
            "github": "https://github.com/Raghunath2604",
            "repo": "https://github.com/Raghunath2604/geospatial-measurement-api",
        },
        description="Engineering and project attribution metadata",
    )



class GeometryMeasurementRequest(BaseModel):
    """Payload for real-time arbitrary GeoJSON geometry measurement."""

    geometry: dict[str, Any] = Field(..., description="GeoJSON geometry object")
    source_crs: str = Field(
        default="EPSG:4326",
        description="Source Coordinate Reference System (defaults to EPSG:4326)",
    )


class GeometryMeasurementResponse(BaseModel):
    """Real-time geometric calculation response with dual planar and geodesic metrics."""

    geometry_type: str = Field(..., description="Evaluated geometry type")
    status: str = Field(..., description="Measurement status (MEASURED, NOT_APPLICABLE, etc.)")
    planar_area_sq_m: float | None = Field(None, description="Planar area in square metres (UTM/UPS)")
    planar_length_m: float | None = Field(None, description="Planar length or perimeter in metres")
    geodesic_area_sq_m: float | None = Field(None, description="Geodesic ellipsoidal area (WGS 84)")
    geodesic_length_m: float | None = Field(None, description="Geodesic ellipsoidal perimeter/length (WGS 84)")
    measurement_crs: str = Field(..., description="Dynamically resolved UTM or UPS projection")
    centroid: list[float] = Field(..., description="Centroid [longitude, latitude]")
    bbox: list[float] = Field(..., description="Bounding box [min_x, min_y, max_x, max_y]")
    vertex_count: int = Field(..., description="Total coordinate vertices")
    units: dict[str, float] = Field(..., description="Multi-unit planar measurement conversions")
    message: str | None = Field(None, description="Topological validity notices or warnings")


class ReverseGeocodeRequest(BaseModel):
    """Payload for real-time reverse geocoding."""

    latitude: float = Field(..., ge=-90.0, le=90.0, description="Latitude [-90..90]")
    longitude: float = Field(..., ge=-180.0, le=180.0, description="Longitude [-180..180]")


class ReverseGeocodeResponse(BaseModel):
    """Administrative address and territorial location response."""

    display_name: str
    country: str | None = None
    state: str | None = None
    city: str | None = None
    postcode: str | None = None
    osm_id: int | None = None


class ElevationRequest(BaseModel):
    """Payload for real-time terrain elevation lookup."""

    latitude: float = Field(..., ge=-90.0, le=90.0, description="Latitude [-90..90]")
    longitude: float = Field(..., ge=-180.0, le=180.0, description="Longitude [-180..180]")


class ElevationResponse(BaseModel):
    """Terrain elevation response in metres and feet."""

    elevation_m: float
    elevation_ft: float
    source: str

