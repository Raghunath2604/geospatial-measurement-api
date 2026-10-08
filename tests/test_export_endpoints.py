"""Tests for the export endpoints:
    GET /api/files/{id}/export/geojson/
    GET /api/files/{id}/export/csv/
    GET /api/files/{id}/export/kml/
"""
from __future__ import annotations

import io
import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def app_client(tmp_path):
    from app.config import Settings
    from app.main import create_app
    settings = Settings(db_path=str(tmp_path / "test.db"), max_features=1000)
    application = create_app(settings)
    with TestClient(application, raise_server_exceptions=False) as client:
        yield client


@pytest.fixture()
def uploaded_file_id(app_client):
    sample_geojson = json.dumps({
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [[[77.59, 12.97], [77.595, 12.97], [77.595, 12.975], [77.59, 12.975], [77.59, 12.97]]]}, "properties": {"name": "Test Farm", "crop": "Paddy"}},
            {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[77.59, 12.97], [77.60, 12.98]]}, "properties": {"name": "Test Road"}},
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [77.592, 12.972]}, "properties": {"name": "Survey Marker"}},
        ],
    }).encode()
    resp = app_client.post("/api/files/", files={"file": ("test_survey.geojson", io.BytesIO(sample_geojson), "application/geo+json")})
    assert resp.status_code == 201, f"Upload failed: {resp.text}"
    data = resp.json()
    assert data["status"] == "COMPLETED"
    return data["id"]


class TestGeoJSONExport:
    def test_export_geojson_returns_200(self, app_client, uploaded_file_id):
        resp = app_client.get(f"/api/files/{uploaded_file_id}/export/geojson/")
        assert resp.status_code == 200

    def test_export_geojson_content_type(self, app_client, uploaded_file_id):
        resp = app_client.get(f"/api/files/{uploaded_file_id}/export/geojson/")
        assert "geo+json" in resp.headers["content-type"]

    def test_export_geojson_content_disposition(self, app_client, uploaded_file_id):
        resp = app_client.get(f"/api/files/{uploaded_file_id}/export/geojson/")
        cd = resp.headers.get("content-disposition", "")
        assert "attachment" in cd
        assert "_measured.geojson" in cd

    def test_export_geojson_is_valid_feature_collection(self, app_client, uploaded_file_id):
        resp = app_client.get(f"/api/files/{uploaded_file_id}/export/geojson/")
        doc = resp.json()
        assert doc["type"] == "FeatureCollection"
        assert len(doc["features"]) == 3

    def test_export_geojson_measurement_props_injected(self, app_client, uploaded_file_id):
        resp = app_client.get(f"/api/files/{uploaded_file_id}/export/geojson/")
        doc = resp.json()
        polygon_feat = next(f for f in doc["features"] if f["geometry"]["type"] == "Polygon")
        assert "_measurement_status" in polygon_feat["properties"]
        assert polygon_feat["properties"]["_area_sq_m"] > 100_000

    def test_export_geojson_preserves_source_properties(self, app_client, uploaded_file_id):
        resp = app_client.get(f"/api/files/{uploaded_file_id}/export/geojson/")
        doc = resp.json()
        polygon_feat = next(f for f in doc["features"] if f["geometry"]["type"] == "Polygon")
        assert polygon_feat["properties"].get("name") == "Test Farm"

    def test_export_geojson_404_on_unknown_file(self, app_client):
        resp = app_client.get("/api/files/nonexistent-file-id/export/geojson/")
        assert resp.status_code == 404


class TestCSVExport:
    def test_export_csv_returns_200(self, app_client, uploaded_file_id):
        resp = app_client.get(f"/api/files/{uploaded_file_id}/export/csv/")
        assert resp.status_code == 200

    def test_export_csv_content_type(self, app_client, uploaded_file_id):
        resp = app_client.get(f"/api/files/{uploaded_file_id}/export/csv/")
        assert "text/csv" in resp.headers["content-type"]

    def test_export_csv_has_header_row(self, app_client, uploaded_file_id):
        resp = app_client.get(f"/api/files/{uploaded_file_id}/export/csv/")
        lines = resp.text.strip().splitlines()
        assert lines[0] == "feature_index,geometry_type,status,area_sq_m,length_m,measurement_crs,notice"

    def test_export_csv_has_correct_row_count(self, app_client, uploaded_file_id):
        resp = app_client.get(f"/api/files/{uploaded_file_id}/export/csv/")
        lines = resp.text.strip().splitlines()
        assert len(lines) == 4  # 1 header + 3 feature rows

    def test_export_csv_polygon_has_area(self, app_client, uploaded_file_id):
        resp = app_client.get(f"/api/files/{uploaded_file_id}/export/csv/")
        lines = resp.text.strip().splitlines()
        poly_row = next((line_item for line_item in lines[1:] if "Polygon" in line_item), None)
        assert poly_row is not None
        cols = poly_row.split(",")
        area = float(cols[3])
        assert area > 100_000

    def test_export_csv_404_on_unknown_file(self, app_client):
        resp = app_client.get("/api/files/nonexistent-id/export/csv/")
        assert resp.status_code == 404

    def test_export_csv_content_disposition_attachment(self, app_client, uploaded_file_id):
        resp = app_client.get(f"/api/files/{uploaded_file_id}/export/csv/")
        cd = resp.headers.get("content-disposition", "")
        assert "attachment" in cd
        assert "_measurements.csv" in cd


class TestKMLExport:
    def test_export_kml_returns_200(self, app_client, uploaded_file_id):
        resp = app_client.get(f"/api/files/{uploaded_file_id}/export/kml/")
        assert resp.status_code == 200

    def test_export_kml_content_type(self, app_client, uploaded_file_id):
        resp = app_client.get(f"/api/files/{uploaded_file_id}/export/kml/")
        assert "kml" in resp.headers["content-type"]

    def test_export_kml_content_disposition(self, app_client, uploaded_file_id):
        resp = app_client.get(f"/api/files/{uploaded_file_id}/export/kml/")
        cd = resp.headers.get("content-disposition", "")
        assert "attachment" in cd
        assert "_measured.kml" in cd

    def test_export_kml_is_valid_xml(self, app_client, uploaded_file_id):
        import xml.etree.ElementTree as ET
        resp = app_client.get(f"/api/files/{uploaded_file_id}/export/kml/")
        root = ET.fromstring(resp.text)
        assert root.tag.endswith("kml")

    def test_export_kml_has_document_element(self, app_client, uploaded_file_id):
        import xml.etree.ElementTree as ET
        resp = app_client.get(f"/api/files/{uploaded_file_id}/export/kml/")
        root = ET.fromstring(resp.text)
        ns = "http://www.opengis.net/kml/2.2"
        doc = root.find(f"{{{ns}}}Document")
        assert doc is not None

    def test_export_kml_has_placemarks(self, app_client, uploaded_file_id):
        import xml.etree.ElementTree as ET
        resp = app_client.get(f"/api/files/{uploaded_file_id}/export/kml/")
        root = ET.fromstring(resp.text)
        ns = "http://www.opengis.net/kml/2.2"
        placemarks = root.findall(f".//{{{ns}}}Placemark")
        assert len(placemarks) == 3

    def test_export_kml_polygon_placemark_has_geometry(self, app_client, uploaded_file_id):
        import xml.etree.ElementTree as ET
        resp = app_client.get(f"/api/files/{uploaded_file_id}/export/kml/")
        root = ET.fromstring(resp.text)
        ns = "http://www.opengis.net/kml/2.2"
        polygons = root.findall(f".//{{{ns}}}Polygon")
        assert len(polygons) >= 1

    def test_export_kml_linestring_placemark_has_geometry(self, app_client, uploaded_file_id):
        import xml.etree.ElementTree as ET
        resp = app_client.get(f"/api/files/{uploaded_file_id}/export/kml/")
        root = ET.fromstring(resp.text)
        ns = "http://www.opengis.net/kml/2.2"
        linestrings = root.findall(f".//{{{ns}}}LineString")
        assert len(linestrings) >= 1

    def test_export_kml_point_placemark_has_geometry(self, app_client, uploaded_file_id):
        import xml.etree.ElementTree as ET
        resp = app_client.get(f"/api/files/{uploaded_file_id}/export/kml/")
        root = ET.fromstring(resp.text)
        ns = "http://www.opengis.net/kml/2.2"
        points = root.findall(f".//{{{ns}}}Point")
        assert len(points) >= 1

    def test_export_kml_extended_data_contains_measurement(self, app_client, uploaded_file_id):
        import xml.etree.ElementTree as ET
        resp = app_client.get(f"/api/files/{uploaded_file_id}/export/kml/")
        root = ET.fromstring(resp.text)
        ns = "http://www.opengis.net/kml/2.2"
        ext_datas = root.findall(f".//{{{ns}}}ExtendedData")
        assert len(ext_datas) > 0
        found_status = any(
            d.get("name") == "measurement_status"
            for ed in ext_datas
            for d in ed.findall(f"{{{ns}}}Data")
        )
        assert found_status

    def test_export_kml_preserves_feature_name(self, app_client, uploaded_file_id):
        import xml.etree.ElementTree as ET
        resp = app_client.get(f"/api/files/{uploaded_file_id}/export/kml/")
        root = ET.fromstring(resp.text)
        ns = "http://www.opengis.net/kml/2.2"
        names = [el.text for el in root.findall(f".//{{{ns}}}Placemark/{{{ns}}}name")]
        assert "Test Farm" in names

    def test_export_kml_404_on_unknown_file(self, app_client):
        resp = app_client.get("/api/files/nonexistent-id/export/kml/")
        assert resp.status_code == 404

    def test_export_kml_xml_declaration_present(self, app_client, uploaded_file_id):
        resp = app_client.get(f"/api/files/{uploaded_file_id}/export/kml/")
        assert resp.text.startswith("<?xml version")


class TestExportDataIntegrity:
    def test_geojson_and_csv_row_counts_match(self, app_client, uploaded_file_id):
        geojson_resp = app_client.get(f"/api/files/{uploaded_file_id}/export/geojson/")
        csv_resp = app_client.get(f"/api/files/{uploaded_file_id}/export/csv/")
        geojson_count = len(geojson_resp.json()["features"])
        csv_count = len(csv_resp.text.strip().splitlines()) - 1
        assert geojson_count == csv_count

    def test_kml_and_geojson_placemark_counts_match(self, app_client, uploaded_file_id):
        import xml.etree.ElementTree as ET
        geojson_resp = app_client.get(f"/api/files/{uploaded_file_id}/export/geojson/")
        kml_resp = app_client.get(f"/api/files/{uploaded_file_id}/export/kml/")
        geojson_count = len(geojson_resp.json()["features"])
        root = ET.fromstring(kml_resp.text)
        ns = "http://www.opengis.net/kml/2.2"
        kml_count = len(root.findall(f".//{{{ns}}}Placemark"))
        assert geojson_count == kml_count

    def test_polygon_area_consistent_across_geojson_and_csv(self, app_client, uploaded_file_id):
        geojson_resp = app_client.get(f"/api/files/{uploaded_file_id}/export/geojson/")
        csv_resp = app_client.get(f"/api/files/{uploaded_file_id}/export/csv/")
        doc = geojson_resp.json()
        poly_feat = next(f for f in doc["features"] if f["geometry"]["type"] == "Polygon")
        geojson_area = poly_feat["properties"]["_area_sq_m"]
        csv_lines = csv_resp.text.strip().splitlines()
        poly_csv_row = next(line_item for line_item in csv_lines[1:] if "Polygon" in line_item)
        csv_area = float(poly_csv_row.split(",")[3])
        assert abs(geojson_area - csv_area) < 0.01
