"""Shapefile ZIP archive parser.

Safely unpacks and parses Shapefile ZIP archives using pyshp (pure Python).
Guards against zip-slip directory traversals and zip-bomb resource exhaustion.
Enforces CRS resolution from .prj WKT with safe geographic fallback.
"""

from __future__ import annotations

import datetime
import io
import os
import shutil
import tempfile
import zipfile
from typing import Any

import shapefile
from pyproj.crs import CRS, CRSError

from app.domain import ParsedFeature, ParsedFile
from app.errors import UnprocessableEntityError
from app.services.projection import is_geographic_bounds


def _sanitize_record_value(val: Any) -> Any:
    """Format record values to ensure standard JSON serializability."""
    if isinstance(val, (datetime.date, datetime.datetime)):
        return val.isoformat()
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace")
    return val


def parse_shapefile_zip(
    content: bytes,
    max_uncompressed_bytes: int = 200 * 1024 * 1024,
    max_zip_members: int = 100,
    max_warnings: int = 50,
) -> ParsedFile:
    """Safely unpack and parse a Shapefile from a ZIP archive.

    Args:
        content: Raw bytes of the ZIP archive.
        max_uncompressed_bytes: Decompression byte ceiling.
        max_zip_members: Maximum number of members allowed in the zip.
        max_warnings: Warning list cutoff limit.

    Returns:
        ParsedFile with extracted features, resolved source CRS, and any warnings.

    Raises:
        UnprocessableEntityError: On corrupt zip, missing required files, path traversal,
            zip bomb, or unresolvable CRS.
    """
    if not content:
        raise UnprocessableEntityError("Uploaded ZIP file is empty")

    try:
        zip_buffer = io.BytesIO(content)
        archive = zipfile.ZipFile(zip_buffer)
    except zipfile.BadZipFile as exc:
        raise UnprocessableEntityError(
            f"Corrupt or invalid ZIP archive: {exc}"
        ) from exc

    infolist = archive.infolist()
    if len(infolist) > max_zip_members:
        raise UnprocessableEntityError(
            f"Zip archive contains {len(infolist)} files; maximum allowed is {max_zip_members}"
        )

    # 1. Filter and validate members (guard against zip-slip & bombs)
    valid_members: list[zipfile.ZipInfo] = []
    total_uncompressed = 0

    for info in infolist:
        name = info.filename
        # Ignore macOS metadata directories and hidden dotfiles
        if name.startswith("__MACOSX/") or "/__MACOSX/" in name:
            continue
        basename = os.path.basename(name)
        if basename.startswith(".") or not basename:
            continue

        # Prevent Zip-Slip path traversal
        norm_name = os.path.normpath(name)
        if (
            norm_name.startswith("..")
            or os.path.isabs(norm_name)
            or ".." in norm_name.split(os.path.sep)
        ):
            raise UnprocessableEntityError(
                f"Malicious zip member name detected (path traversal): {name}"
            )

        total_uncompressed += info.file_size
        if total_uncompressed > max_uncompressed_bytes:
            raise UnprocessableEntityError(
                f"Zip decompression limit exceeded ({max_uncompressed_bytes} bytes)"
            )

        valid_members.append(info)

    # 2. Check for required files (.shp and .dbf)
    shp_members = [m for m in valid_members if m.filename.lower().endswith(".shp")]
    if not shp_members:
        raise UnprocessableEntityError("Zip archive does not contain a .shp file")
    if len(shp_members) > 1:
        raise UnprocessableEntityError(
            f"Zip archive contains {len(shp_members)} shapefiles; exactly 1 .shp is expected"
        )

    shp_member = shp_members[0]
    shp_stem = os.path.splitext(shp_member.filename)[0]

    # Matching companion files by stem or lower-case extension
    dbf_members = [
        m
        for m in valid_members
        if os.path.splitext(m.filename)[0] == shp_stem
        and m.filename.lower().endswith(".dbf")
    ]
    if not dbf_members:
        raise UnprocessableEntityError(
            f"Zip archive is missing required .dbf companion file for {shp_member.filename}"
        )
    dbf_member = dbf_members[0]

    prj_members = [
        m
        for m in valid_members
        if os.path.splitext(m.filename)[0] == shp_stem
        and m.filename.lower().endswith(".prj")
    ]
    prj_member = prj_members[0] if prj_members else None

    shx_members = [
        m
        for m in valid_members
        if os.path.splitext(m.filename)[0] == shp_stem
        and m.filename.lower().endswith(".shx")
    ]
    shx_member = shx_members[0] if shx_members else None

    cpg_members = [
        m
        for m in valid_members
        if os.path.splitext(m.filename)[0] == shp_stem
        and m.filename.lower().endswith(".cpg")
    ]
    cpg_member = cpg_members[0] if cpg_members else None

    warnings: list[str] = []

    def add_warning(msg: str) -> None:
        if len(warnings) < max_warnings:
            warnings.append(msg)

    # 3. Extract needed members to a controlled temporary directory
    with tempfile.TemporaryDirectory() as tmp_dir:
        dest_shp = os.path.join(tmp_dir, "dataset.shp")
        dest_dbf = os.path.join(tmp_dir, "dataset.dbf")
        dest_prj = os.path.join(tmp_dir, "dataset.prj")
        dest_shx = os.path.join(tmp_dir, "dataset.shx")
        dest_cpg = os.path.join(tmp_dir, "dataset.cpg")

        # Copy only the matched members
        with archive.open(shp_member) as s, open(dest_shp, "wb") as d:
            shutil.copyfileobj(s, d)
        with archive.open(dbf_member) as s, open(dest_dbf, "wb") as d:
            shutil.copyfileobj(s, d)
        if prj_member:
            with archive.open(prj_member) as s, open(dest_prj, "wb") as d:
                shutil.copyfileobj(s, d)
        if shx_member:
            with archive.open(shx_member) as s, open(dest_shx, "wb") as d:
                shutil.copyfileobj(s, d)
        if cpg_member:
            with archive.open(cpg_member) as s, open(dest_cpg, "wb") as d:
                shutil.copyfileobj(s, d)

        # 4. Read encoding if .cpg exists
        encoding = "utf-8"
        if os.path.exists(dest_cpg):
            try:
                with open(dest_cpg, encoding="utf-8", errors="ignore") as f:
                    cpg_text = f.read().strip()
                    if cpg_text:
                        encoding = cpg_text
            except Exception:
                pass

        # 5. Open with pyshp
        reader = None
        try:
            try:
                reader = shapefile.Reader(dest_shp, encoding=encoding)
            except Exception as exc:
                # Fallback encoding to latin-1
                try:
                    reader = shapefile.Reader(dest_shp, encoding="latin-1")
                except Exception as inner_exc:
                    raise UnprocessableEntityError(
                        f"Unable to read shapefile: {inner_exc}"
                    ) from exc

            # 6. Resolve CRS
            source_crs: str | None = None
            if os.path.exists(dest_prj):
                try:
                    with open(dest_prj, encoding="utf-8", errors="ignore") as f:
                        prj_text = f.read().strip()
                    if prj_text:
                        crs_obj = CRS.from_user_input(prj_text)
                        epsg_code = crs_obj.to_epsg()
                        source_crs = f"EPSG:{epsg_code}" if epsg_code else prj_text
                except (CRSError, Exception) as exc:
                    raise UnprocessableEntityError(
                        f"Invalid or unparseable .prj WKT coordinate reference system: {exc}"
                    ) from exc

            if not source_crs:
                # Fallback logic: check bounding box
                bbox = reader.bbox
                if bbox and len(bbox) == 4:
                    min_x, min_y, max_x, max_y = bbox
                    if is_geographic_bounds(min_x, min_y, max_x, max_y):
                        source_crs = "EPSG:4326"
                        add_warning(
                            "Missing .prj file; assumed EPSG:4326 because bounding box is within geographic limits"
                        )
                    else:
                        raise UnprocessableEntityError(
                            f"Missing .prj file and bounding box [{min_x}, {min_y}, {max_x}, {max_y}] "
                            "exceeds geographic limits (indicates projected coordinates); CRS cannot be inferred"
                        )
                else:
                    # No bbox or empty shapes
                    source_crs = "EPSG:4326"
                    add_warning("Missing .prj file; defaulted to EPSG:4326")

            # 7. Extract features
            field_names = [f[0] for f in reader.fields[1:]]
            features: list[ParsedFeature] = []

            try:
                for idx, shape_rec in enumerate(reader.iterShapeRecords()):
                    shape = shape_rec.shape
                    # Check for null shape
                    if shape.shapeType == shapefile.NULL or not shape.points:
                        add_warning(f"Shape at index {idx} skipped: Null geometry")
                        continue

                    try:
                        geom = shape.__geo_interface__
                    except Exception as exc:
                        add_warning(
                            f"Shape at index {idx} skipped: Geometry conversion error ({exc})"
                        )
                        continue

                    # Build sanitized properties
                    raw_rec = shape_rec.record
                    properties: dict[str, Any] = {}
                    for f_idx, f_name in enumerate(field_names):
                        if f_idx < len(raw_rec):
                            properties[f_name] = _sanitize_record_value(raw_rec[f_idx])

                    features.append(
                        ParsedFeature(
                            index=idx,
                            geometry_type=geom["type"],
                            geometry=geom,
                            properties=properties,
                        )
                    )
            except Exception as exc:
                raise UnprocessableEntityError(
                    f"Error iterating shapefile records: {exc}"
                ) from exc

            if not features:
                raise UnprocessableEntityError("No usable features found in Shapefile")

            return ParsedFile(
                features=features, source_crs=source_crs, warnings=warnings
            )
        finally:
            if reader is not None:
                reader.close()
