"""Unit tests for domain models and projection arithmetic."""

from __future__ import annotations

from app.domain import FileStatus, MeasurementStatus, ParsedFeature
from app.services.projection import (
    is_geographic_bounds,
    normalize_longitude,
    utm_epsg_for,
)


def test_normalize_longitude() -> None:
    """Validate longitude normalization into [-180, 180)."""
    assert normalize_longitude(0.0) == 0.0
    assert normalize_longitude(77.59) == 77.59
    assert normalize_longitude(-122.4) == -122.4
    assert normalize_longitude(180.0) == -180.0
    assert normalize_longitude(-180.0) == -180.0
    assert normalize_longitude(190.0) == -170.0
    assert normalize_longitude(-190.0) == 170.0
    assert normalize_longitude(540.0) == -180.0
    assert normalize_longitude(-540.0) == -180.0


def test_utm_epsg_for_zones() -> None:
    """Verify UTM zone code selection for global coordinates."""
    # Bengaluru, India (~77.59 E, 12.97 N) -> Zone 43N -> EPSG:32643
    assert utm_epsg_for(12.97, 77.59) == 32643

    # Sydney, Australia (~151.2 E, -33.86 S) -> Zone 56S -> EPSG:32756
    assert utm_epsg_for(-33.86, 151.20) == 32756

    # London, UK (~-0.12 W, 51.5 N) -> Zone 30N -> EPSG:32630
    assert utm_epsg_for(51.50, -0.12) == 32630

    # San Francisco, USA (~-122.4 W, 37.77 N) -> Zone 10N -> EPSG:32610
    assert utm_epsg_for(37.77, -122.41) == 32610

    # Equator boundary test
    assert utm_epsg_for(0.0, 77.59) == 32643  # Northern hemisphere at 0 deg


def test_utm_epsg_for_polar_regions() -> None:
    """Verify Universal Polar Stereographic (UPS) selection beyond UTM limits."""
    # North Pole region (> 84 N) -> EPSG:32661
    assert utm_epsg_for(85.0, 0.0) == 32661
    assert utm_epsg_for(89.5, 77.0) == 32661

    # South Pole region (< -80 S) -> EPSG:32761
    assert utm_epsg_for(-81.0, 0.0) == 32761
    assert utm_epsg_for(-89.0, -120.0) == 32761


def test_is_geographic_bounds() -> None:
    """Verify bounding box checks against WGS84 degree ranges."""
    assert is_geographic_bounds(-180.0, -90.0, 180.0, 90.0) is True
    assert is_geographic_bounds(77.0, 12.0, 78.0, 13.0) is True

    # Projected coordinates exceeding limits
    assert is_geographic_bounds(500000.0, 1400000.0, 501000.0, 1401000.0) is False
    assert is_geographic_bounds(-200.0, 10.0, 10.0, 20.0) is False
    assert is_geographic_bounds(10.0, -95.0, 20.0, 20.0) is False


def test_domain_dataclasses() -> None:
    """Verify immutability and attributes of domain entities."""
    feat = ParsedFeature(
        index=0,
        geometry_type="Point",
        geometry={"type": "Point", "coordinates": [0.0, 0.0]},
        properties={"key": "val"},
    )
    assert feat.index == 0
    assert feat.geometry_type == "Point"
    assert feat.properties["key"] == "val"
    assert FileStatus.COMPLETED.value == "COMPLETED"
    assert MeasurementStatus.MEASURED.value == "MEASURED"
