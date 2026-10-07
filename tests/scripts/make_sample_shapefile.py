"""Script to generate a sample Shapefile ZIP archive for testing and demonstration."""

from __future__ import annotations

import datetime
import os
import tempfile
import zipfile

import shapefile

WGS84_WKT = (
    'GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],'
    'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]]'
)


def create_sample_shapefile_zip(output_zip_path: str) -> None:
    """Generate a sample Shapefile ZIP containing polygons, lines, points, and attributes."""
    os.makedirs(os.path.dirname(os.path.abspath(output_zip_path)), exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp_dir:
        base_name = "survey_parcels"
        shp_base = os.path.join(tmp_dir, base_name)

        # 1. Create Shapefile writer
        # We write Polygon shapes (type 5)
        w = shapefile.Writer(shp_base, shapeType=shapefile.POLYGON)
        w.field("PLOT_ID", "C", size=20)
        w.field("SURVEYOR", "C", size=30)
        w.field("ACREAGE", "N", decimal=2)
        w.field("SURVEY_DT", "D")

        # Feature 0: Simple agricultural plot polygon (clockwise exterior)
        poly1 = [
            [77.5900, 12.9700],
            [77.5900, 12.9750],
            [77.5950, 12.9750],
            [77.5950, 12.9700],
            [77.5900, 12.9700],
        ]
        w.poly([poly1])
        w.record(
            PLOT_ID="PARCEL-001",
            SURVEYOR="AeroSurvey Inc",
            ACREAGE=74.13,
            SURVEY_DT=datetime.date(2026, 3, 15),
        )

        # Feature 1: Plot with interior cutout (hole)
        # In Shapefile convention, clockwise outer ring, counter-clockwise inner ring
        exterior = [
            [77.6000, 12.9800],
            [77.6000, 12.9850],
            [77.6050, 12.9850],
            [77.6050, 12.9800],
            [77.6000, 12.9800],
        ]
        hole = [
            [77.6010, 12.9810],
            [77.6040, 12.9810],
            [77.6040, 12.9840],
            [77.6010, 12.9840],
            [77.6010, 12.9810],
        ]
        w.poly([exterior, hole])
        w.record(
            PLOT_ID="PARCEL-002",
            SURVEYOR="AeroSurvey Inc",
            ACREAGE=45.20,
            SURVEY_DT=datetime.date(2026, 3, 16),
        )

        # Feature 2: Triangular conservation reserve (clockwise exterior)
        poly3 = [
            [77.5800, 12.9600],
            [77.5825, 12.9650],
            [77.5850, 12.9600],
            [77.5800, 12.9600],
        ]
        w.poly([poly3])
        w.record(
            PLOT_ID="PARCEL-003",
            SURVEYOR="Dept Forestry",
            ACREAGE=18.55,
            SURVEY_DT=datetime.date(2026, 3, 17),
        )

        w.close()

        # 2. Write companion .prj file
        with open(f"{shp_base}.prj", "w", encoding="utf-8") as f:
            f.write(WGS84_WKT)

        # 3. Create .cpg file for encoding
        with open(f"{shp_base}.cpg", "w", encoding="utf-8") as f:
            f.write("UTF-8")

        # 4. Pack into ZIP archive
        with zipfile.ZipFile(output_zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for ext in [".shp", ".dbf", ".shx", ".prj", ".cpg"]:
                src = f"{shp_base}{ext}"
                if os.path.exists(src):
                    z.write(src, arcname=f"{base_name}{ext}")


if __name__ == "__main__":
    out_file = os.path.join(
        os.path.dirname(__file__), "..", "sample_data", "survey_shapefile.zip"
    )
    create_sample_shapefile_zip(out_file)
    print(f"Generated sample shapefile zip at: {os.path.abspath(out_file)}")
