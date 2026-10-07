"""SQLite storage repository.

Implements thread-safe data persistence with SQLite WAL mode and foreign key integrity.
Manages atomic transactions for file ingestion and SQL aggregation queries for measurements.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.domain import (
    FileRecord,
    FileStatus,
    Measurement,
    MeasurementStatus,
    MeasurementSummary,
    ParsedFeature,
    StoredFeature,
)
from app.errors import NotFoundError


def _now_iso() -> str:
    """Current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


class Repository:
    """Thread-safe SQLite data access object with connection-per-call pattern."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._ensure_dir()
        self.init_db()

    def _ensure_dir(self) -> None:
        parent = Path(self.db_path).parent
        if parent and not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)

    def _get_connection(self) -> sqlite3.Connection:
        """Create a new short-lived SQLite connection configured with WAL and foreign keys."""
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        return conn

    def init_db(self) -> None:
        """Create schema tables and indices if they do not exist."""
        with self._get_connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS files (
                    id TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    feature_count INTEGER NOT NULL DEFAULT 0,
                    crs TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    warnings TEXT NOT NULL DEFAULT '[]',
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS features (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_id TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                    feature_index INTEGER NOT NULL,
                    geometry_type TEXT NOT NULL,
                    geometry TEXT NOT NULL,
                    properties TEXT NOT NULL,
                    source_crs TEXT NOT NULL,
                    measurement_status TEXT NOT NULL,
                    area_sq_m REAL,
                    length_m REAL,
                    measurement_crs TEXT,
                    measurement_message TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_features_file_id ON features(file_id);
                CREATE INDEX IF NOT EXISTS idx_features_file_idx ON features(file_id, feature_index);
                """
            )

    def create_file(self, file_id: str, filename: str) -> FileRecord:
        """Insert a new file record initialized in PROCESSING status."""
        created_at = _now_iso()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO files (id, filename, feature_count, crs, status, created_at, warnings, error)
                VALUES (?, ?, 0, NULL, ?, ?, '[]', NULL);
                """,
                (file_id, filename, FileStatus.PROCESSING.value, created_at),
            )
        return FileRecord(
            id=file_id,
            filename=filename,
            feature_count=0,
            crs=None,
            status=FileStatus.PROCESSING,
            created_at=created_at,
            warnings=[],
            error=None,
        )

    def fail_file(
        self, file_id: str, error: str, warnings: list[str] | None = None
    ) -> FileRecord:
        """Atomically transition a file record to FAILED status with error details."""
        warnings_json = json.dumps(warnings or [])
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE files
                SET status = ?, error = ?, warnings = ?
                WHERE id = ?;
                """,
                (FileStatus.FAILED.value, error, warnings_json, file_id),
            )

        record = self.get_file(file_id)
        if not record:
            raise NotFoundError(f"File '{file_id}' not found")
        return record

    def complete_file(
        self,
        file_id: str,
        crs: str,
        features_and_measurements: list[tuple[ParsedFeature, Measurement]],
        warnings: list[str],
    ) -> FileRecord:
        """Persist all features and measurements and update file status in ONE atomic transaction."""
        now = _now_iso()
        warnings_json = json.dumps(warnings)
        feature_rows: list[tuple[Any, ...]] = []

        for pf, m in features_and_measurements:
            feature_rows.append(
                (
                    file_id,
                    pf.index,
                    pf.geometry_type,
                    json.dumps(pf.geometry),
                    json.dumps(pf.properties),
                    crs,
                    m.status.value,
                    m.area_sq_m,
                    m.length_m,
                    m.measurement_crs,
                    m.message,
                    now,
                )
            )

        with self._get_connection() as conn:
            # Begin transaction
            conn.execute("BEGIN IMMEDIATE;")
            try:
                if feature_rows:
                    conn.executemany(
                        """
                        INSERT INTO features (
                            file_id, feature_index, geometry_type, geometry, properties,
                            source_crs, measurement_status, area_sq_m, length_m,
                            measurement_crs, measurement_message, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        feature_rows,
                    )

                conn.execute(
                    """
                    UPDATE files
                    SET feature_count = ?, crs = ?, status = ?, warnings = ?, error = NULL
                    WHERE id = ?;
                    """,
                    (
                        len(features_and_measurements),
                        crs,
                        FileStatus.COMPLETED.value,
                        warnings_json,
                        file_id,
                    ),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        record = self.get_file(file_id)
        if not record:
            raise NotFoundError(f"File '{file_id}' not found")
        return record

    def get_file(self, file_id: str) -> FileRecord | None:
        """Fetch a single file record by ID."""
        with self._get_connection() as conn:
            row = conn.execute(
                """
                SELECT id, filename, feature_count, crs, status, created_at, warnings, error
                FROM files
                WHERE id = ?;
                """,
                (file_id,),
            ).fetchone()

        if not row:
            return None

        warnings = json.loads(row["warnings"]) if row["warnings"] else []
        return FileRecord(
            id=row["id"],
            filename=row["filename"],
            feature_count=row["feature_count"],
            crs=row["crs"],
            status=FileStatus(row["status"]),
            created_at=row["created_at"],
            warnings=warnings,
            error=row["error"],
        )

    def list_files(self, limit: int = 50, offset: int = 0) -> list[FileRecord]:
        """Fetch list of all file records sorted newest first."""
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT id, filename, feature_count, crs, status, created_at, warnings, error
                FROM files
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?;
                """,
                (limit, offset),
            ).fetchall()

        records: list[FileRecord] = []
        for row in rows:
            warnings = json.loads(row["warnings"]) if row["warnings"] else []
            records.append(
                FileRecord(
                    id=row["id"],
                    filename=row["filename"],
                    feature_count=row["feature_count"],
                    crs=row["crs"],
                    status=FileStatus(row["status"]),
                    created_at=row["created_at"],
                    warnings=warnings,
                    error=row["error"],
                )
            )
        return records

    def get_latest_completed_file(self) -> FileRecord | None:
        """Fetch the most recent file with COMPLETED status."""
        with self._get_connection() as conn:
            row = conn.execute(
                """
                SELECT id, filename, feature_count, crs, status, created_at, warnings, error
                FROM files
                WHERE status = ?
                ORDER BY created_at DESC
                LIMIT 1;
                """,
                (FileStatus.COMPLETED.value,),
            ).fetchone()

        if not row:
            return None

        warnings = json.loads(row["warnings"]) if row["warnings"] else []
        return FileRecord(
            id=row["id"],
            filename=row["filename"],
            feature_count=row["feature_count"],
            crs=row["crs"],
            status=FileStatus(row["status"]),
            created_at=row["created_at"],
            warnings=warnings,
            error=row["error"],
        )

    def get_measurements_summary(self, file_id: str) -> MeasurementSummary:
        """Calculate whole-file measurement summary directly via SQL aggregates."""
        with self._get_connection() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) as total_features,
                    SUM(CASE WHEN measurement_status = 'MEASURED' THEN 1 ELSE 0 END) as measured_count,
                    SUM(CASE WHEN measurement_status = 'NOT_APPLICABLE' THEN 1 ELSE 0 END) as not_applicable_count,
                    SUM(CASE WHEN measurement_status = 'UNSUPPORTED' THEN 1 ELSE 0 END) as unsupported_count,
                    SUM(CASE WHEN measurement_status = 'ERROR' THEN 1 ELSE 0 END) as error_count,
                    COALESCE(SUM(area_sq_m), 0.0) as total_area_sq_m,
                    COALESCE(SUM(length_m), 0.0) as total_length_m
                FROM features
                WHERE file_id = ?;
                """,
                (file_id,),
            ).fetchone()

        if not row:
            return MeasurementSummary(0, 0, 0, 0, 0, 0.0, 0.0)

        return MeasurementSummary(
            feature_count=row["total_features"] or 0,
            measured_count=row["measured_count"] or 0,
            not_applicable_count=row["not_applicable_count"] or 0,
            unsupported_count=row["unsupported_count"] or 0,
            error_count=row["error_count"] or 0,
            total_area_sq_m=round(float(row["total_area_sq_m"] or 0.0), 2),
            total_length_m=round(float(row["total_length_m"] or 0.0), 2),
        )

    def get_measurements(
        self, file_id: str, limit: int = 100, offset: int = 0
    ) -> tuple[MeasurementSummary, list[StoredFeature], int]:
        """Fetch whole-file summary and paginated feature measurement records."""
        summary = self.get_measurements_summary(file_id)

        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT id, file_id, feature_index, geometry_type, geometry, properties,
                       source_crs, measurement_status, area_sq_m, length_m,
                       measurement_crs, measurement_message, created_at
                FROM features
                WHERE file_id = ?
                ORDER BY feature_index ASC
                LIMIT ? OFFSET ?;
                """,
                (file_id, limit, offset),
            ).fetchall()

        features: list[StoredFeature] = []
        for r in rows:
            features.append(
                StoredFeature(
                    id=r["id"],
                    file_id=r["file_id"],
                    feature_index=r["feature_index"],
                    geometry_type=r["geometry_type"],
                    geometry=json.loads(r["geometry"]),
                    properties=json.loads(r["properties"]),
                    source_crs=r["source_crs"],
                    measurement_status=MeasurementStatus(r["measurement_status"]),
                    area_sq_m=r["area_sq_m"],
                    length_m=r["length_m"],
                    measurement_crs=r["measurement_crs"],
                    measurement_message=r["measurement_message"],
                    created_at=r["created_at"],
                )
            )

        return summary, features, summary.feature_count

    def get_features(
        self, file_id: str, limit: int = 100, offset: int = 0
    ) -> tuple[list[StoredFeature], int]:
        """Fetch paginated features for a given file."""
        with self._get_connection() as conn:
            count_row = conn.execute(
                "SELECT COUNT(*) as total FROM features WHERE file_id = ?;", (file_id,)
            ).fetchone()
            total = count_row["total"] if count_row else 0

            rows = conn.execute(
                """
                SELECT id, file_id, feature_index, geometry_type, geometry, properties,
                       source_crs, measurement_status, area_sq_m, length_m,
                       measurement_crs, measurement_message, created_at
                FROM features
                WHERE file_id = ?
                ORDER BY feature_index ASC
                LIMIT ? OFFSET ?;
                """,
                (file_id, limit, offset),
            ).fetchall()

        features: list[StoredFeature] = []
        for r in rows:
            features.append(
                StoredFeature(
                    id=r["id"],
                    file_id=r["file_id"],
                    feature_index=r["feature_index"],
                    geometry_type=r["geometry_type"],
                    geometry=json.loads(r["geometry"]),
                    properties=json.loads(r["properties"]),
                    source_crs=r["source_crs"],
                    measurement_status=MeasurementStatus(r["measurement_status"]),
                    area_sq_m=r["area_sq_m"],
                    length_m=r["length_m"],
                    measurement_crs=r["measurement_crs"],
                    measurement_message=r["measurement_message"],
                    created_at=r["created_at"],
                )
            )

        return features, total
