"""GeoJSON FeatureCollection parser.

Parses RFC 7946 GeoJSON FeatureCollections with full defensive validation.
Supports all GeoJSON geometry types: Point, MultiPoint, LineString,
MultiLineString, Polygon, MultiPolygon, and GeometryCollection.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.domain import ParsedFeature, ParsedFile
from app.errors import UnprocessableEntityError

logger = logging.getLogger("geomeasure.parsers.geojson")

_SUPPORTED_GEOMETRY_TYPES = frozenset({
    "Point", "MultiPoint", "LineString", "MultiLineString",
    "Polygon", "MultiPolygon", "GeometryCollection",
})

_MAX_CONTENT_BYTES = 200 * 1024 * 1024  # 200 MB


def parse_geojson(
    content: bytes,
    max_warnings: int = 50,
) -> ParsedFile:
    """Parse a GeoJSON FeatureCollection from raw bytes.

    Args:
        content: Raw UTF-8 bytes of the GeoJSON document.
        max_warnings: Maximum number of warnings to accumulate.

    Returns:
        ParsedFile with features and source CRS.

    Raises:
        UnprocessableEntityError: If JSON is malformed, not a FeatureCollection,
            or contains no features.
    """
    if not content:
        raise UnprocessableEntityError("GeoJSON file is empty (0 bytes).")

    # Decode bytes
    try:
        text = content.decode("utf-8-sig")  # handles optional BOM
    except UnicodeDecodeError as exc:
        raise UnprocessableEntityError(
            f"GeoJSON file is not valid UTF-8: {exc}"
        ) from exc

    # Parse JSON
    try:
        doc: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise UnprocessableEntityError(
            f"Malformed JSON in GeoJSON file: {exc}"
        ) from exc

    if not isinstance(doc, dict):
        raise UnprocessableEntityError(
            "GeoJSON document must be a JSON object, not a bare array or scalar."
        )

    doc_type = doc.get("type")

    feature_list: list[Any]
    if doc_type == "Feature":
        feature_list = [doc]
    elif doc_type == "FeatureCollection":
        raw_features = doc.get("features")
        if not isinstance(raw_features, list):
            raise UnprocessableEntityError(
                "GeoJSON FeatureCollection is missing 'features' array."
            )
        feature_list = raw_features
    else:
        raise UnprocessableEntityError(
            f"Expected GeoJSON type 'FeatureCollection' or 'Feature', got '{doc_type}'."
        )

    if not feature_list:
        raise UnprocessableEntityError(
            "GeoJSON FeatureCollection contains no features."
        )

    # Extract source CRS from optional legacy 'crs' property (GeoJSON 2008)
    source_crs: str | None = "EPSG:4326"  # RFC 7946 default
    legacy_crs = doc.get("crs")
    if isinstance(legacy_crs, dict):
        crs_props = legacy_crs.get("properties", {})
        crs_name = crs_props.get("name", "")
        if crs_name:
            # Normalize urn:ogc:def:crs:EPSG::4326 → EPSG:4326
            if "EPSG" in crs_name:
                epsg_idx = crs_name.upper().find("EPSG")
                if epsg_idx >= 0:
                    remainder = crs_name[epsg_idx:]
                    digits = "".join(c for c in remainder if c.isdigit())
                    if digits:
                        source_crs = f"EPSG:{digits}"
            else:
                source_crs = crs_name

    features: list[ParsedFeature] = []
    warnings: list[str] = []

    for idx, feat in enumerate(feature_list):
        if not isinstance(feat, dict):
            if len(warnings) < max_warnings:
                warnings.append(f"Feature[{idx}]: not a JSON object; skipped.")
            continue

        if feat.get("type") != "Feature":
            if len(warnings) < max_warnings:
                warnings.append(
                    f"Feature[{idx}]: 'type' is not 'Feature'; skipped."
                )
            continue

        geom = feat.get("geometry")
        if geom is None:
            # Null geometry features are valid in RFC 7946 but unmeasurable
            if len(warnings) < max_warnings:
                warnings.append(f"Feature[{idx}]: null geometry; skipped.")
            continue

        if not isinstance(geom, dict):
            if len(warnings) < max_warnings:
                warnings.append(f"Feature[{idx}]: geometry is not a JSON object; skipped.")
            continue

        geom_type = geom.get("type")
        if geom_type not in _SUPPORTED_GEOMETRY_TYPES:
            if len(warnings) < max_warnings:
                warnings.append(
                    f"Feature[{idx}]: unsupported geometry type '{geom_type}'; skipped."
                )
            continue

        if "coordinates" not in geom and geom_type != "GeometryCollection":
            if len(warnings) < max_warnings:
                warnings.append(
                    f"Feature[{idx}]: geometry missing 'coordinates'; skipped."
                )
            continue

        props = feat.get("properties") or {}
        if not isinstance(props, dict):
            props = {}

        features.append(
            ParsedFeature(
                index=len(features),
                geometry_type=str(geom_type),
                geometry=geom,
                properties=props,
            )
        )

    if not features:
        raise UnprocessableEntityError(
            "GeoJSON file yielded no parseable features after validation."
        )

    logger.info(
        "Parsed %d GeoJSON features (CRS: %s, %d warnings)",
        len(features),
        source_crs,
        len(warnings),
    )
    return ParsedFile(features=features, source_crs=source_crs, warnings=warnings)
