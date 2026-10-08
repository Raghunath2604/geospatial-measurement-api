"""GeoPackage (.gpkg) parser using only stdlib sqlite3.

GeoPackage is an OGC standard SQLite database containing geospatial data.
This implementation is dependency-free, reading geometry blobs using the
GeoPackage binary encoding spec (OGC 12-128r18) and extracting WKB payloads
via a pure-Python WKB → GeoJSON converter backed by Shapely.

Security:
- File size is enforced at the ingest layer (streaming byte cap).
- Feature count is capped via max_features in IngestService.
- No external network calls; pure local sqlite3 read-only access.
"""

from __future__ import annotations

import logging
import sqlite3
import struct
import tempfile
from pathlib import Path
from typing import Any

from app.domain import ParsedFeature, ParsedFile
from app.errors import UnprocessableEntityError

logger = logging.getLogger("geomeasure.parsers.geopackage")

# GeoPackage geometry blob header magic bytes
_GPKG_MAGIC = b"GP"


def _parse_gpkg_header(blob: bytes) -> tuple[int, bytes]:
    """Parse GeoPackage Geometry Binary format header and extract WKB payload.

    Returns:
        (srs_id, wkb_bytes) — the SRID from the header and the raw WKB geometry.

    Raises:
        ValueError: If the blob is malformed or not a valid GPKG geometry.
    """
    if len(blob) < 8:
        raise ValueError("GPKG geometry blob is too short.")

    magic = blob[:2]
    if magic != _GPKG_MAGIC:
        raise ValueError(f"Invalid GPKG magic bytes: {magic!r}")

    # version = blob[2] (1 byte, ignored)
    flags = blob[3]

    # Byte order for header: bit 0 of flags
    # 0 = big-endian, 1 = little-endian
    byte_order = flags & 0x01
    endian = "<" if byte_order else ">"

    # Envelope type: bits 1-3 of flags
    envelope_type = (flags >> 1) & 0x07

    # SRS ID is bytes 4-7
    srs_id: int = struct.unpack_from(endian + "i", blob, 4)[0]

    # Envelope sizes (in doubles, 8 bytes each):
    # 0 = no envelope (0 doubles)
    # 1 = bbox (4 doubles = 32 bytes)
    # 2 = bbox + Z (6 doubles = 48 bytes)
    # 3 = bbox + M (6 doubles = 48 bytes)
    # 4 = bbox + ZM (8 doubles = 64 bytes)
    envelope_sizes = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}
    env_size = envelope_sizes.get(envelope_type, 0)

    # WKB starts after 8-byte header + envelope
    wkb_offset = 8 + env_size
    if wkb_offset >= len(blob):
        raise ValueError("GPKG geometry blob has no WKB payload after header.")

    return srs_id, blob[wkb_offset:]


def _wkb_to_geojson(wkb: bytes) -> dict[str, Any]:
    """Convert WKB geometry bytes to a GeoJSON geometry dict using Shapely."""
    try:
        from shapely import from_wkb
        from shapely.geometry import mapping

        geom = from_wkb(wkb)
        if geom is None or geom.is_empty:
            raise ValueError("Empty geometry from WKB.")
        return dict(mapping(geom))
    except ImportError as err:
        raise UnprocessableEntityError(
            "Shapely is required for GeoPackage WKB decoding but is not installed."
        ) from err
    except Exception as exc:
        raise ValueError(f"WKB decoding failed: {exc}") from exc


def _srs_to_epsg(srs_id: int) -> str:
    """Map a GeoPackage SRS ID to an EPSG CRS label."""
    if srs_id in (4326, 0):
        return "EPSG:4326"
    if srs_id == -1:
        return "EPSG:4326"  # Undefined geographic → assume WGS84
    return f"EPSG:{srs_id}"


def parse_geopackage(
    content: bytes,
    max_warnings: int = 50,
) -> ParsedFile:
    """Parse feature geometries from a GeoPackage (.gpkg) file.

    Args:
        content: Raw bytes of the .gpkg SQLite database file.
        max_warnings: Maximum number of parse warnings to collect.

    Returns:
        ParsedFile with parsed features and source CRS.

    Raises:
        UnprocessableEntityError: If the file is not a valid GeoPackage,
            contains no geometry tables, or has no parseable features.
    """
    if not content:
        raise UnprocessableEntityError("GeoPackage file is empty (0 bytes).")

    # GeoPackage files must begin with SQLite magic: "SQLite format 3\000"
    if not content.startswith(b"SQLite format 3\x00"):
        raise UnprocessableEntityError(
            "File does not appear to be a valid SQLite/GeoPackage database."
        )

    # Write to a temporary file so sqlite3 can open it
    with tempfile.NamedTemporaryFile(suffix=".gpkg", delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    features: list[ParsedFeature] = []
    warnings: list[str] = []
    source_crs: str = "EPSG:4326"

    try:
        conn = sqlite3.connect(f"file:{tmp_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row

        try:
            # Verify gpkg_contents table exists (required in a valid GeoPackage)
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='gpkg_contents';"
            ).fetchall()
            if not tables:
                raise UnprocessableEntityError(
                    "GeoPackage is missing required 'gpkg_contents' table. "
                    "This file may not be a valid GeoPackage."
                )

            # Enumerate all geometry feature tables
            geom_table_rows = conn.execute(
                """
                SELECT gc.table_name, gc.column_name, gc.srs_id
                FROM gpkg_geometry_columns gc
                JOIN gpkg_contents c ON gc.table_name = c.table_name
                WHERE c.data_type = 'features';
                """
            ).fetchall()

            if not geom_table_rows:
                raise UnprocessableEntityError(
                    "GeoPackage contains no feature geometry tables."
                )

            feat_idx = 0
            for table_row in geom_table_rows:
                table_name = table_row["table_name"]
                geom_col = table_row["column_name"]
                srs_id: int = table_row["srs_id"]

                # Set CRS from first geometry table's SRS
                if feat_idx == 0:
                    source_crs = _srs_to_epsg(srs_id)

                # Get all non-geometry column names for properties
                pragma_rows = conn.execute(
                    f'PRAGMA table_info("{table_name}");'
                ).fetchall()
                prop_columns = [
                    r["name"]
                    for r in pragma_rows
                    if r["name"] != geom_col
                ]

                # Build query selecting geometry + all property columns
                col_list = ", ".join(
                    [f'"{geom_col}"'] + [f'"{c}"' for c in prop_columns]
                )
                rows = conn.execute(
                    f'SELECT {col_list} FROM "{table_name}";'
                ).fetchall()

                for row in rows:
                    blob = row[geom_col]
                    if blob is None:
                        if len(warnings) < max_warnings:
                            warnings.append(
                                f"Table '{table_name}' feature[{feat_idx}]: "
                                "null geometry blob; skipped."
                            )
                        feat_idx += 1
                        continue

                    try:
                        _, wkb = _parse_gpkg_header(bytes(blob))
                        geom_dict = _wkb_to_geojson(wkb)
                    except (ValueError, Exception) as exc:
                        if len(warnings) < max_warnings:
                            warnings.append(
                                f"Table '{table_name}' feature[{feat_idx}]: "
                                f"geometry decode failed: {exc}; skipped."
                            )
                        feat_idx += 1
                        continue

                    # Build properties dict
                    props: dict[str, Any] = {}
                    for col in prop_columns:
                        val = row[col]
                        # sqlite returns Python native types; keep as-is
                        props[col] = val

                    features.append(
                        ParsedFeature(
                            index=len(features),
                            geometry_type=str(geom_dict.get("type", "Unknown")),
                            geometry=geom_dict,
                            properties=props,
                        )
                    )
                    feat_idx += 1

        finally:
            conn.close()

    except UnprocessableEntityError:
        raise
    except sqlite3.DatabaseError as exc:
        raise UnprocessableEntityError(
            f"GeoPackage SQLite read error: {exc}"
        ) from exc
    finally:
        # Clean up temporary file
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except Exception:
            pass

    if not features:
        raise UnprocessableEntityError(
            "GeoPackage yielded no parseable features after validation."
        )

    logger.info(
        "Parsed %d GeoPackage features (CRS: %s, %d warnings)",
        len(features),
        source_crs,
        len(warnings),
    )
    return ParsedFile(features=features, source_crs=source_crs, warnings=warnings)
