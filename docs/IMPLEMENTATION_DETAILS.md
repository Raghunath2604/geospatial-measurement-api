# Implementation Deep Dive (IMPLEMENTATION_DETAILS.md)
## Geospatial File Measurement & Orbital Telemetry Platform

This document provides in-depth technical documentation covering mathematical algorithms, parser internals, memory optimizations, and security architecture.

---

## 1. Geodesic Algorithms & Coordinate Transformations

### 1.1 The Map Projection Dilemma
In Euclidean 2D space, calculating the area of a polygon $P = \{(x_0, y_0), \dots, (x_{n-1}, y_{n-1})\}$ uses the standard Shoelace formula:
$$A = \frac{1}{2} \left| \sum_{i=0}^{n-1} (x_i y_{i+1} - x_{i+1} y_i) \right|$$

When coordinates $(x, y)$ are raw longitude and latitude in degrees ($\text{EPSG:4326}$), applying the Shoelace formula produces square degrees, not square meters. Furthermore, since lines of longitude converge toward the poles ($1^\circ\text{ longitude} = 111.32\text{ km} \times \cos(\phi)$), planar calculations on raw degrees produce errors exceeding **40%** at mid-to-high latitudes.

### 1.2 Dynamic Auto-Zoning Engine
To resolve this without manual user intervention, [`app/services/projection.py`](file:///C:/Users/manu7/OneDrive/Desktop/areo/app/services/projection.py) inspects the unprojected feature:
1. Calculates the geometric centroid $(\bar{\lambda}, \bar{\phi})$.
2. If $|\bar{\phi}| \ge 84^\circ$, selects Universal Polar Stereographic (UPS):
   - $\bar{\phi} \ge 84^\circ \implies \text{EPSG:32661}$ (UPS North)
   - $\bar{\phi} \le -84^\circ \implies \text{EPSG:32761}$ (UPS South)
3. For all other latitudes, calculates the exact Universal Transverse Mercator (UTM) 6-degree longitudinal zone:
   $$\text{Zone} = \left\lfloor \frac{\bar{\lambda} + 180^\circ}{6^\circ} \right\rfloor + 1$$
   $$\text{EPSG} = \begin{cases} 32600 + \text{Zone} & \text{if } \bar{\phi} \ge 0 \\ 32700 + \text{Zone} & \text{if } \bar{\phi} < 0 \end{cases}$$
4. Instantiates a cached `pyproj.Transformer` with `always_xy=True` and transforms coordinates using PROJ C-bindings.
5. Invokes `shapely.area()` and `shapely.length()` on the projected metric geometry.

### 1.3 Karney Ellipsoidal Geodesic Oracle
To guarantee survey-grade validation, [`app/services/measurement.py`](file:///C:/Users/manu7/OneDrive/Desktop/areo/app/services/measurement.py) executes Charles F.F. Karney's algorithms on the WGS 84 ellipsoid ($a = 6378137\text{ m}, 1/f = 298.257223563$):
- Computes geodesic distance along ellipsoidal geodesics (shortest distance on the biaxial ellipsoid) rather than great-circle spheres.
- Computes polygon area using the Karney planimetric polygon area formula, accounting for ellipsoidal flattening.

---

## 2. Ingestion Parsers & Streaming Internals

### 2.1 Shapefile ZIP Parser ([`app/parsers/shapefile_zip.py`](file:///C:/Users/manu7/OneDrive/Desktop/areo/app/parsers/shapefile_zip.py))
- Reads the archive in memory via `io.BytesIO`.
- Inspects the zip directory without decompressing to enforce:
  - Total uncompressed size $\le 200\text{ MB}$.
  - File member count $\le 100$.
- Extracts matching `.shp`, `.shx`, `.dbf`, and optional `.prj` file buffers.
- Parses records via `pyshp.Reader` using custom streams.
- Converts shape records to standard GeoJSON-compatible geometries and WKT representations.

### 2.2 KML / KMZ Parser ([`app/parsers/kml.py`](file:///C:/Users/manu7/OneDrive/Desktop/areo/app/parsers/kml.py))
- For `.kmz` files, extracts `doc.kml` using in-memory zip validation.
- Parses XML using `lxml.etree` with safe configuration:
  - `resolve_entities=False` (prevents XXE attacks)
  - `no_network=True` (prevents external entity fetching)
  - `huge_tree=False` (prevents entity expansion bombs)
- Traverses `<Placemark>` nodes, extracting:
  - `<Point>`, `<LineString>`, `<Polygon>`, `<MultiGeometry>`
  - `<ExtendedData>` key-value dictionaries
  - Coordinate strings (`lon,lat,alt`) with altitude preservation.

### 2.3 GeoJSON & GeoPackage Parsers
- **GeoJSON**: Validates `FeatureCollection`, `Feature`, or bare `Geometry` schemas.
- **GeoPackage (`.gpkg`)**: SQLite-based container parsed via in-memory SQLite connector, extracting WKB geometry blobs and attribute tables according to OGC GeoPackage standard 1.3.

---

## 3. Asynchronous Worker Queue ([`app/workers/task_queue.py`](file:///C:/Users/manu7/OneDrive/Desktop/areo/app/workers/task_queue.py))

- Implemented as an in-memory task queue running within FastAPI's asyncio lifespan.
- Operates a threadpool of background worker coroutines (default concurrency: 4).
- Workflow:
  1. `POST /api/async/ingest/` reads file stream, registers `task_id` in state dictionary (`QUEUED`), and queues item in `asyncio.Queue`.
  2. Available worker dequeues item, updates status to `PROCESSING`.
  3. Executes CPU-bound `IngestService.ingest_stream()` in `asyncio.to_thread()`, preventing event loop starvation.
  4. On completion, writes `COMPLETED` and `file_id` to task state.
  5. On error, writes `FAILED` and error message to task state.

---

## 4. Observability & Security

### 4.1 Prometheus Instrumentation ([`app/monitoring/metrics.py`](file:///C:/Users/manu7/OneDrive/Desktop/areo/app/monitoring/metrics.py))
- **`http_requests_total`**: Counter labeled by `method`, `endpoint`, `status_code`.
- **`http_request_duration_seconds`**: Histogram tracking latency distribution with buckets `[0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 10.0]`.
- **`geospatial_features_processed_total`**: Counter tracking total features parsed and measured.
- **`active_async_tasks`**: Gauge tracking concurrent background tasks.

### 4.2 Security Protocol
- Pre-decompression zip bomb checks (max 200MB, max 100 members).
- 50 MB maximum upload ceiling via streaming chunk validator.
- 10,000 maximum feature limit per archive.
- API Key Bearer authentication (optional via `GEO_API_KEY`).
- Sliding-window rate limiting via SlowAPI (60 req/min).
