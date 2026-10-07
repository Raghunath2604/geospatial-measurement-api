"""KMZ parser extracting internal KML from zipped archives.

A KMZ file is a ZIP archive that contains a main KML file (commonly 'doc.kml'
or any file with a '.kml' extension) and optional supporting assets.
Protected against Zip-Slip, decompression bombs, and malformed member names.
"""

from __future__ import annotations

import io
import zipfile

from app.domain import ParsedFile
from app.errors import UnprocessableEntityError
from app.parsers.kml import parse_kml


def parse_kmz(
    content: bytes,
    max_uncompressed_bytes: int = 200 * 1024 * 1024,
    max_zip_members: int = 100,
    max_warnings: int = 50,
) -> ParsedFile:
    """Extract and parse the primary KML file from a KMZ archive.

    Args:
        content: Raw binary content of the .kmz file.
        max_uncompressed_bytes: Total uncompressed byte cap across all members.
        max_zip_members: Maximum number of entries allowed in the archive.
        max_warnings: Maximum warnings collected during KML parsing.

    Returns:
        ParsedFile containing features and source CRS (EPSG:4326).

    Raises:
        UnprocessableEntityError: If archive is invalid, exceeds quotas, has Zip-Slip,
            or contains no .kml file.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except (zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise UnprocessableEntityError(f"Corrupt or invalid KMZ archive: {exc}") from exc

    with zf:
        members = zf.infolist()
        if len(members) == 0:
            raise UnprocessableEntityError("KMZ archive is empty.")

        if len(members) > max_zip_members:
            raise UnprocessableEntityError(
                f"KMZ archive contains {len(members)} entries, exceeding the limit of {max_zip_members}."
            )

        total_uncompressed = 0
        kml_members: list[zipfile.ZipInfo] = []

        for info in members:
            # Check for Zip-Slip / path traversal
            norm_name = info.filename.replace("\\", "/")
            if norm_name.startswith("/") or ".." in norm_name.split("/"):
                raise UnprocessableEntityError(
                    f"Path traversal detected in KMZ entry: '{info.filename}'"
                )

            total_uncompressed += info.file_size
            if total_uncompressed > max_uncompressed_bytes:
                raise UnprocessableEntityError(
                    f"KMZ uncompressed size exceeds limit of {max_uncompressed_bytes} bytes."
                )

            if not info.is_dir() and norm_name.lower().endswith(".kml"):
                kml_members.append(info)

        if not kml_members:
            raise UnprocessableEntityError(
                "KMZ archive does not contain any valid .kml file."
            )

        # Select primary KML member: prefer doc.kml or first member
        selected_member = kml_members[0]
        for m in kml_members:
            if m.filename.lower().endswith("doc.kml"):
                selected_member = m
                break

        try:
            kml_bytes = zf.read(selected_member)
        except Exception as exc:
            raise UnprocessableEntityError(
                f"Failed to read '{selected_member.filename}' from KMZ: {exc}"
            ) from exc

    parsed = parse_kml(kml_bytes, max_warnings=max_warnings)
    if len(kml_members) > 1 and len(parsed.warnings) < max_warnings:
        parsed.warnings.append(
            f"KMZ contains {len(kml_members)} KML files; parsed primary file '{selected_member.filename}'."
        )

    return parsed
