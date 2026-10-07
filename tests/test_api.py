"""Integration tests for the REST API endpoints using FastAPI TestClient."""

from __future__ import annotations

import io
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

SAMPLE_KML_PATH = os.path.join(os.path.dirname(__file__), "sample_data", "survey.kml")
SAMPLE_SHP_ZIP = os.path.join(
    os.path.dirname(__file__), "sample_data", "survey_shapefile.zip"
)


@pytest.fixture
def client() -> TestClient:
    """Fixture providing a TestClient backed by an isolated SQLite temp db."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = os.path.join(tmp_dir, "test_api.db")
        settings = Settings(
            db_path=db_path,
            max_upload_bytes=10 * 1024 * 1024,  # 10 MB for tests
            max_uncompressed_bytes=50 * 1024 * 1024,
            max_features=1000,
        )
        app = create_app(settings)
        with TestClient(app) as test_client:
            yield test_client


def test_health_check(client: TestClient) -> None:
    """Verify /health returns 200 OK with expected JSON."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "geo-measure-api"}


def test_upload_kml_flow(client: TestClient) -> None:
    """Verify full upload, status retrieval, measurements, and features flow for KML."""
    with open(SAMPLE_KML_PATH, "rb") as f:
        file_bytes = f.read()

    # 1. Upload KML (201 Created)
    resp = client.post(
        "/api/files/",
        files={
            "file": (
                "survey.kml",
                io.BytesIO(file_bytes),
                "application/vnd.google-earth.kml+xml",
            )
        },
    )
    assert resp.status_code == 201, resp.text
    file_info = resp.json()
    file_id = file_info["id"]

    assert file_info["filename"] == "survey.kml"
    assert file_info["feature_count"] == 5
    assert file_info["crs"] == "EPSG:4326"
    assert file_info["status"] == "COMPLETED"

    # 2. GET /api/files/{id}/
    get_resp = client.get(f"/api/files/{file_id}/")
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == file_id

    # 3. GET /api/files/{id}/measurements/ with pagination
    meas_resp = client.get(f"/api/files/{file_id}/measurements/?limit=2&offset=0")
    assert meas_resp.status_code == 200
    meas_data = meas_resp.json()

    assert meas_data["file_id"] == file_id
    assert meas_data["total"] == 5
    assert len(meas_data["measurements"]) == 2  # Page limit 2

    # Verify summary is whole-file despite limit=2
    summary = meas_data["summary"]
    assert summary["feature_count"] == 5
    assert summary["measured_count"] == 4  # 2 polygons + 2 linestrings
    assert summary["not_applicable_count"] == 1  # 1 GCP point
    assert summary["total_area_sq_m"] > 0.0
    assert summary["total_length_m"] > 0.0

    # 4. GET /api/files/{id}/features/
    feat_resp = client.get(f"/api/files/{file_id}/features/?limit=10&offset=0")
    assert feat_resp.status_code == 200
    feat_data = feat_resp.json()
    assert feat_data["total"] == 5
    assert len(feat_data["features"]) == 5
    assert feat_data["features"][0]["geometry"]["type"] == "Polygon"


def test_upload_shapefile_zip_flow(client: TestClient) -> None:
    """Verify full upload and measurements flow for Shapefile ZIP."""
    with open(SAMPLE_SHP_ZIP, "rb") as f:
        file_bytes = f.read()

    resp = client.post(
        "/api/files/",
        files={"file": ("parcels.zip", io.BytesIO(file_bytes), "application/zip")},
    )
    assert resp.status_code == 201, resp.text
    file_info = resp.json()
    file_id = file_info["id"]

    assert file_info["feature_count"] == 3
    assert file_info["status"] == "COMPLETED"

    meas_resp = client.get(f"/api/files/{file_id}/measurements/")
    assert meas_resp.status_code == 200
    data = meas_resp.json()
    assert data["total"] == 3
    assert data["summary"]["measured_count"] == 3
    assert data["summary"]["total_area_sq_m"] > 0.0


def test_upload_unsupported_media_type(client: TestClient) -> None:
    """Upload with unsupported extension must return 415."""
    resp = client.post(
        "/api/files/",
        files={
            "file": (
                "data.geojson",
                io.BytesIO(b'{"type":"FeatureCollection"}'),
                "application/json",
            )
        },
    )
    assert resp.status_code == 415
    assert "Unsupported file type" in resp.json()["detail"]


def test_upload_empty_file(client: TestClient) -> None:
    """Upload of 0-byte file must return 422."""
    resp = client.post(
        "/api/files/",
        files={
            "file": (
                "empty.kml",
                io.BytesIO(b""),
                "application/vnd.google-earth.kml+xml",
            )
        },
    )
    assert resp.status_code == 422
    assert "empty" in resp.json()["detail"].lower()


def test_upload_oversized_file() -> None:
    """Upload exceeding byte limit must return 413."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        settings = Settings(
            db_path=os.path.join(tmp_dir, "test.db"),
            max_upload_bytes=100,  # Tiny 100-byte cap
        )
        app = create_app(settings)
        with TestClient(app) as custom_client:
            big_content = b"A" * 500
            resp = custom_client.post(
                "/api/files/",
                files={
                    "file": ("large.kml", io.BytesIO(big_content), "application/xml")
                },
            )
            assert resp.status_code == 413
            assert "exceeds maximum limit" in resp.json()["detail"]


def test_upload_unparseable_file_persists_as_failed(client: TestClient) -> None:
    """Unparseable KML must return 422 and persist file record in FAILED status."""
    corrupt_kml = b"<kml><corrupted></kml>"
    resp = client.post(
        "/api/files/",
        files={"file": ("corrupt.kml", io.BytesIO(corrupt_kml), "application/xml")},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert "detail" in body
    assert "file" in body
    failed_file = body["file"]
    assert failed_file["status"] == "FAILED"
    assert failed_file["error"] is not None

    file_id = failed_file["id"]

    # Verify querying measurements for FAILED file returns 409 Conflict
    meas_resp = client.get(f"/api/files/{file_id}/measurements/")
    assert meas_resp.status_code == 409
    assert "FAILED" in meas_resp.json()["detail"]


def test_get_nonexistent_file_returns_404(client: TestClient) -> None:
    """Querying unknown file ID returns 404."""
    resp = client.get("/api/files/nonexistent-uuid-1234/")
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()
