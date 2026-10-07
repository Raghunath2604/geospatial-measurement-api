"""Independent mathematical oracle tests comparing planar UTM measurements against pyproj.Geod.

Verifies mathematical correctness across diverse global coordinate zones.
Includes an explicit guard test preventing erroneous degree-based measurements.
"""

from __future__ import annotations

import pyproj
import pytest
import shapely.geometry
from pyproj import Geod

from app.domain import MeasurementStatus
from app.errors import UnprocessableEntityError
from app.services.measurement import MeasurementService, resolve_crs

# WGS84 Geodetic calculator
GEOD = Geod(ellps="WGS84")

# Tolerance rationale:
# UTM projections have a scale factor k0 = 0.9996 at the central meridian and
# increase to ~1.0010 at the 3-degree zone edge. Area scales approximately as k^2,
# meaning planar UTM area diverges by at most 0.2% - 0.4% from geodesic ellipsoidal area.
# We set a tight 0.005 (0.5%) relative tolerance for features inside their zone.
TOLERANCE_REL = 0.005


def _geodesic_area(geom: shapely.geometry.base.BaseGeometry) -> float:
    """Calculate absolute geodesic ellipsoidal area in square metres using pyproj.Geod.

    Note: pyproj.Geod.geometry_area_perimeter adds or subtracts interior rings
    depending on coordinate winding order. To ensure independent oracle correctness,
    we explicitly compute exterior area minus interior hole areas.
    """
    if isinstance(geom, shapely.geometry.Polygon):
        ext_poly = shapely.geometry.Polygon(geom.exterior)
        ext_area, _ = GEOD.geometry_area_perimeter(ext_poly)
        hole_areas = 0.0
        for interior in geom.interiors:
            hole_poly = shapely.geometry.Polygon(interior)
            h_area, _ = GEOD.geometry_area_perimeter(hole_poly)
            hole_areas += abs(h_area)
        return abs(ext_area) - hole_areas
    elif isinstance(geom, shapely.geometry.MultiPolygon):
        return sum(_geodesic_area(p) for p in geom.geoms)

    area, _ = GEOD.geometry_area_perimeter(geom)
    return abs(area)


def _geodesic_length(geom: shapely.geometry.base.BaseGeometry) -> float:
    """Calculate geodesic ellipsoidal length in metres using pyproj.Geod."""
    return abs(GEOD.geometry_length(geom))


def test_bengaluru_utm_vs_geodesic_oracle() -> None:
    """Verify agricultural field in Bengaluru (Zone 43N, near equator)."""
    coords = [
        [77.5900, 12.9700],
        [77.5950, 12.9700],
        [77.5950, 12.9750],
        [77.5900, 12.9750],
        [77.5900, 12.9700],
    ]
    poly_geom = {"type": "Polygon", "coordinates": [coords]}
    shapely_poly = shapely.geometry.Polygon(coords)

    service = MeasurementService("EPSG:4326")
    meas = service.measure(feature_index=0, geometry=poly_geom)

    assert meas.status == MeasurementStatus.MEASURED
    assert meas.area_sq_m is not None
    assert meas.measurement_crs == "EPSG:32643"

    geodesic_area = _geodesic_area(shapely_poly)
    rel_error = abs(meas.area_sq_m - geodesic_area) / geodesic_area
    assert rel_error < TOLERANCE_REL, (
        f"UTM area {meas.area_sq_m} divergent from geodesic {geodesic_area} (rel: {rel_error})"
    )


def test_sydney_utm_vs_geodesic_oracle() -> None:
    """Verify park polygon in Sydney, Australia (Zone 56S, southern hemisphere)."""
    coords = [
        [151.2000, -33.8600],
        [151.2060, -33.8600],
        [151.2060, -33.8650],
        [151.2000, -33.8650],
        [151.2000, -33.8600],
    ]
    poly_geom = {"type": "Polygon", "coordinates": [coords]}
    shapely_poly = shapely.geometry.Polygon(coords)

    service = MeasurementService("EPSG:4326")
    meas = service.measure(feature_index=0, geometry=poly_geom)

    assert meas.status == MeasurementStatus.MEASURED
    assert meas.area_sq_m is not None
    assert meas.measurement_crs == "EPSG:32756"

    geodesic_area = _geodesic_area(shapely_poly)
    rel_error = abs(meas.area_sq_m - geodesic_area) / geodesic_area
    assert rel_error < TOLERANCE_REL


def test_london_utm_vs_geodesic_oracle() -> None:
    """Verify urban plot in London, UK (Zone 30N, high mid-latitude)."""
    coords = [
        [-0.1200, 51.5000],
        [-0.1150, 51.5000],
        [-0.1150, 51.5040],
        [-0.1200, 51.5040],
        [-0.1200, 51.5000],
    ]
    poly_geom = {"type": "Polygon", "coordinates": [coords]}
    shapely_poly = shapely.geometry.Polygon(coords)

    service = MeasurementService("EPSG:4326")
    meas = service.measure(feature_index=0, geometry=poly_geom)

    assert meas.status == MeasurementStatus.MEASURED
    assert meas.area_sq_m is not None
    assert meas.measurement_crs == "EPSG:32630"

    geodesic_area = _geodesic_area(shapely_poly)
    rel_error = abs(meas.area_sq_m - geodesic_area) / geodesic_area
    assert rel_error < TOLERANCE_REL


def test_polygon_with_interior_hole_oracle() -> None:
    """Verify that interior hole area is subtracted correctly rather than added."""
    exterior = [
        [77.6000, 12.9800],
        [77.6060, 12.9800],
        [77.6060, 12.9860],
        [77.6000, 12.9860],
        [77.6000, 12.9800],
    ]
    hole = [
        [77.6020, 12.9820],
        [77.6040, 12.9820],
        [77.6040, 12.9840],
        [77.6020, 12.9840],
        [77.6020, 12.9820],
    ]
    poly_geom = {"type": "Polygon", "coordinates": [exterior, hole]}
    shapely_poly = shapely.geometry.Polygon(exterior, [hole])

    service = MeasurementService("EPSG:4326")
    meas = service.measure(feature_index=0, geometry=poly_geom)

    assert meas.status == MeasurementStatus.MEASURED
    assert meas.area_sq_m is not None

    geodesic_area = _geodesic_area(shapely_poly)
    rel_error = abs(meas.area_sq_m - geodesic_area) / geodesic_area
    assert rel_error < TOLERANCE_REL


def test_multipolygon_oracle() -> None:
    """Verify MultiPolygon area matches sum of parts."""
    poly1 = [
        [77.5900, 12.9700],
        [77.5920, 12.9700],
        [77.5920, 12.9720],
        [77.5900, 12.9720],
        [77.5900, 12.9700],
    ]
    poly2 = [
        [77.5950, 12.9750],
        [77.5970, 12.9750],
        [77.5970, 12.9770],
        [77.5950, 12.9770],
        [77.5950, 12.9750],
    ]
    mp_geom = {"type": "MultiPolygon", "coordinates": [[poly1], [poly2]]}
    shapely_mp = shapely.geometry.MultiPolygon(
        [shapely.geometry.Polygon(poly1), shapely.geometry.Polygon(poly2)]
    )

    service = MeasurementService("EPSG:4326")
    meas = service.measure(feature_index=0, geometry=mp_geom)

    assert meas.status == MeasurementStatus.MEASURED
    assert meas.area_sq_m is not None

    geodesic_area = _geodesic_area(shapely_mp)
    rel_error = abs(meas.area_sq_m - geodesic_area) / geodesic_area
    assert rel_error < TOLERANCE_REL


def test_projected_input_epsg_32643() -> None:
    """Verify that a source file already in UTM EPSG:32643 is handled accurately."""
    # Create a 200m x 200m square in UTM 43N coordinates
    # Expected area: 40,000 sq m
    coords = [
        [780000.0, 1435000.0],
        [780200.0, 1435000.0],
        [780200.0, 1435200.0],
        [780000.0, 1435200.0],
        [780000.0, 1435000.0],
    ]
    poly_geom = {"type": "Polygon", "coordinates": [coords]}

    service = MeasurementService("EPSG:32643")
    meas = service.measure(feature_index=0, geometry=poly_geom)

    assert meas.status == MeasurementStatus.MEASURED
    assert meas.area_sq_m is not None
    assert abs(meas.area_sq_m - 40000.0) < 1.0


def test_projected_input_web_mercator_reprojected() -> None:
    """Verify that Web Mercator (EPSG:3857) is NOT measured in Mercator units (which has severe scale distortion)."""
    # Point near 60 degrees North: Web Mercator area is distorted by 1/cos^2(60) = 4x
    # Real area on ground should be measured in UTM 32V (EPSG:32632), NOT Mercator!
    to_3857 = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    wgs_coords = [
        [10.0, 60.0],
        [10.01, 60.0],
        [10.01, 60.005],
        [10.0, 60.005],
        [10.0, 60.0],
    ]
    merc_coords = [list(to_3857.transform(lon, lat)) for lon, lat in wgs_coords]
    poly_geom = {"type": "Polygon", "coordinates": [merc_coords]}

    service = MeasurementService("EPSG:3857")
    meas = service.measure(feature_index=0, geometry=poly_geom)

    assert meas.status == MeasurementStatus.MEASURED
    assert meas.area_sq_m is not None
    # Must have picked local UTM zone 32N (EPSG:32632)
    assert meas.measurement_crs == "EPSG:32632"

    # Compare with geodesic area
    geodesic_area = _geodesic_area(shapely.geometry.Polygon(wgs_coords))
    rel_error = abs(meas.area_sq_m - geodesic_area) / geodesic_area
    assert rel_error < TOLERANCE_REL


def test_linestring_length_oracle() -> None:
    """Verify LineString metric distance matches geodesic distance within tolerance."""
    coords = [
        [77.5900, 12.9700],
        [77.5950, 12.9750],
        [77.6000, 12.9700],
    ]
    line_geom = {"type": "LineString", "coordinates": coords}
    shapely_line = shapely.geometry.LineString(coords)

    service = MeasurementService("EPSG:4326")
    meas = service.measure(feature_index=0, geometry=line_geom)

    assert meas.status == MeasurementStatus.MEASURED
    assert meas.length_m is not None

    geodesic_len = _geodesic_length(shapely_line)
    rel_error = abs(meas.length_m - geodesic_len) / geodesic_len
    assert rel_error < TOLERANCE_REL


def test_degree_measurement_guard() -> None:
    """GUARD TEST: Fails emphatically if anyone computes area in angular degrees squared.

    A ~0.2 km^2 (approx 450m x 450m) field must have an area > 100,000 m^2.
    If evaluated directly on degree coordinates, the result is ~2e-5 (0.00002).
    """
    coords = [
        [77.5900, 12.9700],
        [77.5945, 12.9700],
        [77.5945, 12.9745],
        [77.5900, 12.9745],
        [77.5900, 12.9700],
    ]
    poly_geom = {"type": "Polygon", "coordinates": [coords]}

    service = MeasurementService("EPSG:4326")
    meas = service.measure(feature_index=0, geometry=poly_geom)

    assert meas.status == MeasurementStatus.MEASURED
    assert meas.area_sq_m is not None
    # Must be > 100,000 sq metres; never ~2e-5
    assert meas.area_sq_m > 100_000.0, (
        f"CRITICAL: Area is {meas.area_sq_m}; looks like calculation was run on degrees!"
    )


def test_bowtie_invalid_polygon_flagged() -> None:
    """Invalid self-intersecting polygon must be flagged in message without silent repair."""
    bowtie_coords = [
        [0.0, 0.0],
        [1.0, 1.0],
        [1.0, 0.0],
        [0.0, 1.0],
        [0.0, 0.0],
    ]
    bowtie_geom = {"type": "Polygon", "coordinates": [bowtie_coords]}

    service = MeasurementService("EPSG:4326")
    meas = service.measure(feature_index=0, geometry=bowtie_geom)

    assert meas.status == MeasurementStatus.MEASURED
    assert meas.message is not None
    assert "invalid" in meas.message.lower()
    assert "without silent repair" in meas.message.lower()


def test_points_not_applicable() -> None:
    """Point features must receive NOT_APPLICABLE status with no area or length."""
    service = MeasurementService("EPSG:4326")

    point_meas = service.measure(
        feature_index=0, geometry={"type": "Point", "coordinates": [10.0, 10.0]}
    )
    assert point_meas.status == MeasurementStatus.NOT_APPLICABLE
    assert point_meas.area_sq_m is None
    assert point_meas.length_m is None

    mp_meas = service.measure(
        feature_index=1,
        geometry={"type": "MultiPoint", "coordinates": [[10.0, 10.0], [10.1, 10.1]]},
    )
    assert mp_meas.status == MeasurementStatus.NOT_APPLICABLE


def test_reject_unsupported_crs_types() -> None:
    """Reject non-geographic and non-projected CRS definitions (e.g. Geocentric) with 422."""
    # EPSG:4978 is WGS 84 Geocentric (3D Cartesian X,Y,Z)
    with pytest.raises(
        UnprocessableEntityError, match="must be geographic or projected"
    ):
        resolve_crs("EPSG:4978")
