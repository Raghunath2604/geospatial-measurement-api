"""Prometheus metrics definitions for the Geospatial Measurement API.

Exposes key operational metrics:
- File ingestion counts by format and status (counter)
- Ingestion duration histogram (seconds)
- Upload file size histogram (bytes)
- Async queue depth gauge
- Real-time geometry measurement latency
- HTTP request count by endpoint and method (counter)
- HTTP response latency histogram

Usage:
    from app.monitoring.metrics import (
        INGEST_COUNTER, INGEST_DURATION, FILE_SIZE_BYTES,
        record_ingest, record_request
    )
"""

from __future__ import annotations

import time
from collections.abc import Generator
from contextlib import contextmanager

try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        REGISTRY,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )
    _PROMETHEUS_AVAILABLE = True
except ImportError:
    _PROMETHEUS_AVAILABLE = False

# ---------------------------------------------------------------------------
# Registry & Metric Declarations
# ---------------------------------------------------------------------------

if _PROMETHEUS_AVAILABLE:
    # File ingestion counter: labels = format, status
    INGEST_COUNTER: Counter = Counter(
        "geomeasure_file_ingestions_total",
        "Total number of file ingestion attempts",
        labelnames=["format", "status"],
    )

    # Ingestion end-to-end latency histogram (seconds)
    INGEST_DURATION: Histogram = Histogram(
        "geomeasure_file_ingestion_duration_seconds",
        "Time taken to parse, measure, and persist a geospatial file",
        labelnames=["format"],
        buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
    )

    # Upload size histogram (bytes)
    FILE_SIZE_BYTES: Histogram = Histogram(
        "geomeasure_file_size_bytes",
        "Size of uploaded geospatial files in bytes",
        labelnames=["format"],
        buckets=(
            10_000,       # 10 KB
            100_000,      # 100 KB
            1_000_000,    # 1 MB
            5_000_000,    # 5 MB
            10_000_000,   # 10 MB
            50_000_000,   # 50 MB
        ),
    )

    # Async queue depth gauge
    ASYNC_QUEUE_DEPTH: Gauge = Gauge(
        "geomeasure_async_queue_depth",
        "Number of ingestion tasks currently queued",
    )

    # Feature count per ingested file histogram
    FEATURE_COUNT: Histogram = Histogram(
        "geomeasure_file_feature_count",
        "Number of features per successfully ingested file",
        labelnames=["format"],
        buckets=(1, 5, 10, 50, 100, 500, 1000, 5000, 10000),
    )

    # Real-time geometry measurement duration
    LIVE_MEASURE_DURATION: Histogram = Histogram(
        "geomeasure_live_measure_duration_seconds",
        "Latency of real-time POST /api/measure/geometry/ calls",
        buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5),
    )

    # HTTP request counter: labels = method, endpoint, status_code
    HTTP_REQUEST_COUNTER: Counter = Counter(
        "geomeasure_http_requests_total",
        "Total HTTP requests by method, endpoint, and status code",
        labelnames=["method", "endpoint", "status_code"],
    )

    # HTTP request latency histogram
    HTTP_REQUEST_DURATION: Histogram = Histogram(
        "geomeasure_http_request_duration_seconds",
        "HTTP request duration by method and endpoint",
        labelnames=["method", "endpoint"],
        buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 5.0),
    )

else:
    # Stub objects when prometheus_client is not installed
    class _NoopMetric:
        """Silent no-op metric stub."""
        def labels(self, **kwargs: object) -> _NoopMetric:
            return self
        def inc(self, amount: float = 1) -> None: ...
        def observe(self, amount: float) -> None: ...
        def set(self, value: float) -> None: ...

    INGEST_COUNTER = _NoopMetric()  # type: ignore[assignment]
    INGEST_DURATION = _NoopMetric()  # type: ignore[assignment]
    FILE_SIZE_BYTES = _NoopMetric()  # type: ignore[assignment]
    ASYNC_QUEUE_DEPTH = _NoopMetric()  # type: ignore[assignment]
    FEATURE_COUNT = _NoopMetric()  # type: ignore[assignment]
    LIVE_MEASURE_DURATION = _NoopMetric()  # type: ignore[assignment]
    HTTP_REQUEST_COUNTER = _NoopMetric()  # type: ignore[assignment]
    HTTP_REQUEST_DURATION = _NoopMetric()  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------

def record_ingest(
    fmt: str,
    status: str,
    duration_s: float,
    file_size_bytes: int,
    feature_count: int = 0,
) -> None:
    """Record a completed file ingestion attempt.

    Args:
        fmt: Source file format label (e.g., 'KML', 'GEOJSON').
        status: Ingestion outcome ('COMPLETED' or 'FAILED').
        duration_s: Elapsed wall-clock seconds for the ingestion.
        file_size_bytes: Raw upload size in bytes.
        feature_count: Number of features parsed (0 on failure).
    """
    fmt_lower = fmt.upper()
    INGEST_COUNTER.labels(format=fmt_lower, status=status).inc()
    INGEST_DURATION.labels(format=fmt_lower).observe(duration_s)
    FILE_SIZE_BYTES.labels(format=fmt_lower).observe(file_size_bytes)
    if status == "COMPLETED" and feature_count > 0:
        FEATURE_COUNT.labels(format=fmt_lower).observe(feature_count)


@contextmanager
def timed_ingest(fmt: str) -> Generator[None, None, None]:
    """Context manager that automatically times an ingestion block.

    Usage::

        with timed_ingest("KML") as timer:
            record = ingest_service.ingest_stream(...)
        # metrics automatically recorded on exit
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - start
        INGEST_DURATION.labels(format=fmt.upper()).observe(elapsed)


def get_metrics_output() -> tuple[bytes, str]:
    """Generate current Prometheus metrics text output.

    Returns:
        (content_bytes, content_type) — ready for HTTP response.

    Raises:
        RuntimeError: If prometheus_client is not installed.
    """
    if not _PROMETHEUS_AVAILABLE:
        raise RuntimeError(
            "prometheus_client is not installed. "
            "Add 'prometheus-client' to requirements.txt."
        )
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


def is_prometheus_available() -> bool:
    """Return True if prometheus_client package is installed."""
    return _PROMETHEUS_AVAILABLE
