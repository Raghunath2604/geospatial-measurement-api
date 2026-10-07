"""Ingestion orchestration service.

Handles file stream reception, validation, parsing, coordinate reference system
resolution, metric measurement execution, and atomic persistence.
Guarantees that parsing or processing failures are persisted as FAILED records
and converted to HTTP 422 responses with full file metadata.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from typing import BinaryIO

from app.config import Settings
from app.domain import FileRecord, ParsedFile
from app.errors import (
    GeoServiceError,
    PayloadTooLargeError,
    UnprocessableEntityError,
    UnsupportedMediaTypeError,
)
from app.parsers.kml import parse_kml
from app.parsers.shapefile_zip import parse_shapefile_zip
from app.services.measurement import MeasurementService, crs_label, resolve_crs
from app.storage.repository import Repository

logger = logging.getLogger("geomeasure.ingest")


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
            UnsupportedMediaTypeError: If extension is not .kml or .zip (415).
            PayloadTooLargeError: If uploaded content exceeds max_upload_bytes (413).
            UnprocessableEntityError: If file is empty, unparseable, or invalid (422).
        """
        filename = sanitize_filename(raw_filename)
        lower_name = filename.lower()

        # Validate file extension
        is_kml = lower_name.endswith(".kml")
        is_zip = lower_name.endswith(".zip")
        if not (is_kml or is_zip):
            raise UnsupportedMediaTypeError(
                "Unsupported file type. Only '.kml' and '.zip' (Shapefile archive) files are accepted."
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

        # Create record in PROCESSING status
        file_id = uuid.uuid4().hex
        self.repo.create_file(file_id=file_id, filename=filename)
        logger.info(
            "Initiated ingestion for file_id=%s filename='%s' (size=%d bytes)",
            file_id,
            filename,
            total_bytes,
        )

        parsed_file: ParsedFile | None = None
        warnings: list[str] = []

        try:
            # 1. Parse according to format
            if is_kml:
                parsed_file = parse_kml(
                    content, max_warnings=self.settings.max_warnings
                )
            else:
                parsed_file = parse_shapefile_zip(
                    content,
                    max_uncompressed_bytes=self.settings.max_uncompressed_bytes,
                    max_zip_members=self.settings.max_zip_members,
                    max_warnings=self.settings.max_warnings,
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
            logger.info(
                "Successfully ingested file_id=%s with %d features (CRS: %s)",
                file_id,
                len(features_and_measurements),
                resolved_crs_label,
            )
            return record

        except GeoServiceError as exc:
            logger.warning("Ingestion failed for file_id=%s: %s", file_id, exc.detail)
            failed_record = self.repo.fail_file(
                file_id=file_id, error=exc.detail, warnings=warnings
            )
            exc.extra["file"] = failed_record
            raise

        except Exception as exc:
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
