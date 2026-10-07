"""Pure arithmetic projection utilities for metric CRS selection.

Provides zone calculation without external geospatial dependencies.
Determines UTM (EPSG:326xx / 327xx) or Universal Polar Stereographic (UPS)
projections based on feature centroid coordinates.
"""

from __future__ import annotations


def normalize_longitude(lon: float) -> float:
    """Normalize longitude to [-180.0, 180.0).

    Examples:
        normalize_longitude(190.0) -> -170.0
        normalize_longitude(-190.0) -> 170.0
        normalize_longitude(180.0) -> -180.0
    """
    if -180.0 <= lon < 180.0:
        return lon
    if lon == 180.0:
        return -180.0
    normalized = (lon + 180.0) % 360.0 - 180.0
    if normalized == 180.0 or normalized == -180.0:
        return -180.0
    return round(normalized, 10)


def utm_epsg_for(lat: float, lon: float) -> int:
    """Determine the optimal metric EPSG code for a given latitude and longitude.

    Rules:
    - Latitude > 84.0 deg: Universal Polar Stereographic (UPS) North -> EPSG:32661
    - Latitude < -80.0 deg: Universal Polar Stereographic (UPS) South -> EPSG:32761
    - Northern Hemisphere (lat >= 0.0): EPSG:32601 - 32660 (32600 + zone)
    - Southern Hemisphere (lat < 0.0): EPSG:32701 - 32760 (32700 + zone)
    where zone = int((norm_lon + 180.0) // 6) + 1 (clamped to 1..60).
    """
    if lat > 84.0:
        return 32661
    if lat < -80.0:
        return 32761

    norm_lon = normalize_longitude(lon)
    # Zone 1 covers [-180, -174), Zone 60 covers [174, 180)
    zone = int((norm_lon + 180.0) // 6) + 1
    zone = max(1, min(60, zone))

    if lat >= 0.0:
        return 32600 + zone
    return 32700 + zone


def is_geographic_bounds(
    min_x: float, min_y: float, max_x: float, max_y: float
) -> bool:
    """Return True if the bounding box values fall within valid WGS84 degree limits.

    Valid ranges:
    - X (longitude): [-180.0, 180.0]
    - Y (latitude): [-90.0, 90.0]
    """
    return (
        -180.0 <= min_x <= 180.0
        and -180.0 <= max_x <= 180.0
        and -90.0 <= min_y <= 90.0
        and -90.0 <= max_y <= 90.0
    )
