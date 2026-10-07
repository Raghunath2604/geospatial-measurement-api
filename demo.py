"""End-to-end demonstration script.

Validates that the server is running, uploads sample files, retrieves measurements,
and opens the visual web dashboard in the default browser.
"""

from __future__ import annotations

import os
import sys
import time
import webbrowser
import httpx

BASE_URL = "http://127.0.0.1:8000"
KML_PATH = os.path.join(os.path.dirname(__file__), "tests", "sample_data", "survey.kml")
SHP_PATH = os.path.join(
    os.path.dirname(__file__), "tests", "sample_data", "survey_shapefile.zip"
)


def run_demo() -> None:
    print("=" * 70)
    print("  GEOSPATIAL FILE MEASUREMENT API — END-TO-END DEMO")
    print("=" * 70)

    # 1. Check health
    print(f"\n[1/4] Checking server health at {BASE_URL}/health ...")
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(f"{BASE_URL}/health")
            if resp.status_code != 200:
                print(f"Server returned status {resp.status_code}: {resp.text}")
                sys.exit(1)
            print(f"      Status: HEALTHY -> {resp.json()}")
    except Exception as exc:
        print(f"Error: Could not connect to {BASE_URL} ({exc}).")
        print("Please start the server first with: python run.py")
        sys.exit(1)

    # 2. Upload KML
    print(f"\n[2/4] Uploading KML survey: {KML_PATH} ...")
    with open(KML_PATH, "rb") as f, httpx.Client(timeout=30.0) as client:
        files = {"file": ("survey.kml", f, "application/vnd.google-earth.kml+xml")}
        resp = client.post(f"{BASE_URL}/api/files/", files=files)
        if resp.status_code != 201:
            print(f"Upload failed: {resp.status_code} {resp.text}")
            sys.exit(1)
        kml_info = resp.json()
        kml_id = kml_info["id"]
        print(f"      Ingested file_id: {kml_id}")
        print(
            f"      Status: {kml_info['status']} | Features: {kml_info['feature_count']} | CRS: {kml_info['crs']}"
        )

        # Get KML measurements
        meas_resp = client.get(f"{BASE_URL}/api/files/{kml_id}/measurements/")
        kml_meas = meas_resp.json()
        summary = kml_meas["summary"]
        print(f"      Summary: {summary['measured_count']} measured, {summary['not_applicable_count']} N/A")
        print(f"      Total Area:   {summary['total_area_sq_m']:,.2f} sq metres")
        print(f"      Total Length: {summary['total_length_m']:,.2f} metres")

    # 3. Upload Shapefile ZIP
    print(f"\n[3/4] Uploading Shapefile archive: {SHP_PATH} ...")
    with open(SHP_PATH, "rb") as f, httpx.Client(timeout=30.0) as client:
        files = {"file": ("parcels.zip", f, "application/zip")}
        resp = client.post(f"{BASE_URL}/api/files/", files=files)
        if resp.status_code != 201:
            print(f"Upload failed: {resp.status_code} {resp.text}")
            sys.exit(1)
        shp_info = resp.json()
        shp_id = shp_info["id"]
        print(f"      Ingested file_id: {shp_id}")
        print(
            f"      Status: {shp_info['status']} | Features: {shp_info['feature_count']} | CRS: {shp_info['crs']}"
        )

        meas_resp = client.get(f"{BASE_URL}/api/files/{shp_id}/measurements/")
        shp_meas = meas_resp.json()
        summary = shp_meas["summary"]
        print(f"      Summary: {summary['measured_count']} measured")
        print(f"      Total Area:   {summary['total_area_sq_m']:,.2f} sq metres")

    # 4. Success summary
    print("\n" + "=" * 70)
    print("  ALL SERVICES COMPLETELY OPERATIONAL AND VERIFIED!")
    print("=" * 70)
    print(f"\n- Visual Map Dashboard:  {BASE_URL}/")
    print(f"- Interactive Docs:      {BASE_URL}/docs")
    print(f"- ReDoc Documentation:   {BASE_URL}/redoc\n")

    try:
        webbrowser.open(f"{BASE_URL}/")
    except Exception:
        pass


if __name__ == "__main__":
    run_demo()
