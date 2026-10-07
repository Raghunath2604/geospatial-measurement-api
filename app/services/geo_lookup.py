"""Real-time geographic lookup services: Reverse Geocoding and Terrain Elevation.

Provides real-time administrative address lookup and terrain elevation queries
with local caching, strict timeouts, and graceful fallbacks.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

import httpx

logger = logging.getLogger("geomeasure.lookup")

HEADERS = {
    "User-Agent": "GeoMeasureStudio/1.0 (Autonomous Geospatial Platform; contact@geomeasure.org)"
}


@lru_cache(maxsize=1024)
def _cached_reverse_geocode(lat_rounded: float, lon_rounded: float) -> dict[str, Any]:
    """Execute Nominatim reverse geocode with rounded coordinates to optimize cache hits."""
    url = "https://nominatim.openstreetmap.org/reverse"
    params = {
        "format": "jsonv2",
        "lat": str(lat_rounded),
        "lon": str(lon_rounded),
        "zoom": "14",
        "addressdetails": "1",
    }
    try:
        with httpx.Client(timeout=3.0, headers=HEADERS, follow_redirects=True) as client:
            resp = client.get(url, params=params)
            if resp.status_code == 200:
                data = resp.json()
                address = data.get("address", {})
                display_name = data.get("display_name", f"{lat_rounded}, {lon_rounded}")
                country = address.get("country")
                state = address.get("state") or address.get("region")
                city = (
                    address.get("city")
                    or address.get("town")
                    or address.get("village")
                    or address.get("municipality")
                    or address.get("county")
                )
                postcode = address.get("postcode")
                return {
                    "display_name": display_name,
                    "country": country,
                    "state": state,
                    "city": city,
                    "postcode": postcode,
                    "osm_id": data.get("osm_id"),
                }
    except Exception as exc:
        logger.debug("Reverse geocode lookup timed out or failed: %s", exc)

    return {
        "display_name": f"Coordinates ({lat_rounded:.5f}°, {lon_rounded:.5f}°)",
        "country": None,
        "state": None,
        "city": None,
        "postcode": None,
        "osm_id": None,
    }


def reverse_geocode(lat: float, lon: float) -> dict[str, Any]:
    """Reverse geocode latitude and longitude to administrative address.

    Args:
        lat: Latitude in decimal degrees [-90..90]
        lon: Longitude in decimal degrees [-180..180]

    Returns:
        Dictionary with display_name, country, state, city, postcode.
    """
    # Round to ~100m precision (3 decimal places) for caching
    return _cached_reverse_geocode(round(lat, 3), round(lon, 3))


@lru_cache(maxsize=1024)
def _cached_elevation(lat_rounded: float, lon_rounded: float) -> dict[str, Any]:
    """Execute terrain elevation lookup with caching."""
    url = f"https://api.open-elevation.com/api/v1/lookup?locations={lat_rounded},{lon_rounded}"
    try:
        with httpx.Client(timeout=3.0, headers=HEADERS, follow_redirects=True) as client:
            resp = client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                results = data.get("results", [])
                if results and "elevation" in results[0]:
                    elev_m = float(results[0]["elevation"])
                    return {
                        "elevation_m": round(elev_m, 1),
                        "elevation_ft": round(elev_m * 3.28084, 1),
                        "source": "Open-Elevation (SRTM)",
                    }
    except Exception as exc:
        logger.debug("Elevation lookup timed out or failed: %s", exc)

    return {
        "elevation_m": 0.0,
        "elevation_ft": 0.0,
        "source": "Unavailable / Sea-Level Baseline",
    }


def get_elevation(lat: float, lon: float) -> dict[str, Any]:
    """Retrieve terrain altitude in metres above sea level.

    Args:
        lat: Latitude in decimal degrees
        lon: Longitude in decimal degrees

    Returns:
        Dictionary with elevation_m, elevation_ft, and source attribution.
    """
    return _cached_elevation(round(lat, 4), round(lon, 4))
