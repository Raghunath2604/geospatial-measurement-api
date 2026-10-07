"""Unit tests for the KML parser and security protections."""

from __future__ import annotations

import os

import pytest

from app.errors import UnprocessableEntityError
from app.parsers.kml import parse_kml

SAMPLE_KML_PATH = os.path.join(os.path.dirname(__file__), "sample_data", "survey.kml")


def test_parse_sample_survey_kml() -> None:
    """Validate parsing of the comprehensive survey.kml dataset."""
    with open(SAMPLE_KML_PATH, "rb") as f:
        content = f.read()

    parsed = parse_kml(content)
    assert parsed.source_crs == "EPSG:4326"
    assert len(parsed.features) == 5

    # Feature 0: Polygon
    f0 = parsed.features[0]
    assert f0.index == 0
    assert f0.geometry_type == "Polygon"
    assert f0.properties.get("name") == "Plot Alpha - Agricultural Field"
    assert f0.properties.get("crop_type") == "Paddy Rice"

    # Feature 1: Donut Polygon
    f1 = parsed.features[1]
    assert f1.index == 1
    assert f1.geometry_type == "Polygon"
    assert len(f1.geometry["coordinates"]) == 2  # Outer + inner ring
    assert f1.properties.get("facility_id") == "RES-402"

    # Feature 2: LineString
    f2 = parsed.features[2]
    assert f2.index == 2
    assert f2.geometry_type == "LineString"
    assert f2.properties.get("altitude_m") == "120.0"

    # Feature 3: Point
    f3 = parsed.features[3]
    assert f3.index == 3
    assert f3.geometry_type == "Point"
    assert f3.geometry["coordinates"] == [77.5925, 12.9725]

    # Feature 4: Standalone LinearRing -> LineString
    f4 = parsed.features[4]
    assert f4.index == 4
    assert f4.geometry_type == "LineString"


def test_kml_multigeometry_homogeneous() -> None:
    """Test homogeneous MultiGeometry parsing into MultiPolygon."""
    kml_text = """<?xml version="1.0" encoding="UTF-8"?>
    <kml xmlns="http://www.opengis.net/kml/2.2">
      <Document>
        <Placemark>
          <name>Twin Parcels</name>
          <MultiGeometry>
            <Polygon>
              <outerBoundaryIs>
                <LinearRing>
                  <coordinates>0,0 1,0 1,1 0,1 0,0</coordinates>
                </LinearRing>
              </outerBoundaryIs>
            </Polygon>
            <Polygon>
              <outerBoundaryIs>
                <LinearRing>
                  <coordinates>2,2 3,2 3,3 2,3 2,2</coordinates>
                </LinearRing>
              </outerBoundaryIs>
            </Polygon>
          </MultiGeometry>
        </Placemark>
      </Document>
    </kml>
    """
    parsed = parse_kml(kml_text.encode("utf-8"))
    assert len(parsed.features) == 1
    assert parsed.features[0].geometry_type == "MultiPolygon"
    assert len(parsed.features[0].geometry["coordinates"]) == 2


def test_kml_multigeometry_heterogeneous() -> None:
    """Test mixed MultiGeometry parsing into GeometryCollection."""
    kml_text = """<?xml version="1.0" encoding="UTF-8"?>
    <kml xmlns="http://www.opengis.net/kml/2.2">
      <Document>
        <Placemark>
          <name>Mixed Collection</name>
          <MultiGeometry>
            <Point>
              <coordinates>0,0</coordinates>
            </Point>
            <LineString>
              <coordinates>0,0 1,1</coordinates>
            </LineString>
          </MultiGeometry>
        </Placemark>
      </Document>
    </kml>
    """
    parsed = parse_kml(kml_text.encode("utf-8"))
    assert len(parsed.features) == 1
    assert parsed.features[0].geometry_type == "GeometryCollection"


def test_kml_error_paths() -> None:
    """Test parser error handling on invalid inputs."""
    # 1. Empty content
    with pytest.raises(UnprocessableEntityError, match="empty"):
        parse_kml(b"")

    # 2. Non-XML content
    with pytest.raises(UnprocessableEntityError, match="XML parse error"):
        parse_kml(b"This is not XML")

    # 3. Wrong root element
    with pytest.raises(UnprocessableEntityError, match="expected <kml>"):
        parse_kml(b"<gpx version='1.1'></gpx>")

    # 4. No placemarks
    with pytest.raises(UnprocessableEntityError, match="No Placemark elements found"):
        parse_kml(b"<kml><Document><name>Empty</name></Document></kml>")

    # 5. Invalid coordinates range
    kml_bad_coords = """<kml><Document><Placemark><Point>
        <coordinates>999.0,999.0</coordinates>
    </Point></Placemark></Document></kml>"""
    with pytest.raises(UnprocessableEntityError, match="No usable Placemark features"):
        parse_kml(kml_bad_coords.encode("utf-8"))


def test_kml_xxe_protection() -> None:
    """Ensure XML entity expansion / XXE attacks do not resolve external entities."""
    xxe_kml = """<?xml version="1.0" encoding="UTF-8"?>
    <!DOCTYPE kml [
      <!ENTITY xxe SYSTEM "file:///etc/passwd">
    ]>
    <kml xmlns="http://www.opengis.net/kml/2.2">
      <Document>
        <Placemark>
          <name>&xxe;</name>
          <Point>
            <coordinates>0,0</coordinates>
          </Point>
        </Placemark>
      </Document>
    </kml>
    """
    parsed = parse_kml(xxe_kml.encode("utf-8"))
    # Entity should NOT be expanded to file contents
    assert parsed.features[0].properties.get("name") != "/etc/passwd"
