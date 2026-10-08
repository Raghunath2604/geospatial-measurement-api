"""Vercel Serverless Function entrypoint for FastAPI."""

import os
import sys
from pathlib import Path

# Set Vercel marker
os.environ["VERCEL"] = "1"

# Add project root to sys.path
root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

from app.main import app  # noqa: E402

# ASGI handler exposed for Vercel Python runtime
handler = app
