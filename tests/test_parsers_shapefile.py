"""Unit tests for the Shapefile ZIP parser and security safeguards."""

from __future__ import annotations

import io
import os
import zipfile

import pytest
import shapefile

from app.errors import UnprocessableEntityError
from app.parsers.shapefile_zip import parse_shapefile_zip

SAMPLE_SHP_ZIP = os.path.join(
    os.path.dirname(__file__), "sample_data", "survey_shapefile.zip"
)


def test_parse_sample_shapefile_zip() -> None:
    """Validate parsing of the generated sample Shapefile ZIP dataset."""
    with open(SAMPLE_SHP_ZIP, "rb") as f:
        content = f.read()

    parsed = parse_shapefile_zip(content)
    assert parsed.source_crs is not None
    assert "4326" in parsed.source_crs
    assert len(parsed.features) == 3

    # Feature 0: Polygon
    f0 = parsed.features[0]
    assert f0.geometry_type == "Polygon"
    assert f0.properties.get("PLOT_ID") == "PARCEL-001"
    assert f0.properties.get("SURVEYOR") == "AeroSurvey Inc"
    assert f0.properties.get("ACREAGE") == 74.13
    assert f0.properties.get("SURVEY_DT") == "2026-03-15"

    # Feature 1: Donut polygon with cutout
    f1 = parsed.features[1]
    assert f1.geometry_type == "Polygon"
    assert len(f1.geometry["coordinates"]) == 2  # Outer ring and inner cutout


def _create_minimal_shp_bytes(
    bbox: list[float] | None = None,
    include_prj: bool = True,
    prj_wkt: str | None = None,
) -> bytes:
    """Helper to build an in-memory Shapefile ZIP."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        # Create shp & dbf
        shp_io = io.BytesIO()
        shx_io = io.BytesIO()
        dbf_io = io.BytesIO()

        w = shapefile.Writer(
            shp=shp_io, shx=shx_io, dbf=dbf_io, shapeType=shapefile.POLYGON
        )
        w.field("NAME", "C", size=20)
        coords = (
            [
                [bbox[0], bbox[1]],
                [bbox[2], bbox[1]],
                [bbox[2], bbox[3]],
                [bbox[0], bbox[3]],
                [bbox[0], bbox[1]],
            ]
            if bbox
            else [[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]
        )

        w.poly([coords])
        w.record(NAME="Test")
        w.close()

        z.writestr("test.shp", shp_io.getvalue())
        z.writestr("test.shx", shx_io.getvalue())
        z.writestr("test.dbf", dbf_io.getvalue())

        if include_prj:
            wkt = prj_wkt or (
                'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],'
                'PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]]'
            )
            z.writestr("test.prj", wkt)

    return buf.getvalue()


def test_missing_prj_geographic_fallback() -> None:
    """Shapefile lacking .prj but with geographic coordinates assumes EPSG:4326 with warning."""
    zip_bytes = _create_minimal_shp_bytes(
        bbox=[10.0, 20.0, 11.0, 21.0], include_prj=False
    )
    parsed = parse_shapefile_zip(zip_bytes)
    assert parsed.source_crs == "EPSG:4326"
    assert any("assumed EPSG:4326" in w for w in parsed.warnings)


def test_missing_prj_projected_rejection() -> None:
    """Shapefile lacking .prj with coordinates outside lat/lon bounds must be rejected (422)."""
    zip_bytes = _create_minimal_shp_bytes(
        bbox=[500000.0, 1400000.0, 501000.0, 1401000.0], include_prj=False
    )
    with pytest.raises(UnprocessableEntityError, match="exceeds geographic limits"):
        parse_shapefile_zip(zip_bytes)


def test_zip_corrupt_or_empty() -> None:
    """Validate rejection of empty or corrupt zip buffers."""
    with pytest.raises(UnprocessableEntityError, match="empty"):
        parse_shapefile_zip(b"")

    with pytest.raises(UnprocessableEntityError, match="Corrupt or invalid ZIP"):
        parse_shapefile_zip(b"PK\x03\x04not a valid zip")


def test_zip_missing_required_files() -> None:
    """Validate zip archives missing mandatory components."""
    # Zip with no .shp
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("hello.txt", "world")
    with pytest.raises(UnprocessableEntityError, match="does not contain a .shp file"):
        parse_shapefile_zip(buf.getvalue())

    # Zip with .shp but missing .dbf
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("data.shp", "dummy")
    with pytest.raises(
        UnprocessableEntityError, match="missing required .dbf companion file"
    ):
        parse_shapefile_zip(buf.getvalue())


def test_zip_multiple_shapefiles() -> None:
    """Zip containing multiple shapefiles should be rejected as ambiguous."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("a.shp", "shp1")
        z.writestr("a.dbf", "dbf1")
        z.writestr("b.shp", "shp2")
        z.writestr("b.dbf", "dbf2")
    with pytest.raises(UnprocessableEntityError, match="contains 2 shapefiles"):
        parse_shapefile_zip(buf.getvalue())


def test_zip_slip_path_traversal() -> None:
    """Detect and block malicious zip-slip member entries."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../../etc/passwd.shp", "evil")
        z.writestr("../../etc/passwd.dbf", "evil")
    with pytest.raises(UnprocessableEntityError, match="path traversal"):
        parse_shapefile_zip(buf.getvalue())


def test_zip_bomb_byte_limit() -> None:
    """Block decompression bombs exceeding max_uncompressed_bytes."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("huge.shp", b"0" * 2000)
        z.writestr("huge.dbf", b"0" * 2000)

    # Set byte cap lower than uncompressed size
    with pytest.raises(
        UnprocessableEntityError, match="Zip decompression limit exceeded"
    ):
        parse_shapefile_zip(buf.getvalue(), max_uncompressed_bytes=1000)
