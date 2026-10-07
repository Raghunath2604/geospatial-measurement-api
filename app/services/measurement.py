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
