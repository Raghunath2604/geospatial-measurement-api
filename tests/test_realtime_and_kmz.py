"""Tests for real-time geometry measurement, KMZ parsing, and export endpoints."""

from __future__ import annotations

import io
import os
import tempfile
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

SAMPLE_KML_PATH = os.path.join(os.path.dirname(__file__), "sample_data", "survey.kml")


@pytest.fixture
def client() -> TestClient:
    """Fixture providing a TestClient backed by an isolated SQLite temp db."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = os.path.join(tmp_dir, "test_realtime.db")
        settings = Settings(
            db_path=db_path,
            max_upload_bytes=10 * 1024 * 1024,
            max_uncompressed_bytes=50 * 1024 * 1024,
            max_features=1000,
        )
        app = create_app(settings)
        with TestClient(app) as test_client:
            yield test_client


def test_measure_arbitrary_polygon_live(client: TestClient) -> None:
    """Verify POST /api/measure/geometry/ computes planar UTM and geodesic metrics."""
    # A ~100m x 100m square polygon in Bengaluru (12.96, 77.58)
    coords = [
        [77.5800, 12.9600],
        [77.5810, 12.9600],
        [77.5810, 12.9610],
        [77.5800, 12.9610],
        [77.5800, 12.9600],
    ]
    payload = {
        "geometry": {
            "type": "Polygon",
            "coordinates": [coords],
        },
        "source_crs": "EPSG:4326",
    }
    resp = client.post("/api/measure/geometry/", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["geometry_type"] == "Polygon"
    assert data["status"] == "MEASURED"
    assert data["planar_area_sq_m"] > 10_000.0  # ~11,800 m^2
    assert data["geodesic_area_sq_m"] > 10_000.0
    # UTM zone 43N for Bengaluru
    assert "32643" in data["measurement_crs"]
    assert data["vertex_count"] == 5
    assert data["centroid"] == pytest.approx([77.5805, 12.9605], abs=0.001)
    assert "area_hectares" in data["units"]
    assert "area_acres" in data["units"]
    assert "length_km" in data["units"]


def test_measure_point_live(client: TestClient) -> None:
    """Verify Point geometries return NOT_APPLICABLE with zero area/length."""
    payload = {
        "geometry": {
            "type": "Point",
            "coordinates": [77.58, 12.96],
        }
    }
    resp = client.post("/api/measure/geometry/", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "NOT_APPLICABLE"
    assert data["planar_area_sq_m"] == 0.0
    assert data["planar_length_m"] == 0.0


def test_measure_bowtie_polygon_live(client: TestClient) -> None:
    """Verify self-intersecting polygon returns topological warning notice."""
    bowtie_coords = [
        [0.0, 0.0],
        [1.0, 1.0],
        [0.0, 1.0],
        [1.0, 0.0],
        [0.0, 0.0],
    ]
    payload = {
        "geometry": {
            "type": "Polygon",
            "coordinates": [bowtie_coords],
        }
    }
    resp = client.post("/api/measure/geometry/", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["message"] is not None
    assert "topological flaws" in data["message"].lower() or "self-intersection" in data["message"].lower()


def test_upload_kmz_flow(client: TestClient) -> None:
    """Verify packaging survey.kml into a .kmz archive parses and processes correctly."""
    with open(SAMPLE_KML_PATH, "rb") as f:
        kml_content = f.read()

    # Create KMZ archive in memory
    kmz_buf = io.BytesIO()
    with zipfile.ZipFile(kmz_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("doc.kml", kml_content)
        zf.writestr("images/icon.png", b"\x89PNG\r\n\x1a\n...")

    kmz_bytes = kmz_buf.getvalue()

    resp = client.post(
        "/api/files/",
        files={"file": ("mission.kmz", io.BytesIO(kmz_bytes), "application/vnd.google-earth.kmz")},
    )
    assert resp.status_code == 201, resp.text
    file_data = resp.json()
    assert file_data["filename"] == "mission.kmz"
    assert file_data["status"] == "COMPLETED"
    assert file_data["feature_count"] == 5

    file_id = file_data["id"]

    # Verify GeoJSON export
    export_resp = client.get(f"/api/files/{file_id}/export/geojson/")
    assert export_resp.status_code == 200
    assert export_resp.headers["content-type"].startswith("application/geo+json")
    fc = export_resp.json()
    assert fc["type"] == "FeatureCollection"
    assert len(fc["features"]) == 5
    assert "_area_sq_m" in fc["features"][0]["properties"]

    # Verify CSV export
    csv_resp = client.get(f"/api/files/{file_id}/export/csv/")
    assert csv_resp.status_code == 200
    assert "text/csv" in csv_resp.headers["content-type"]
    assert "feature_index,geometry_type,status" in csv_resp.text


def test_reverse_geocode_and_elevation_endpoints(client: TestClient) -> None:
    """Verify reverse geocoding and elevation endpoints return structured schemas."""
    # Reverse geocode
    geo_resp = client.post(
        "/api/measure/reverse-geocode/",
        json={"latitude": 12.9716, "longitude": 77.5946},
    )
    assert geo_resp.status_code == 200
    geo_data = geo_resp.json()
    assert "display_name" in geo_data

    # Elevation
    elev_resp = client.post(
        "/api/measure/elevation/",
        json={"latitude": 12.9716, "longitude": 77.5946},
    )
    assert elev_resp.status_code == 200
    elev_data = elev_resp.json()
    assert "elevation_m" in elev_data
    assert "source" in elev_data
