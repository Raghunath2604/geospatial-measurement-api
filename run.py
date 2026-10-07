"""Entrypoint script to boot the API server with pretty console output."""

from __future__ import annotations

import os

import uvicorn

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "127.0.0.1")

    print("\n" + "=" * 65)
    print("  [+] GEOSPATIAL FILE MEASUREMENT API -- SERVER STARTING")
    print("=" * 65)
    print(f"  * Web Dashboard:     http://{host}:{port}/")
    print(f"  * Swagger Docs:      http://{host}:{port}/docs")
    print(f"  * Health Check:      http://{host}:{port}/health")
    print("=" * 65 + "\n")

    uvicorn.run("app.main:create_app", factory=True, host=host, port=port, reload=True)
