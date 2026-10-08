"""Tests for new backend features: GeoJSON parser, GeoPackage parser,
async task queue, Prometheus metrics, and API key authentication.
"""

from __future__ import annotations

import io
import json
import sqlite3
import struct
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# GeoJSON Parser Tests
# ---------------------------------------------------------------------------


class TestGeoJSONParser:
    """Tests for app.parsers.geojson.parse_geojson."""

    def test_valid_feature_collection(self) -> None:
        from app.parsers.geojson import parse_geojson

        doc = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [[77.59, 12.97], [77.595, 12.97], [77.595, 12.975], [77.59, 12.975], [77.59, 12.97]]
                        ],
                    },
                    "properties": {"name": "Farm"},
                },
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[77.59, 12.97], [77.60, 12.98]],
                    },
                    "properties": {"name": "Road"},
                },
            ],
        }
        result = parse_geojson(json.dumps(doc).encode())
        assert len(result.features) == 2
        assert result.source_crs == "EPSG:4326"
        assert result.features[0].geometry_type == "Polygon"
        assert result.features[1].geometry_type == "LineString"

    def test_bare_feature(self) -> None:
        """A bare Feature (not wrapped in FeatureCollection) is also valid."""
        from app.parsers.geojson import parse_geojson

        doc = {
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [77.59, 12.97],
            },
            "properties": {},
        }
        result = parse_geojson(json.dumps(doc).encode())
        assert len(result.features) == 1
        assert result.features[0].geometry_type == "Point"

    def test_empty_feature_collection_raises(self) -> None:
        from app.errors import UnprocessableEntityError
        from app.parsers.geojson import parse_geojson

        doc = {"type": "FeatureCollection", "features": []}
        with pytest.raises(UnprocessableEntityError, match="no features"):
            parse_geojson(json.dumps(doc).encode())

    def test_malformed_json_raises(self) -> None:
        from app.errors import UnprocessableEntityError
        from app.parsers.geojson import parse_geojson

        with pytest.raises(UnprocessableEntityError, match="Malformed JSON"):
            parse_geojson(b"{this is not valid json}")

    def test_wrong_type_raises(self) -> None:
        from app.errors import UnprocessableEntityError
        from app.parsers.geojson import parse_geojson

        doc = {"type": "GeometryCollection", "geometries": []}
        with pytest.raises(UnprocessableEntityError, match="Expected GeoJSON type"):
            parse_geojson(json.dumps(doc).encode())

    def test_null_geometry_skipped_with_warning(self) -> None:
        from app.parsers.geojson import parse_geojson

        doc = {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "geometry": None, "properties": {}},
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [77.0, 12.0]},
                    "properties": {},
                },
            ],
        }
        result = parse_geojson(json.dumps(doc).encode())
        assert len(result.features) == 1  # null geometry skipped
        assert len(result.warnings) == 1
        assert "null geometry" in result.warnings[0]

    def test_empty_bytes_raises(self) -> None:
        from app.errors import UnprocessableEntityError
        from app.parsers.geojson import parse_geojson

        with pytest.raises(UnprocessableEntityError, match="empty"):
            parse_geojson(b"")

    def test_legacy_crs_property_parsed(self) -> None:
        """Verify that the legacy GeoJSON 2008 'crs' property sets source_crs."""
        from app.parsers.geojson import parse_geojson

        doc = {
            "type": "FeatureCollection",
            "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::32643"}},
            "features": [
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [400000, 1400000]},
                    "properties": {},
                }
            ],
        }
        result = parse_geojson(json.dumps(doc).encode())
        assert result.source_crs == "EPSG:32643"

    def test_properties_preserved(self) -> None:
        from app.parsers.geojson import parse_geojson

        doc = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [77.0, 12.0]},
                    "properties": {"name": "Test", "value": 42, "active": True},
                }
            ],
        }
        result = parse_geojson(json.dumps(doc).encode())
        props = result.features[0].properties
        assert props["name"] == "Test"
        assert props["value"] == 42
        assert props["active"] is True


# ---------------------------------------------------------------------------
# GeoJSON Ingestion Pipeline Tests (through IngestService)
# ---------------------------------------------------------------------------


class TestGeoJSONIngestion:
    """End-to-end ingestion tests for GeoJSON files."""

    @pytest.fixture
    def ingest_service(self) -> object:
        from app.config import Settings
        from app.services.ingest import IngestService
        from app.storage.repository import Repository

        with tempfile.TemporaryDirectory() as d:
            settings = Settings(db_path=f"{d}/test.db", max_features=1000)
            repo = Repository(settings.db_path)
            yield IngestService(repo, settings), repo

    def test_geojson_ingestion_complete(self, ingest_service: tuple) -> None:
        svc, repo = ingest_service
        sample = json.dumps({
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [[77.59, 12.97], [77.595, 12.97], [77.595, 12.975], [77.59, 12.975], [77.59, 12.97]]
                        ],
                    },
                    "properties": {"name": "Farm"},
                }
            ],
        }).encode()

        from app.domain import FileStatus
        record = svc.ingest_stream(io.BytesIO(sample), "survey.geojson")
        assert record.status == FileStatus.COMPLETED
        assert record.feature_count == 1
        assert record.crs == "EPSG:4326"

        summary, feats, total = repo.get_measurements(record.id)
        assert total == 1
        assert summary.total_area_sq_m > 100_000  # Must be metric, not degrees


# ---------------------------------------------------------------------------
# Metrics Module Tests
# ---------------------------------------------------------------------------


class TestMetrics:
    """Tests for app.monitoring.metrics."""

    def test_prometheus_available(self) -> None:
        from app.monitoring.metrics import is_prometheus_available
        # prometheus_client is now in requirements
        assert is_prometheus_available() is True

    def test_get_metrics_output(self) -> None:
        from app.monitoring.metrics import get_metrics_output
        content, content_type = get_metrics_output()
        assert isinstance(content, bytes)
        assert "text/plain" in content_type

    def test_record_ingest_does_not_raise(self) -> None:
        from app.monitoring.metrics import record_ingest
        # Should run without raising even if called multiple times
        record_ingest("KML", "COMPLETED", 0.5, 1024, 5)
        record_ingest("GEOJSON", "FAILED", 0.1, 512, 0)

    def test_metrics_endpoint_returns_200(self, tmp_path: Path) -> None:
        """The /metrics endpoint must return 200 with Prometheus text format."""
        from app.config import Settings
        from app.main import create_app

        settings = Settings(db_path=str(tmp_path / "test.db"), max_features=100)
        app = create_app(settings)

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/metrics")
            assert resp.status_code == 200
            assert "text/plain" in resp.headers.get("content-type", "")


# ---------------------------------------------------------------------------
# Auth Module Tests
# ---------------------------------------------------------------------------


class TestAPIKeyAuth:
    """Tests for app.api.auth.require_api_key."""

    def test_open_access_when_no_key_configured(self, tmp_path: Path) -> None:
        """When GEO_API_KEY is empty, all routes are accessible without a key."""
        from app.config import Settings
        from app.main import create_app

        settings = Settings(db_path=str(tmp_path / "test.db"), api_key="")
        app = create_app(settings)

        with TestClient(app) as client:
            resp = client.get("/api/files/config/")
            assert resp.status_code == 200

    def test_requires_key_when_configured(self, tmp_path: Path) -> None:
        """When GEO_API_KEY is set, protected routes require the header."""
        from app.config import Settings
        from app.main import create_app

        settings = Settings(db_path=str(tmp_path / "test.db"), api_key="test-secret-key")
        app = create_app(settings)

        with TestClient(app, raise_server_exceptions=False) as client:
            # Without key → 401
            resp = client.get("/api/files/config/")
            assert resp.status_code == 401

            # With wrong key → 403
            resp = client.get("/api/files/config/", headers={"X-API-Key": "wrong-key"})
            assert resp.status_code == 403

            # With correct key → 200
            resp = client.get("/api/files/config/", headers={"X-API-Key": "test-secret-key"})
            assert resp.status_code == 200

    def test_health_always_public(self, tmp_path: Path) -> None:
        """The /health endpoint is always exempt from API key requirement."""
        from app.config import Settings
        from app.main import create_app

        settings = Settings(db_path=str(tmp_path / "test.db"), api_key="test-secret")
        app = create_app(settings)

        with TestClient(app) as client:
            resp = client.get("/health")
            assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Rate Limiting Module Tests
# ---------------------------------------------------------------------------


class TestRateLimiting:
    """Tests for app.api.auth rate limiting configuration."""

    def test_rate_limiting_available(self) -> None:
        from app.api.auth import is_rate_limiting_available
        assert is_rate_limiting_available() is True

    def test_limiter_configured(self) -> None:
        from app.api.auth import limiter
        assert limiter is not None


# ---------------------------------------------------------------------------
# Async Task Queue Tests
# ---------------------------------------------------------------------------


class TestAsyncEndpoints:
    """Tests for POST /api/async/ingest/ and GET /api/tasks/{id}/ endpoints."""

    def test_async_ingest_returns_202(self, tmp_path: Path) -> None:
        from app.config import Settings
        from app.main import create_app

        settings = Settings(db_path=str(tmp_path / "test.db"), max_features=1000)
        app = create_app(settings)

        sample_geojson = json.dumps({
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [77.59, 12.97]},
                    "properties": {},
                }
            ],
        }).encode()

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/api/async/ingest/",
                files={"file": ("survey.geojson", io.BytesIO(sample_geojson), "application/geo+json")},
            )
            assert resp.status_code == 202
            data = resp.json()
            assert "task_id" in data
            assert data["status"] in ("QUEUED", "PROCESSING", "COMPLETED")
            assert data["filename"] == "survey.geojson"

    def test_async_ingest_unsupported_format_returns_415(self, tmp_path: Path) -> None:
        from app.config import Settings
        from app.main import create_app

        settings = Settings(db_path=str(tmp_path / "test.db"))
        app = create_app(settings)

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/api/async/ingest/",
                files={"file": ("survey.csv", io.BytesIO(b"a,b,c"), "text/csv")},
            )
            assert resp.status_code == 415

    def test_task_not_found_returns_404(self, tmp_path: Path) -> None:
        from app.config import Settings
        from app.main import create_app

        settings = Settings(db_path=str(tmp_path / "test.db"))
        app = create_app(settings)

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/api/tasks/nonexistent-task-id/")
            assert resp.status_code == 404

    def test_list_tasks_returns_200(self, tmp_path: Path) -> None:
        from app.config import Settings
        from app.main import create_app

        settings = Settings(db_path=str(tmp_path / "test.db"))
        app = create_app(settings)

        with TestClient(app) as client:
            resp = client.get("/api/tasks/")
            assert resp.status_code == 200
            assert isinstance(resp.json(), list)


# ---------------------------------------------------------------------------
# GeoPackage Parser Tests
# ---------------------------------------------------------------------------


class TestGeoPackageParser:
    """Tests for app.parsers.geopackage.parse_geopackage."""

    def test_empty_content_raises(self) -> None:
        from app.errors import UnprocessableEntityError
        from app.parsers.geopackage import parse_geopackage

        with pytest.raises(UnprocessableEntityError, match="empty"):
            parse_geopackage(b"")

    def test_invalid_magic_raises(self) -> None:
        from app.errors import UnprocessableEntityError
        from app.parsers.geopackage import parse_geopackage

        with pytest.raises(UnprocessableEntityError, match="not appear to be a valid SQLite"):
            parse_geopackage(b"not a valid sqlite file contents")

    def test_valid_geopackage_parsing(self, tmp_path: Path) -> None:
        from app.parsers.geopackage import parse_geopackage

        db_path = tmp_path / "sample.gpkg"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE gpkg_contents (table_name TEXT, data_type TEXT, identifier TEXT);")
        conn.execute(
            "CREATE TABLE gpkg_geometry_columns "
            "(table_name TEXT, column_name TEXT, geometry_type_name TEXT, srs_id INTEGER, z INTEGER, m INTEGER);"
        )
        conn.execute("CREATE TABLE survey (id INTEGER PRIMARY KEY, geom BLOB, name TEXT);")
        conn.execute("INSERT INTO gpkg_contents VALUES ('survey', 'features', 'survey');")
        conn.execute("INSERT INTO gpkg_geometry_columns VALUES ('survey', 'geom', 'POINT', 4326, 0, 0);")

        # GPKG header: magic b'GP' + version 0 + flags 1 (little-endian, no envelope) + srid 4326
        hdr = b"GP" + bytes([0, 1]) + struct.pack("<i", 4326)
        # WKB Point(77.59, 12.97): endian 1 + geom_type 1 + x, y doubles
        wkb = bytes([1]) + struct.pack("<I", 1) + struct.pack("<dd", 77.59, 12.97)
        blob = hdr + wkb
        conn.execute("INSERT INTO survey VALUES (1, ?, ?)", (blob, "Waypoint 1"))
        conn.commit()
        conn.close()

        content = db_path.read_bytes()
        result = parse_geopackage(content)
        assert len(result.features) == 1
        assert result.features[0].geometry_type == "Point"
        assert result.features[0].properties["name"] == "Waypoint 1"
        assert result.source_crs == "EPSG:4326"
