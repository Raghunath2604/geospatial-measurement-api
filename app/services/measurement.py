"""Geospatial measurement service.

Evaluates metric area (square metres) and length (metres) for geospatial features.
Enforces projection from geographic (EPSG:4326) or projected CRSs to optimal
metric UTM or UPS zones. Isolated per-feature execution ensures individual
geometry defects never raise exceptions or fail the file ingestion.
"""

from __future__ import annotations

from typing import Any

import pyproj
import shapely
import shapely.ops
import shapely.validation
from pyproj.crs import CRS, CRSError

from app.domain import Measurement, MeasurementStatus
from app.errors import UnprocessableEntityError
from app.services.projection import utm_epsg_for


def resolve_crs(crs_input: str | CRS | None) -> CRS:
    """Resolve and validate a CRS string or object.

    Args:
        crs_input: EPSG code, WKT string, PROJ string, or CRS object.

    Returns:
        Validated pyproj.crs.CRS instance.

    Raises:
        UnprocessableEntityError: If CRS is missing, invalid, or neither geographic nor projected.
    """
    if crs_input is None or (isinstance(crs_input, str) and not crs_input.strip()):
        raise UnprocessableEntityError("CRS is required and cannot be empty")

    if isinstance(crs_input, CRS):
        crs_obj = crs_input
    else:
        try:
            crs_obj = CRS.from_user_input(crs_input)
        except (CRSError, Exception) as exc:
            raise UnprocessableEntityError(
                f"Failed to resolve coordinate reference system '{crs_input}': {exc}"
            ) from exc

    # Reject non-geographic and non-projected CRSs (e.g. Geocentric, Engineering)
    if not (crs_obj.is_geographic or crs_obj.is_projected):
        raise UnprocessableEntityError(
            f"Unsupported CRS type '{crs_obj.type_name}'. CRS must be geographic or projected."
        )

    return crs_obj


def crs_label(crs_obj: CRS) -> str:
    """Produce a concise standard label for a CRS (e.g. 'EPSG:4326')."""
    epsg_code = crs_obj.to_epsg()
    if epsg_code is not None:
        return f"EPSG:{epsg_code}"
    # If no single EPSG code, check authority
    auth_code = crs_obj.to_authority()
    if auth_code:
        return f"{auth_code[0]}:{auth_code[1]}"
    return crs_obj.name or crs_obj.to_string()


class MeasurementService:
    """Performs metric calculations on features reprojected to local metric UTM/UPS."""

    def __init__(self, source_crs: str | CRS) -> None:
        self.source_crs = resolve_crs(source_crs)
        self.source_label = crs_label(self.source_crs)
        self._wgs84 = CRS.from_epsg(4326)

        # Transformer to WGS84 for projected inputs (lon, lat centroid lookup)
        if self.source_crs.is_geographic:
            self._to_wgs84 = None
        else:
            self._to_wgs84 = pyproj.Transformer.from_crs(
                self.source_crs, self._wgs84, always_xy=True
            )

        # Cache of metric transformers: epsg_code -> pyproj.Transformer
        self._transformers: dict[int, pyproj.Transformer] = {}

    def _get_transformer_to(self, target_epsg: int) -> pyproj.Transformer:
        if target_epsg not in self._transformers:
            target_crs = CRS.from_epsg(target_epsg)
            self._transformers[target_epsg] = pyproj.Transformer.from_crs(
                self.source_crs, target_crs, always_xy=True
            )
        return self._transformers[target_epsg]

    def measure(
        self, feature_index: int, geometry: dict[str, Any] | None
    ) -> Measurement:
        """Measure a single feature geometry. Never raises an unhandled exception.

        Args:
            feature_index: 0-based source index of the feature.
            geometry: GeoJSON geometry dictionary or None.

        Returns:
            Measurement instance with computed values or non-error status.
        """
        if not geometry or not isinstance(geometry, dict):
            return Measurement(
                feature_index=feature_index,
                geometry_type="Unknown",
                status=MeasurementStatus.UNSUPPORTED,
                message="Feature has null or empty geometry",
            )

        geom_type = geometry.get("type", "Unknown")

        # 1. Point / MultiPoint -> NOT_APPLICABLE
        if geom_type in ("Point", "MultiPoint"):
            return Measurement(
                feature_index=feature_index,
                geometry_type=geom_type,
                status=MeasurementStatus.NOT_APPLICABLE,
                message="Point features do not possess area or length",
            )

        # 2. Check supported geometry types
        supported_types = ("Polygon", "MultiPolygon", "LineString", "MultiLineString")
        if geom_type not in supported_types:
            return Measurement(
                feature_index=feature_index,
                geometry_type=geom_type,
                status=MeasurementStatus.UNSUPPORTED,
                message=f"Unsupported geometry type: {geom_type}",
            )

        # 3. Parse Shapely shape and validate
        try:
            shapely_geom = shapely.geometry.shape(geometry)
        except Exception as exc:
            return Measurement(
                feature_index=feature_index,
                geometry_type=geom_type,
                status=MeasurementStatus.ERROR,
                message=f"Failed to parse geometry structure: {exc}",
            )

        if shapely_geom.is_empty:
            return Measurement(
                feature_index=feature_index,
                geometry_type=geom_type,
                status=MeasurementStatus.UNSUPPORTED,
                message="Geometry is empty and has no measurable extent",
            )

        try:
            # Force 2D geometry (drop z/m coordinates)
            geom_2d = shapely.force_2d(shapely_geom)

            # Check validity (e.g. self-intersection / bow-tie) without repairing
            is_valid = geom_2d.is_valid
            validity_msg: str | None = None
            if not is_valid:
                reason = shapely.validation.explain_validity(geom_2d)
                validity_msg = (
                    f"Geometry is invalid ({reason}); measured without silent repair"
                )

            # Determine centroid coordinates in WGS84 degrees
            centroid = geom_2d.centroid
            if centroid.is_empty:
                return Measurement(
                    feature_index=feature_index,
                    geometry_type=geom_type,
                    status=MeasurementStatus.UNSUPPORTED,
                    message="Centroid cannot be evaluated for this geometry",
                )

            if self._to_wgs84 is not None:
                # Source is projected; reproject centroid to WGS84 to determine UTM/UPS zone
                wgs84_centroid = shapely.ops.transform(
                    self._to_wgs84.transform, centroid
                )
                lon, lat = wgs84_centroid.x, wgs84_centroid.y
            else:
                # Source is already geographic (EPSG:4326)
                lon, lat = centroid.x, centroid.y

            # Determine target metric EPSG
            target_epsg = utm_epsg_for(lat, lon)
            target_crs_label = f"EPSG:{target_epsg}"

            # Transform geometry to metric planar CRS
            transformer = self._get_transformer_to(target_epsg)
            metric_geom = shapely.ops.transform(transformer.transform, geom_2d)

            # Calculate measurements
            area_sq_m: float | None = None
            length_m: float | None = None

            if geom_type in ("Polygon", "MultiPolygon"):
                # Shapely automatically subtracts interior rings (holes)
                area_sq_m = round(float(metric_geom.area), 2)
            elif geom_type in ("LineString", "MultiLineString"):
                length_m = round(float(metric_geom.length), 2)

            return Measurement(
                feature_index=feature_index,
                geometry_type=geom_type,
                status=MeasurementStatus.MEASURED,
                area_sq_m=area_sq_m,
                length_m=length_m,
                measurement_crs=target_crs_label,
                message=validity_msg,
            )

        except Exception as exc:
            return Measurement(
                feature_index=feature_index,
                geometry_type=geom_type,
                status=MeasurementStatus.ERROR,
                message=f"Measurement error: {exc}",
            )


def measure_geometry_live(
    geometry: dict[str, Any] | None,
    source_crs: str = "EPSG:4326",
) -> dict[str, Any]:
    """Measure an arbitrary GeoJSON geometry in real time with dual planar and geodesic metrics.

    Calculates metric planar area/length in dynamically selected UTM/UPS zones alongside
    WGS 84 ellipsoidal geodesics, unit conversions, centroid, bounding box, and validity.
    """
    if not geometry or not isinstance(geometry, dict):
        raise UnprocessableEntityError("Valid GeoJSON geometry dictionary is required.")

    geom_type = geometry.get("type", "Unknown")
    supported = ("Point", "MultiPoint", "Polygon", "MultiPolygon", "LineString", "MultiLineString")
    if geom_type not in supported:
        raise UnprocessableEntityError(f"Unsupported geometry type '{geom_type}'.")

    try:
        shapely_geom = shapely.geometry.shape(geometry)
    except Exception as exc:
        raise UnprocessableEntityError(f"Invalid geometry structure: {exc}") from exc

    if shapely_geom.is_empty:
        raise UnprocessableEntityError("Geometry is empty and has no coordinates.")

    geom_2d = shapely.force_2d(shapely_geom)
    is_valid = geom_2d.is_valid
    validity_msg = None
    if not is_valid:
        validity_msg = f"Geometry has topological flaws: {shapely.validation.explain_validity(geom_2d)}"

    bounds = [round(b, 6) for b in geom_2d.bounds]
    centroid_pt = geom_2d.centroid
    centroid = [round(centroid_pt.x, 6), round(centroid_pt.y, 6)]

    # Count vertices
    vertex_count = 0
    if geom_type == "Point":
        vertex_count = 1
    elif geom_type == "MultiPoint":
        vertex_count = len(geom_2d.geoms)
    elif geom_type == "LineString":
        vertex_count = len(geom_2d.coords)
    elif geom_type == "MultiLineString":
        vertex_count = sum(len(ls.coords) for ls in geom_2d.geoms)
    elif geom_type == "Polygon":
        vertex_count = len(geom_2d.exterior.coords) + sum(len(r.coords) for r in geom_2d.interiors)
    elif geom_type == "MultiPolygon":
        for poly in geom_2d.geoms:
            vertex_count += len(poly.exterior.coords) + sum(len(r.coords) for r in poly.interiors)

    # Resolve source CRS & determine centroid in WGS84 for UTM selection
    crs_obj = resolve_crs(source_crs)
    if crs_obj.is_geographic:
        wgs_lat, wgs_lon = centroid_pt.y, centroid_pt.x
    else:
        to_wgs84 = pyproj.Transformer.from_crs(crs_obj, CRS.from_epsg(4326), always_xy=True)
        wgs_centroid = shapely.ops.transform(to_wgs84.transform, centroid_pt)
        wgs_lat, wgs_lon = wgs_centroid.y, wgs_centroid.x

    target_epsg = utm_epsg_for(wgs_lat, wgs_lon)
    measurement_crs = f"EPSG:{target_epsg}"

    # Handle Points
    if geom_type in ("Point", "MultiPoint"):
        return {
            "geometry_type": geom_type,
            "status": MeasurementStatus.NOT_APPLICABLE.value,
            "planar_area_sq_m": 0.0,
            "planar_length_m": 0.0,
            "geodesic_area_sq_m": 0.0,
            "geodesic_length_m": 0.0,
            "measurement_crs": measurement_crs,
            "centroid": centroid,
            "bbox": bounds,
            "vertex_count": vertex_count,
            "units": {
                "area_sq_m": 0.0,
                "area_hectares": 0.0,
                "area_sq_km": 0.0,
                "area_acres": 0.0,
                "area_sq_ft": 0.0,
                "length_m": 0.0,
                "length_km": 0.0,
                "length_feet": 0.0,
                "length_miles": 0.0,
            },
            "message": "Point geometries do not have planar area or length.",
        }

    # Transform to UTM/UPS
    transformer = pyproj.Transformer.from_crs(crs_obj, CRS.from_epsg(target_epsg), always_xy=True)
    metric_geom = shapely.ops.transform(transformer.transform, geom_2d)

    geod = pyproj.Geod(ellps="WGS84")
    # Transform geom to WGS84 if source is not geographic for geodesic calculation
    if crs_obj.is_geographic:
        wgs_geom = geom_2d
    else:
        to_wgs = pyproj.Transformer.from_crs(crs_obj, CRS.from_epsg(4326), always_xy=True)
        wgs_geom = shapely.ops.transform(to_wgs.transform, geom_2d)

    planar_area: float | None = None
    planar_len: float | None = None
    geod_area: float | None = None
    geod_len: float | None = None

    if geom_type in ("Polygon", "MultiPolygon"):
        planar_area = round(float(metric_geom.area), 2)
        planar_len = round(float(metric_geom.length), 2)  # perimeter
        # Geodesic area
        if wgs_geom.geom_type == "Polygon":
            ext_a, ext_p = geod.geometry_area_perimeter(wgs_geom.exterior)
            holes_a = sum(abs(geod.geometry_area_perimeter(h)[0]) for h in wgs_geom.interiors)
            geod_area = round(abs(ext_a) - holes_a, 2)
            geod_len = round(abs(ext_p), 2)
        else:
            total_a = 0.0
            total_p = 0.0
            for poly in wgs_geom.geoms:
                ea, ep = geod.geometry_area_perimeter(poly.exterior)
                ha = sum(abs(geod.geometry_area_perimeter(h)[0]) for h in poly.interiors)
                total_a += abs(ea) - ha
                total_p += abs(ep)
            geod_area = round(total_a, 2)
            geod_len = round(total_p, 2)

    elif geom_type in ("LineString", "MultiLineString"):
        planar_len = round(float(metric_geom.length), 2)
        planar_area = 0.0
        geod_len = round(abs(geod.geometry_length(wgs_geom)), 2)
        geod_area = 0.0

    sq_m = planar_area or 0.0
    lin_m = planar_len or 0.0

    units = {
        "area_sq_m": sq_m,
        "area_hectares": round(sq_m / 10_000.0, 4),
        "area_sq_km": round(sq_m / 1_000_000.0, 6),
        "area_acres": round(sq_m * 0.000247105, 4),
        "area_sq_ft": round(sq_m * 10.7639, 2),
        "length_m": lin_m,
        "length_km": round(lin_m / 1000.0, 4),
        "length_feet": round(lin_m * 3.28084, 2),
        "length_miles": round(lin_m * 0.000621371, 4),
    }

    return {
        "geometry_type": geom_type,
        "status": MeasurementStatus.MEASURED.value,
        "planar_area_sq_m": planar_area,
        "planar_length_m": planar_len,
        "geodesic_area_sq_m": geod_area,
        "geodesic_length_m": geod_len,
        "measurement_crs": measurement_crs,
        "centroid": centroid,
        "bbox": bounds,
        "vertex_count": vertex_count,
        "units": units,
        "message": validity_msg,
    }

