"""Pydantic v2 schemas for request and response models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.domain import FileRecord, FileStatus, MeasurementStatus, StoredFeature


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


class SystemConfigResponse(BaseModel):
    """Runtime limits and system configuration."""

    max_upload_bytes: int = Field(..., description="Max upload file size in bytes")
    max_upload_mb: float = Field(..., description="Max upload size in megabytes")
    max_uncompressed_bytes: int = Field(
        ..., description="Max uncompressed zip size in bytes"
    )
    max_features: int = Field(..., description="Max features allowed per file")
    allowed_extensions: list[str] = Field(
        default_factory=lambda: [".kml", ".zip"],
        description="Supported geospatial file extensions",
    )
    version: str = Field(default="1.0.0", description="API version")
