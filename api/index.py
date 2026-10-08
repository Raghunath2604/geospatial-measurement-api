"""Vercel Serverless Function entrypoint for FastAPI."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

# Set Vercel marker
os.environ["VERCEL"] = "1"

# Add project root to sys.path
root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

from app.main import app  # noqa: E402


async def handler(scope: dict[str, Any], receive: Any, send: Any) -> None:
    """ASGI proxy that normalizes request path for Vercel Serverless Functions."""
    if scope.get("type") == "http":
        path = scope.get("path", "/")
        if path in ("/api/index.py", "/api/index", "/api/index.py/", "/api/index/"):
            scope["path"] = "/"
            scope["raw_path"] = b"/"
        elif path.startswith("/api/index.py/"):
            sub_path = path[len("/api/index.py") :]
            scope["path"] = sub_path
            scope["raw_path"] = sub_path.encode("ascii")
        elif path.startswith("/api/index/"):
            sub_path = path[len("/api/index") :]
            scope["path"] = sub_path
            scope["raw_path"] = sub_path.encode("ascii")

    await app(scope, receive, send)
