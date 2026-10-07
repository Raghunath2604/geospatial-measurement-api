"""Configuration management using frozen dataclass populated from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    """Application settings and runtime limits."""

    db_path: str = "data/geomeasure.db"
    max_upload_bytes: int = 50 * 1024 * 1024  # 50 MB upload cap
    max_uncompressed_bytes: int = 200 * 1024 * 1024  # 200 MB zip extraction cap
    max_zip_members: int = 100  # Max files allowed in zip archive
    max_features: int = 10_000  # Max features processed per file
    max_warnings: int = 50  # Warning list cutoff per file
    chunk_size_bytes: int = 1024 * 1024  # 1 MB streaming chunk

    @classmethod
    def from_env(cls) -> Settings:
        """Create Settings instance reading from environment variables."""
        db_path = os.getenv("GEO_DB_PATH", "data/geomeasure.db")
        max_upload_bytes = int(os.getenv("GEO_MAX_UPLOAD_BYTES", str(50 * 1024 * 1024)))
        max_uncompressed_bytes = int(
            os.getenv("GEO_MAX_UNCOMPRESSED_BYTES", str(200 * 1024 * 1024))
        )
        max_zip_members = int(os.getenv("GEO_MAX_ZIP_MEMBERS", "100"))
        max_features = int(os.getenv("GEO_MAX_FEATURES", "10000"))
        max_warnings = int(os.getenv("GEO_MAX_WARNINGS", "50"))
        chunk_size_bytes = int(os.getenv("GEO_CHUNK_SIZE_BYTES", str(1024 * 1024)))

        # Ensure directory for sqlite database exists
        db_parent = Path(db_path).parent
        if db_parent and not db_parent.exists():
            db_parent.mkdir(parents=True, exist_ok=True)

        return cls(
            db_path=db_path,
            max_upload_bytes=max_upload_bytes,
            max_uncompressed_bytes=max_uncompressed_bytes,
            max_zip_members=max_zip_members,
            max_features=max_features,
            max_warnings=max_warnings,
            chunk_size_bytes=chunk_size_bytes,
        )
