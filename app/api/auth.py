"""API key authentication and rate limiting middleware.

Provides optional API key enforcement via the 'X-API-Key' request header.
When GEO_API_KEY is set in the environment, all non-public endpoints require
a valid key. Public paths (/, /health, /docs, /redoc, /openapi.json, /metrics,
/static/*) are always exempt.

Rate limiting is applied via SlowAPI (a FastAPI-compatible limiter built on
limits/slowapi). Limits:
  - Unauthenticated requests: 60/minute per IP
  - Authenticated requests (valid API key): 600/minute per IP
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from app.api.deps import get_settings
from app.config import Settings

logger = logging.getLogger("geomeasure.auth")

# ---------------------------------------------------------------------------
# Public paths exempt from API key requirement
# ---------------------------------------------------------------------------

_PUBLIC_PATHS: frozenset[str] = frozenset({
    "/",
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/metrics",
    "/sample/survey.kml",
    "/sample/survey_shapefile.zip",
})

_PUBLIC_PREFIXES: tuple[str, ...] = (
    "/static/",
    "/api/files/tiles/",  # tile proxy has its own token-based auth
)


def _is_public_path(path: str) -> bool:
    """Return True if the given path is exempt from API key checks."""
    if path in _PUBLIC_PATHS:
        return True
    return any(path.startswith(pfx) for pfx in _PUBLIC_PREFIXES)


# ---------------------------------------------------------------------------
# FastAPI dependency: optional API key enforcement
# ---------------------------------------------------------------------------

def require_api_key(
    request: Request,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    settings: Settings = Depends(get_settings),
) -> bool:
    """Dependency that enforces API key authentication when configured.

    Returns:
        True if the request is authenticated with a valid key.
        False if no key is configured (open access mode).

    Raises:
        HTTPException 401: If a key is required but missing.
        HTTPException 403: If the provided key is invalid.
    """
    configured_key = settings.api_key.strip() if settings.api_key else ""

    # No key configured → open access mode
    if not configured_key:
        return False

    # Public paths are always exempt
    if _is_public_path(request.url.path):
        return False

    if x_api_key is None:
        logger.warning(
            "API key required but missing from request: %s %s",
            request.method,
            request.url.path,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key required. Provide it via the 'X-API-Key' header.",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    if x_api_key != configured_key:
        logger.warning(
            "Invalid API key provided for: %s %s",
            request.method,
            request.url.path,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API key.",
        )

    logger.debug("API key authenticated for: %s %s", request.method, request.url.path)
    return True


# ---------------------------------------------------------------------------
# SlowAPI rate limiter — gracefully optional
# ---------------------------------------------------------------------------

try:
    from slowapi import Limiter, _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded
    from slowapi.util import get_remote_address

    def _get_rate_limit_key(request: Request) -> str:
        """Rate limit key: use 'authenticated:<ip>' for valid API keys, else bare IP."""
        api_key = request.headers.get("X-API-Key", "")
        ip = get_remote_address(request)
        if api_key:
            # Use a hashed key so the real secret isn't stored in Redis/memory
            return f"auth:{hash(api_key)&0xFFFFFFFF}:{ip}"
        return ip

    limiter = Limiter(key_func=_get_rate_limit_key)

    RATE_LIMIT_UNAUTHENTICATED = "60/minute"
    RATE_LIMIT_AUTHENTICATED = "600/minute"

    _SLOWAPI_AVAILABLE = True

except ImportError:
    limiter = None  # type: ignore[assignment]
    RateLimitExceeded = None  # type: ignore[no-redef,assignment,misc]
    _rate_limit_exceeded_handler = None  # type: ignore[assignment]
    RATE_LIMIT_UNAUTHENTICATED = "60/minute"
    RATE_LIMIT_AUTHENTICATED = "600/minute"
    _SLOWAPI_AVAILABLE = False

    logger.debug(
        "slowapi not installed — rate limiting is disabled. "
        "Install with: pip install slowapi"
    )


def is_rate_limiting_available() -> bool:
    """Return True if slowapi is installed and rate limiting is active."""
    return _SLOWAPI_AVAILABLE
