"""Configuration management using frozen dataclass populated from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Load .env file automatically on application initialization
load_dotenv()


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

    # Third-party credentials & tokens
    mapbox_access_token: str = ""
    carto_api_key: str = ""
    opentopography_api_key: str = ""

    # API security
    api_key: str = ""  # GEO_API_KEY — when set, all non-public routes require this key

    # Async processing queue
    async_worker_concurrency: int = 4  # Number of background ingestion workers

    # Author & project attribution
    author_name: str = "Raghunath"
    author_github: str = "https://github.com/Raghunath2604"
    repository_url: str = "https://github.com/Raghunath2604/geospatial-measurement-api"

    # Server binding
    host: str = "127.0.0.1"
    port: int = 8000

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

        mapbox_access_token = os.getenv("MAPBOX_ACCESS_TOKEN", "").strip()
        carto_api_key = os.getenv("CARTO_API_KEY", "").strip()
        opentopography_api_key = os.getenv("OPENTOPOGRAPHY_API_KEY", "").strip()

        # Security
        api_key = os.getenv("GEO_API_KEY", "").strip()

        # Async workers
        async_worker_concurrency = int(os.getenv("GEO_ASYNC_WORKERS", "4"))

        author_name = os.getenv("GEO_AUTHOR_NAME", "Raghunath").strip()
        author_github = os.getenv(
            "GEO_AUTHOR_GITHUB", "https://github.com/Raghunath2604"
        ).strip()
        repository_url = os.getenv(
            "GEO_REPOSITORY_URL",
            "https://github.com/Raghunath2604/geospatial-measurement-api",
        ).strip()

        host = os.getenv("GEO_HOST", "127.0.0.1").strip()
        port = int(os.getenv("GEO_PORT", "8000"))

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
            mapbox_access_token=mapbox_access_token,
            carto_api_key=carto_api_key,
            opentopography_api_key=opentopography_api_key,
            api_key=api_key,
            async_worker_concurrency=async_worker_concurrency,
            author_name=author_name,
            author_github=author_github,
            repository_url=repository_url,
            host=host,
            port=port,
        )
