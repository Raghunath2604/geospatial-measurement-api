"""Ingestion orchestration service.

Handles file stream reception, validation, parsing, coordinate reference system
resolution, metric measurement execution, and atomic persistence.
Guarantees that parsing or processing failures are persisted as FAILED records
and converted to HTTP 422 responses with full file metadata.

Supported formats:
    - KML (.kml): Google Earth Keyhole Markup Language
    - KMZ (.kmz): Compressed KML archive
    - Shapefile (.zip): ESRI Shapefile archive
    - GeoJSON (.geojson, .json): RFC 7946 FeatureCollection
    - GeoPackage (.gpkg): OGC GeoPackage SQLite container
"""

from __future__ import annotations

import logging
import os
import re
import time
import uuid
from typing import BinaryIO

from app.config import Settings
from app.domain import FileFormat, FileRecord, ParsedFile
from app.errors import (
    GeoServiceError,
    PayloadTooLargeError,
    UnprocessableEntityError,
    UnsupportedMediaTypeError,
)
from app.monitoring.metrics import record_ingest
from app.parsers.geojson import parse_geojson
from app.parsers.geopackage import parse_geopackage
from app.parsers.kml import parse_kml
from app.parsers.kmz import parse_kmz
from app.parsers.shapefile_zip import parse_shapefile_zip
from app.services.measurement import MeasurementService, crs_label, resolve_crs
from app.storage.repository import Repository

logger = logging.getLogger("geomeasure.ingest")

# Mapping of lowercase extension → FileFormat
_EXT_FORMAT: dict[str, FileFormat] = {
    ".kml": FileFormat.KML,
    ".kmz": FileFormat.KMZ,
    ".zip": FileFormat.SHAPEFILE,
    ".geojson": FileFormat.GEOJSON,
    ".json": FileFormat.GEOJSON,
    ".gpkg": FileFormat.GEOPACKAGE,
}

_ALLOWED_EXTENSIONS: frozenset[str] = frozenset(_EXT_FORMAT.keys())


def sanitize_filename(filename: str | None) -> str:
    """Extract a safe basename stripped of path elements and unsafe characters."""
    if not filename:
        return "unnamed_upload"
    # Strip any directory components
    basename = os.path.basename(filename).strip()
    if not basename:
        return "unnamed_upload"
    # Replace unsafe characters with underscore, keeping letters, numbers, dots, dashes, underscores
    sanitized = re.sub(r"[^\w\.-]", "_", basename)
    return sanitized or "unnamed_upload"


def _detect_format(lower_name: str) -> FileFormat | None:
    """Return the FileFormat for a given lowercase filename, or None if unsupported."""
    for ext, fmt in _EXT_FORMAT.items():
        if lower_name.endswith(ext):
            return fmt
    return None


class IngestService:
    """Orchestrates streaming ingest and measurement processing."""

    def __init__(self, repository: Repository, settings: Settings) -> None:
        self.repo = repository
        self.settings = settings

    def ingest_stream(
        self, file_stream: BinaryIO, raw_filename: str | None
    ) -> FileRecord:
        """Stream an incoming file, validate size and type, process, and persist.

        Args:
            file_stream: Readable binary stream of uploaded content.
            raw_filename: Client-supplied filename.

        Returns:
            Completed FileRecord.

        Raises:
            UnsupportedMediaTypeError: If extension is not a supported format (415).
            PayloadTooLargeError: If uploaded content exceeds max_upload_bytes (413).
            UnprocessableEntityError: If file is empty, unparseable, or invalid (422).
        """
        filename = sanitize_filename(raw_filename)
        lower_name = filename.lower()

        # Validate file extension
        fmt = _detect_format(lower_name)
        if fmt is None:
            raise UnsupportedMediaTypeError(
                f"Unsupported file type. Accepted: {', '.join(sorted(_ALLOWED_EXTENSIONS))}."
            )

        # Stream chunks and enforce byte limit
        chunks: list[bytes] = []
        total_bytes = 0

        while True:
            chunk = file_stream.read(self.settings.chunk_size_bytes)
            if not chunk:
                break
            total_bytes += len(chunk)
            if total_bytes > self.settings.max_upload_bytes:
                raise PayloadTooLargeError(
                    f"Uploaded file exceeds maximum limit of {self.settings.max_upload_bytes} bytes."
                )
            chunks.append(chunk)

        if total_bytes == 0:
            raise UnprocessableEntityError("Uploaded file is empty (0 bytes).")

        content = b"".join(chunks)
        start_time = time.perf_counter()

        # Create record in PROCESSING status
        file_id = uuid.uuid4().hex
        self.repo.create_file(file_id=file_id, filename=filename)
        logger.info(
            "Initiated ingestion for file_id=%s filename='%s' (size=%d bytes, format=%s)",
            file_id,
            filename,
            total_bytes,
            fmt.value,
        )

        parsed_file: ParsedFile | None = None
        warnings: list[str] = []

        try:
            # 1. Parse according to format
            if fmt == FileFormat.KML:
                parsed_file = parse_kml(
                    content, max_warnings=self.settings.max_warnings
                )
            elif fmt == FileFormat.KMZ:
                parsed_file = parse_kmz(
                    content,
                    max_uncompressed_bytes=self.settings.max_uncompressed_bytes,
                    max_zip_members=self.settings.max_zip_members,
                    max_warnings=self.settings.max_warnings,
                )
            elif fmt == FileFormat.SHAPEFILE:
                parsed_file = parse_shapefile_zip(
                    content,
                    max_uncompressed_bytes=self.settings.max_uncompressed_bytes,
                    max_zip_members=self.settings.max_zip_members,
                    max_warnings=self.settings.max_warnings,
                )
            elif fmt == FileFormat.GEOJSON:
                parsed_file = parse_geojson(
                    content,
                    max_warnings=self.settings.max_warnings,
                )
            elif fmt == FileFormat.GEOPACKAGE:
                parsed_file = parse_geopackage(
                    content,
                    max_warnings=self.settings.max_warnings,
                )
            else:
                raise UnsupportedMediaTypeError(
                    f"Internal: unhandled format '{fmt.value}'."
                )

            warnings = parsed_file.warnings

            # 2. Check feature count limit
            if len(parsed_file.features) > self.settings.max_features:
                raise UnprocessableEntityError(
                    f"File contains {len(parsed_file.features)} features, "
                    f"exceeding the limit of {self.settings.max_features}."
                )

            # 3. Resolve and validate CRS
            crs_obj = resolve_crs(parsed_file.source_crs)
            resolved_crs_label = crs_label(crs_obj)

            # 4. Measure each feature
            meas_service = MeasurementService(crs_obj)
            features_and_measurements = []
            for feat in parsed_file.features:
                measurement = meas_service.measure(feat.index, feat.geometry)
                features_and_measurements.append((feat, measurement))

            # 5. Persist features and transition status to COMPLETED
            record = self.repo.complete_file(
                file_id=file_id,
                crs=resolved_crs_label,
                features_and_measurements=features_and_measurements,
                warnings=warnings,
            )

            duration = time.perf_counter() - start_time
            record_ingest(
                fmt=fmt.value,
                status="COMPLETED",
                duration_s=duration,
                file_size_bytes=total_bytes,
                feature_count=len(features_and_measurements),
            )
            logger.info(
                "Successfully ingested file_id=%s with %d features (CRS: %s, %.3fs)",
                file_id,
                len(features_and_measurements),
                resolved_crs_label,
                duration,
            )
            return record

        except GeoServiceError as exc:
            duration = time.perf_counter() - start_time
            record_ingest(
                fmt=fmt.value,
                status="FAILED",
                duration_s=duration,
                file_size_bytes=total_bytes,
            )
            logger.warning("Ingestion failed for file_id=%s: %s", file_id, exc.detail)
            failed_record = self.repo.fail_file(
                file_id=file_id, error=exc.detail, warnings=warnings
            )
            exc.extra["file"] = failed_record
            raise

        except Exception as exc:
            duration = time.perf_counter() - start_time
            record_ingest(
                fmt=fmt.value,
                status="FAILED",
                duration_s=duration,
                file_size_bytes=total_bytes,
            )
            logger.exception(
                "Unexpected error during ingestion for file_id=%s: %s", file_id, exc
            )
            err_msg = f"Internal ingestion error: {exc}"
            failed_record = self.repo.fail_file(
                file_id=file_id, error=err_msg, warnings=warnings
            )
            raise UnprocessableEntityError(
                err_msg, extra={"file": failed_record}
            ) from exc
