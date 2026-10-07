"""KML file parser using hardened lxml.

Handles KML 2.0, 2.1, and 2.2 schemas in a namespace-agnostic manner.
Extracts Point, LineString, LinearRing, Polygon, and MultiGeometry features
into standardized GeoJSON-style feature dictionaries.
Protects against XML External Entity (XXE) and entity expansion attacks.
"""

from __future__ import annotations

import re
from typing import Any

from lxml import etree

from app.domain import ParsedFeature, ParsedFile
from app.errors import UnprocessableEntityError


def _create_hardened_parser() -> etree.XMLParser:
    """Instantiate a secure XML parser protected against XXE and entity expansion."""
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        dtd_validation=False,
        load_dtd=False,
    )


def _parse_coordinates(coord_text: str) -> list[list[float]]:
    """Parse KML coordinate tuples: 'lon,lat[,alt] [lon,lat[,alt] ...]'."""
    cleaned = coord_text.strip()
    if not cleaned:
        raise ValueError("Empty coordinate text")

    coords: list[list[float]] = []
    # Coordinates can be separated by spaces, tabs, or newlines
    for token in re.split(r"\s+", cleaned):
        if not token:
            continue
        parts = token.split(",")
        if len(parts) < 2:
            raise ValueError(f"Malformed coordinate token '{token}'")
        try:
            lon = float(parts[0])
            lat = float(parts[1])
        except ValueError as exc:
            raise ValueError(f"Non-numeric coordinate value in '{token}'") from exc

        # Validate range for geographic degrees
        if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
            raise ValueError(
                f"Coordinates ({lon}, {lat}) out of geographic range [-180..180, -90..90]"
            )
        coords.append([lon, lat])

    if not coords:
        raise ValueError("No valid coordinates parsed")
    return coords


def _close_ring_if_needed(ring: list[list[float]]) -> list[list[float]]:
    """Ensure a linear ring is closed by appending the first point to the end."""
    if len(ring) < 3:
        raise ValueError("LinearRing must have at least 3 points before closure")
    if ring[0] != ring[-1]:
        ring.append(list(ring[0]))
    if len(ring) < 4:
        raise ValueError("Closed LinearRing must have at least 4 points")
    return ring


def _parse_geometry_element(elem: etree._Element) -> dict[str, Any] | None:
    """Recursively parse a KML geometry element into a GeoJSON geometry dictionary."""
    tag = etree.QName(elem).localname

    if tag == "Point":
        coords_elem = elem.xpath("./*[local-name()='coordinates']")
        if not coords_elem or not coords_elem[0].text:
            raise ValueError("Point missing coordinates")
        coords = _parse_coordinates(coords_elem[0].text)
        return {"type": "Point", "coordinates": coords[0]}

    if tag in ("LineString", "LinearRing"):
        coords_elem = elem.xpath("./*[local-name()='coordinates']")
        if not coords_elem or not coords_elem[0].text:
            raise ValueError(f"{tag} missing coordinates")
        coords = _parse_coordinates(coords_elem[0].text)
        if len(coords) < 2:
            raise ValueError(f"{tag} must contain at least 2 points")
        return {"type": "LineString", "coordinates": coords}

    if tag == "Polygon":
        outer_elem = elem.xpath(
            "./*[local-name()='outerBoundaryIs']/*[local-name()='LinearRing']/*[local-name()='coordinates']"
        )
        if not outer_elem or not outer_elem[0].text:
            raise ValueError("Polygon missing outerBoundaryIs coordinates")
        outer_ring = _close_ring_if_needed(_parse_coordinates(outer_elem[0].text))
        rings: list[list[list[float]]] = [outer_ring]

        # Inner boundaries (holes)
        inner_elems = elem.xpath(
            "./*[local-name()='innerBoundaryIs']/*[local-name()='LinearRing']/*[local-name()='coordinates']"
        )
        for inner_elem in inner_elems:
            if inner_elem.text and inner_elem.text.strip():
                try:
                    inner_ring = _close_ring_if_needed(
                        _parse_coordinates(inner_elem.text)
                    )
                    rings.append(inner_ring)
                except ValueError:
                    # Skip malformed hole but preserve outer polygon
                    continue
        return {"type": "Polygon", "coordinates": rings}

    if tag == "MultiGeometry":
        child_geoms: list[dict[str, Any]] = []
        for child in elem:
            child_tag = etree.QName(child).localname
            if child_tag in (
                "Point",
                "LineString",
                "LinearRing",
                "Polygon",
                "MultiGeometry",
            ):
                parsed_child = _parse_geometry_element(child)
                if parsed_child:
                    child_geoms.append(parsed_child)

        if not child_geoms:
            raise ValueError("MultiGeometry contains no valid child geometries")

        types = {g["type"] for g in child_geoms}
        if len(types) == 1:
            geom_type = next(iter(types))
            if geom_type == "Point":
                return {
                    "type": "MultiPoint",
                    "coordinates": [g["coordinates"] for g in child_geoms],
                }
            if geom_type == "LineString":
                return {
                    "type": "MultiLineString",
                    "coordinates": [g["coordinates"] for g in child_geoms],
                }
            if geom_type == "Polygon":
                return {
                    "type": "MultiPolygon",
                    "coordinates": [g["coordinates"] for g in child_geoms],
                }

        # Heterogeneous collection
        return {"type": "GeometryCollection", "geometries": child_geoms}

    return None


def parse_kml(content: bytes, max_warnings: int = 50) -> ParsedFile:
    """Parse KML content bytes and extract features with properties and geometry.

    Args:
        content: Raw bytes of the KML file.
        max_warnings: Maximum number of warnings to collect before truncating.

    Returns:
        ParsedFile containing extracted features, source CRS ('EPSG:4326'), and warnings.

    Raises:
        UnprocessableEntityError: If file is not valid XML, has invalid root, or has no usable features.
    """
    if not content or not content.strip():
        raise UnprocessableEntityError("Uploaded KML file is empty")

    parser = _create_hardened_parser()
    try:
        root = etree.fromstring(content, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise UnprocessableEntityError(f"XML parse error: {exc}") from exc
    except Exception as exc:
        raise UnprocessableEntityError(f"Invalid XML document: {exc}") from exc

    root_tag = etree.QName(root).localname
    if root_tag.lower() != "kml":
        raise UnprocessableEntityError(
            f"Invalid root element <{root_tag}>; expected <kml>"
        )

    # Find all Placemark elements across any namespace
    placemark_nodes = root.xpath(".//*[local-name()='Placemark']")
    if not placemark_nodes:
        raise UnprocessableEntityError("No Placemark elements found in KML file")

    features: list[ParsedFeature] = []
    warnings: list[str] = []

    def add_warning(msg: str) -> None:
        if len(warnings) < max_warnings:
            warnings.append(msg)

    for idx, pm in enumerate(placemark_nodes):
        # 1. Extract properties
        properties: dict[str, Any] = {}

        name_elem = pm.xpath("./*[local-name()='name']")
        if name_elem and name_elem[0].text:
            properties["name"] = name_elem[0].text.strip()

        desc_elem = pm.xpath("./*[local-name()='description']")
        if desc_elem and desc_elem[0].text:
            properties["description"] = desc_elem[0].text.strip()

        # ExtendedData / Data
        for data_elem in pm.xpath(
            "./*[local-name()='ExtendedData']/*[local-name()='Data']"
        ):
            key = data_elem.get("name")
            val_elem = data_elem.xpath("./*[local-name()='value']")
            if key and val_elem and val_elem[0].text:
                properties[key] = val_elem[0].text.strip()

        # ExtendedData / SchemaData / SimpleData
        for simple_elem in pm.xpath(
            "./*[local-name()='ExtendedData']/*[local-name()='SchemaData']/*[local-name()='SimpleData']"
        ):
            key = simple_elem.get("name")
            if key and simple_elem.text:
                properties[key] = simple_elem.text.strip()

        # 2. Extract geometry
        geom_dict: dict[str, Any] | None = None
        error_msg: str | None = None

        # Look for direct geometry children or nested geometry
        for child in pm:
            tag = etree.QName(child).localname
            if tag in (
                "Point",
                "LineString",
                "LinearRing",
                "Polygon",
                "MultiGeometry",
            ):
                try:
                    geom_dict = _parse_geometry_element(child)
                    if geom_dict:
                        break
                except ValueError as err:
                    error_msg = str(err)

        if not geom_dict:
            detail = error_msg or "No valid or supported geometry element found"
            add_warning(f"Placemark at index {idx} skipped: {detail}")
            continue

        features.append(
            ParsedFeature(
                index=idx,
                geometry_type=geom_dict["type"],
                geometry=geom_dict,
                properties=properties,
            )
        )

    if not features:
        raise UnprocessableEntityError("No usable Placemark features found in KML file")

    return ParsedFile(features=features, source_crs="EPSG:4326", warnings=warnings)
