# Technical Requirements Document (TRD)
## Geospatial File Measurement & Orbital Telemetry Platform

**Document Version:** 1.1.0  
**Status:** Approved & Implemented  
**Lead Engineer:** Raghunath ([@Raghunath2604](https://github.com/Raghunath2604))  

---

## 1. System Architecture Overview

The system is constructed as a decoupled, asynchronous-capable geospatial platform consisting of:
1. **API & Orchestration Layer**: FastAPI running with an asynchronous event loop for non-blocking I/O routes and executing CPU-intensive geodetic computations in a managed Python threadpool.
2. **Spatial Processing Kernel**: C-optimized geometry pipeline leveraging **Shapely ≥ 2.0** (GEOS 3.12+ C-API), **pyproj** (PROJ 9.3+ geodetic library), and **pyshp**.
3. **Storage Engine**: SQLite in Write-Ahead Logging (`WAL`) mode with indexing across `file_id`, `feature_index`, and `created_at`.
4. **Asynchronous Worker Queue**: In-memory threadpool queue (`task_queue`) executing background workers with concurrency limits.
5. **Observability & Security Layer**: Prometheus metrics exporter, SlowAPI sliding-window rate limiter, and optional Bearer API Key middleware.
6. **Web Client Cockpit**: Single-Page Application (SPA) built with Vanilla JavaScript, Leaflet 1.9, HTML5 Canvas, and modern Neumorphic CSS.

```
┌────────────────────────────────────────────────────────────────────────┐
│                        CLIENT / USER AGENT                             │
│  Tactical GIS Cockpit  │  Telemetry Waveforms  │  SpaceX Mission Deck  │
└────────────────────────────────────┬───────────────────────────────────┘
                                     │ HTTP / REST / JSON / Tiles
                                     ▼
┌────────────────────────────────────────────────────────────────────────┐
│                   FASTAPI APPLICATION GATEWAY                          │
│  Rate Limiter  │  Auth Middleware  │  Metrics Collector  │ Tile Proxy  │
└──────────────┬─────────────────────┬───────────────────────────┬───────┘
               │ Sync                │ Async Submit              │ Read
               ▼                     ▼                           ▼
┌──────────────────────────┐  ┌──────────────────┐  ┌────────────────────┐
│   INGESTION SERVICE      │  │ TASK QUEUE POOL  │  │   STORAGE ENGINE   │
│ Stream Validation        │  │ Worker Pool (4x) │  │ SQLite (WAL Mode)  │
│ Pre-Decompress Quota     │  │ Task State Store │  │ Files & Features   │
│ Dynamic UTM Reprojection │  └────────┬─────────┘  └────────────────────┘
│ Karney Geodesic Oracle   │           │ Background Ingest
└──────────────┬───────────┘           │
               └───────────────────────┘
```

---

## 2. Core Dependencies & Technology Stack

| Layer | Library / Tool | Version | Technical Justification |
| :--- | :--- | :--- | :--- |
| **Framework** | `FastAPI` | `≥ 0.110.0` | High-throughput ASGI framework with native Pydantic v2 validation. |
| **Server Engine** | `Uvicorn` | `≥ 0.28.0` | Lightning-fast ASGI web server implementation. |
| **Geometry Kernel** | `Shapely` | `≥ 2.0.3` | GEOS 3.12+ C-API bindings with vectorized NumPy/C array operations. |
| **Cartographic CRS** | `pyproj` | `≥ 3.6.1` | PROJ 9.3+ geodetic transforms with dynamic transverse Mercator algorithms. |
| **Shapefile Parser** | `pyshp` | `≥ 2.3.1` | Pure-Python Shapefile reader/writer with zero native compile dependencies. |
| **KML/XML Parser** | `lxml` | `≥ 5.1.0` | C-based libxml2 parser supporting high-speed streaming iterparse. |
| **Metrics** | `prometheus-client` | `≥ 0.20.0` | Standard Prometheus metric registry for scrape target `/metrics`. |
| **Rate Limiting** | `slowapi` | `≥ 0.1.9` | Sliding-window IP-based rate limiting built on limits. |
| **Client GIS** | `Leaflet` | `1.9.4` | Ultra-lightweight interactive vector mapping library. |

---

## 3. Mathematical & Geodesic Calculation Engine

### 3.1 Dynamic Metric Projection (Local UTM / UPS)
Given a geometry $G$ in geographic coordinates ($\text{EPSG:4326}$), its centroid $(\lambda, \phi)$ in decimal degrees is derived:
$$\lambda = \frac{1}{N}\sum x_i, \quad \phi = \frac{1}{N}\sum y_i$$

1. **Polar Check**: If $|\phi| \ge 84^\circ$, project to Universal Polar Stereographic (UPS):
   - North: `EPSG:32661` ($\phi \ge 84^\circ$)
   - South: `EPSG:32761` ($\phi \le -84^\circ$)
2. **UTM Check**: For $-80^\circ \le \phi < 84^\circ$, compute the UTM zone:
   $$\text{Zone} = \lfloor \frac{\lambda + 180}{6} \rfloor + 1$$
   - Northern Hemisphere ($\phi \ge 0$): `EPSG:32600 + Zone`
   - Southern Hemisphere ($\phi < 0$): `EPSG:32700 + Zone`

### 3.2 Projected Planar Computation
The geometry coordinates are transformed via PROJ:
$$T: (\text{EPSG:4326}) \to (\text{EPSG:Local UTM})$$
- Planar Area: $A_{\text{planar}} = \text{Shapely.area}(T(G))$ in $\text{m}^2$
- Planar Length: $L_{\text{planar}} = \text{Shapely.length}(T(G))$ in $\text{m}$

### 3.3 Karney Ellipsoidal Geodesics (WGS 84)
For extreme verification, geodesic distance and polygon area are calculated using Charles F.F. Karney's algorithms:
- Ellipsoid: WGS 84 ($a = 6378137.0\text{ m}$, $f = 1/298.257223563$)
- `pyproj.Geod(ellps="WGS84").geometry_area_perimeter(G)`
- Computes geodesic polygon area and perimeter along the shortest paths on the biaxial ellipsoid.

---

## 4. Database Schema (SQLite WAL)

### Table: `files`
| Column | Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `id` | `TEXT` | `PRIMARY KEY` | 32-character hexadecimal UUID |
| `filename` | `TEXT` | `NOT NULL` | Original uploaded filename |
| `format` | `TEXT` | `NOT NULL` | Detected format: `kml`, `kmz`, `shapefile`, `geojson`, `gpkg` |
| `status` | `TEXT` | `NOT NULL` | `PENDING`, `COMPLETED`, `FAILED` |
| `size_bytes` | `INTEGER` | `NOT NULL` | Input file size |
| `feature_count` | `INTEGER` | `DEFAULT 0` | Total features ingested |
| `created_at` | `TIMESTAMP` | `DEFAULT CURRENT_TIMESTAMP` | Ingestion timestamp |
| `error_message`| `TEXT` | `NULL` | Ingestion failure cause if failed |

### Table: `features`
| Column | Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `id` | `INTEGER` | `PRIMARY KEY AUTOINCREMENT` | Internal primary key |
| `file_id` | `TEXT` | `REFERENCES files(id) ON DELETE CASCADE` | Parent file ID |
| `feature_index`| `INTEGER` | `NOT NULL` | 0-indexed position in file |
| `geometry_type`| `TEXT` | `NOT NULL` | `Point`, `LineString`, `Polygon`, etc. |
| `geometry_wkt` | `TEXT` | `NOT NULL` | WGS 84 geometry representation |
| `properties_json`| `TEXT` | `DEFAULT '{}'` | Metadata properties dictionary |
| `measurement_status` | `TEXT` | `NOT NULL` | `OK` or `FAILED` |
| `measurement_crs` | `TEXT` | `NULL` | Target EPSG code (e.g. `EPSG:32643`) |
| `area_sq_m` | `REAL` | `NULL` | Planar projected area |
| `length_m` | `REAL` | `NULL` | Planar projected perimeter/length |

---

## 5. Security & Ingestion Hardening

1. **Pre-Decompression Quotas (Zip-Bomb Defense)**:
   - Archive files (`.kmz`, `.zip`) are inspected without extracting to disk.
   - Sum of uncompressed member sizes must not exceed $200\text{ MB}$.
   - Number of archive members must not exceed $100$.
2. **Feature Quotas**:
   - Files containing $> 10,000$ features are rejected with HTTP 422 to prevent memory exhaustion.
3. **MIME & Signature Verification**:
   - Shapefile ZIPs must contain valid `.shp`, `.shx`, and `.dbf` components.
   - KML files validated against XML schema using `lxml` with external entity resolution disabled (`resolve_entities=False`) to prevent XML External Entity (XXE) attacks.
4. **Zero-Secrets Protocol**:
   - Upstream third-party API credentials (`MAPBOX_ACCESS_TOKEN`, `OPENTOPOGRAPHY_API_KEY`) reside exclusively in server `.env`.
   - Client requests are proxied via `/api/files/tiles/mapbox/` with automated Esri optical fallback.

---

## 6. API Interface Specification

| Method | Endpoint | Description | Status Codes |
| :--- | :--- | :--- | :--- |
| `POST` | `/api/files/` | Synchronous file upload & measurement | 201, 413, 415, 422 |
| `GET` | `/api/files/` | List all ingested files (paginated) | 200 |
| `GET` | `/api/files/latest` | Get latest completed dataset | 200, 404 |
| `GET` | `/api/files/{id}/` | File metadata & ingestion status | 200, 404 |
| `GET` | `/api/files/{id}/features/` | GeoJSON FeatureCollection of dataset | 200, 404, 409 |
| `GET` | `/api/files/{id}/measurements/`| Paginated feature measurements & summary | 200, 404, 409 |
| `GET` | `/api/files/{id}/export/geojson/` | Download measured GeoJSON archive | 200, 404 |
| `GET` | `/api/files/{id}/export/csv/` | Download measurements CSV | 200, 404 |
| `GET` | `/api/files/{id}/export/kml/` | Download KML 2.2 with ExtendedData | 200, 404 |
| `POST` | `/api/async/ingest/` | Background task submission | 202, 413, 415 |
| `GET` | `/api/tasks/{task_id}/` | Poll background ingestion task status | 200, 404 |
| `POST` | `/api/measure/geometry/` | Real-time live digitized geometry measurement | 200, 422 |
| `GET` | `/api/files/config/` | System limits, author attribution & settings | 200 |
| `GET` | `/health` | Application liveness probe | 200 |
| `GET` | `/metrics` | Prometheus metrics scrape endpoint | 200 |
