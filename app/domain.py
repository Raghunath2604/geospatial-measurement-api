"""Domain models and value objects.

Pure Python dataclasses and enums with ZERO third-party dependencies.
Represents core entities, statuses, and domain structures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class FileStatus(str, Enum):
    """Lifecycle status of an ingested geospatial file."""

    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class MeasurementStatus(str, Enum):
    """Result status of feature measurement."""

    MEASURED = "MEASURED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    UNSUPPORTED = "UNSUPPORTED"
    ERROR = "ERROR"


@dataclass(frozen=True)
class ParsedFeature:
    """A feature parsed from a raw geospatial source file."""

    index: int
    geometry_type: str
    geometry: dict[str, Any]
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ParsedFile:
    """Result of parsing an entire geospatial file."""

    features: list[ParsedFeature]
    source_crs: str | None
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Measurement:
    """Measurement outcome for an individual feature."""

    feature_index: int
    geometry_type: str
    status: MeasurementStatus
    area_sq_m: float | None = None
    length_m: float | None = None
    measurement_crs: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class StoredFeature:
    """A feature persisted in relational storage alongside its measurement."""

    id: int | None
    file_id: str
    feature_index: int
    geometry_type: str
    geometry: dict[str, Any]
    properties: dict[str, Any]
    source_crs: str
    measurement_status: MeasurementStatus
    area_sq_m: float | None
    length_m: float | None
    measurement_crs: str | None
    measurement_message: str | None
    created_at: str


@dataclass(frozen=True)
class FileRecord:
    """Metadata record of an uploaded and ingested geospatial file."""

    id: str
    filename: str
    feature_count: int
    crs: str | None
    status: FileStatus
    created_at: str
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass(frozen=True)
class MeasurementSummary:
    """Aggregate measurements summary calculated over an entire file."""

    feature_count: int
    measured_count: int
    not_applicable_count: int
    unsupported_count: int
    error_count: int
    total_area_sq_m: float
    total_length_m: float
