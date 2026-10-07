# Interview Defence Pack: Geospatial File Measurement API

This document prepares the candidate to defend every architectural choice, mathematical nuance, and engineering trade-off made in this repository during technical interview evaluations.

---

## 20 Core Technical Questions & Answers

### 1. Why select a projected UTM CRS dynamically per feature instead of using a global projection like Web Mercator (EPSG:3857)?
**Answer:**
Global projections like Web Mercator (EPSG:3857) are cylindrical conformal projections designed for 2D screen tiling, not metric measurements. Scale distortion in Web Mercator is proportional to $1 / \cos^2(\text{latitude})$, exceeding $400\%$ distortion at $60^\circ\text{N}$. In contrast, Universal Transverse Mercator (UTM) divides the Earth into $6^\circ$ longitudinal zones ($1 \text{ to } 60$), with a central scale factor $k_0 = 0.9996$ that never exceeds $1.0010$ at zone edges ($< 0.4\%$ scale distortion). Choosing the UTM zone of each feature's centroid guarantees surveying-grade metric accuracy ($< 0.5\%$ error) regardless of where on Earth the drone mission took place.

### 2. Why reproject inputs that are already in a projected CRS (such as State Plane or Web Mercator)?
**Answer:**
If an uploaded file is in Web Mercator (`EPSG:3857`), measuring directly yields wildly inflated planar numbers. If an input is in US State Plane (NAD83), coordinates are frequently in US Survey Feet ($1\text{ ft} \approx 0.3048006\text{ m}$), which would produce erroneous measurements if assumed to be metric. Reprojecting via WGS84 coordinates to a verified metric UTM zone standardizes all calculations into square metres ($\text{m}^2$) and linear metres ($\text{m}$).

### 3. What does `always_xy=True` fix in pyproj?
**Answer:**
By default, modern PROJ/pyproj adheres to EPSG authority axis ordering. The EPSG registry defines `EPSG:4326` as `(latitude, longitude)` (Northing, Easting). However, geospatial data standards (GeoJSON RFC 7946, KML, ESRI Shapefiles) store coordinates as `(x, y) = (longitude, latitude)`. Without `always_xy=True`, `Transformer.from_crs` treats input coordinates according to authority definition, silently swapping $(x, y) \to (y, x)$. This flips drone boundaries across the diagonal into distant oceans. Setting `always_xy=True` explicitly enforces `(x, y) = (easting/lon, northing/lat)` everywhere.

### 4. How is XML External Entity (XXE) expansion prevented in the KML parser?
**Answer:**
In `app/parsers/kml.py`, we instantiate `lxml.etree.XMLParser` with explicit security flags:
```python
parser = etree.XMLParser(
    resolve_entities=False,
    no_network=True,
    dtd_validation=False,
    load_dtd=False,
)
```
This blocks billion-laughs XML entity bombs, local filesystem file disclosures (`<!ENTITY xxe SYSTEM "file:///etc/passwd">`), and Server-Side Request Forgery (SSRF) over network DTD lookups.

### 5. Why are the FastAPI route handlers declared as standard `def` instead of `async def`?
**Answer:**
Spatial parsing (`pyshp`, `lxml`) and coordinate transformations (`pyproj`, `shapely`) are pure CPU-bound operations. If a route is declared `async def`, it executes directly on the asyncio single-threaded event loop. Intensive geometry math would block the event loop, starving concurrent I/O requests and health checks. Defining routes with standard `def` causes FastAPI to offload execution to its internal threadpool (`anyio.to_thread.run_sync`), keeping the main asyncio loop non-blocking.

### 6. Why are feature insertions and status updates executed in a single atomic SQLite transaction in `complete_file`?
**Answer:**
If features were inserted row-by-row with autocommit, a server failure or crash halfway through a 1,000-feature file would leave a "zombie" file in `PROCESSING` status with partial orphaned feature records. In `app/storage/repository.py`, `complete_file` uses `BEGIN IMMEDIATE;`, executes `executemany` for all features, flips `files.status = 'COMPLETED'` with metadata, and commits in one atomic step. On error, it rolls back completely, ensuring relational consistency.

### 7. What happens if a polygon spans across two UTM zones, and what would you do for global-scale features?
**Answer:**
In our implementation, the centroid determines the zone (e.g. Zone 43N). For drone surveys spanning several hundred metres to a few kilometres, the distortion across zone boundaries is negligible ($< 1-2\%$). However, for continental-scale polygons spanning $> 6^\circ$, planar distortion increases. The mitigation for continental features is to detect when the longitudinal extent exceeds $3^\circ$ and dynamically switch to either an equal-area projection (such as Albers Equal Area or Lambert Azimuthal Equal Area centered on the centroid) or compute geodesic ellipsoidal metrics directly via `pyproj.Geod`.

### 8. How did you independently prove that your area and distance calculations are correct?
**Answer:**
Rather than trusting the code or Shapely's planar functions blindly, `tests/test_measurement_oracle.py` uses `pyproj.Geod(ellps="WGS84")` as an independent mathematical oracle. `Geod` computes geodesic distances and areas directly on the WGS84 reference ellipsoid using Charles Karney’s algorithms. We verified that our UTM planar area matches geodesic area within $0.5\%$ relative tolerance across diverse real-world locations (Bengaluru Zone 43N, Sydney Zone 56S, London Zone 30N).

### 9. What is the "degree measurement trap", and how do you guard against it?
**Answer:**
Computing Euclidean area on geographic degrees (`dx * dy`) yields square degrees ($\text{deg}^2$). Near the equator, $1^\circ \times 1^\circ \approx 12,300\text{ km}^2$, but a 20-hectare field ($\approx 0.2\text{ km}^2$) would evaluate to $\approx 0.00002\text{ deg}^2$. In `test_degree_measurement_guard()`, we measure a $\sim 0.2\text{ km}^2$ test polygon and assert that `area_sq_m > 100,000.0`. If any developer accidentally removes the projection transformer, this test fails immediately.

### 10. How does the system handle a Shapefile uploaded without a `.prj` file?
**Answer:**
In `app/parsers/shapefile_zip.py`, when `.prj` is missing, we inspect the overall bounding box (`reader.bbox`). If `min_x, min_y, max_x, max_y` fall strictly within $[-180, 180]$ and $[-90, 90]$, we assume `EPSG:4326` and log an explicit warning in the file record. If coordinates exceed these bounds (e.g., coordinates in the hundreds of thousands), we reject the upload with HTTP 422, because guessing a local projected grid or State Plane system without metadata would produce corrupted measurements.

### 11. How does the system handle interior rings (holes / donuts) in polygons?
**Answer:**
In KML, `<innerBoundaryIs>` tags define holes; in Shapefiles, secondary parts with counter-clockwise winding define inner rings. In `app/services/measurement.py`, Shapely's `Polygon(exterior, interiors).area` automatically subtracts the area of interior rings from the exterior shell. Interestingly, our test oracle proved that `pyproj.Geod` requires separate computation of exterior minus interior areas depending on coordinate winding order, whereas our UTM planar engine handles arbitrary valid holes correctly.

### 12. How does the system handle self-intersecting (bow-tie) invalid polygons?
**Answer:**
Per the assignment specification ("Flag invalid geometries in message; do not silently repair"), we inspect `geom.is_valid` using GEOS. If invalid, we capture the reason via `shapely.validation.explain_validity(geom)` (e.g. `Self-intersection[0.5 0.5]`) and store it in `measurement.message` as:
`"Geometry is invalid (...); measured without silent repair"`.
We never call `shapely.make_valid()`, which would silently modify the user's survey boundary.

### 13. What is the failure granularity strategy: bad feature vs. bad file?
**Answer:**
- **Bad File**: Corrupted zip, missing `.shp`/`.dbf`, unparseable XML root, zero usable placemarks, or unresolvable CRS fail the entire ingestion. The file record is persisted as `FAILED` with error details, and HTTP 422 is returned with the failed file metadata.
- **Bad Feature**: A single malformed Placemark, null geometry, or unsupported geometry (e.g. Point or 3D GeometryCollection) within an otherwise valid file does not fail the file. Points receive `NOT_APPLICABLE`, unsupported shapes receive `UNSUPPORTED`, and calculation exceptions receive `ERROR`. The file transitions to `COMPLETED`.

### 14. How are Zip-Slip and Zip-Bomb attacks mitigated?
**Answer:**
1. **Zip-Slip**: We never invoke `archive.extractall()`. Every `ZipInfo.filename` is inspected with `os.path.normpath()`. Any path starting with `..`, containing absolute separators, or traversing outside the destination raises an HTTP 422.
2. **Zip-Bomb**: We check total file count against `max_zip_members` (default 100), sum `info.file_size` headers against `max_uncompressed_bytes` (default 200 MB), and extract only needed extensions (`.shp`, `.dbf`, `.prj`, `.shx`, `.cpg`) to fixed temporary filenames.

### 15. Why is the measurements summary calculated in SQL rather than over the paginated API response?
**Answer:**
If a file has 1,000 features and the client requests `?limit=10&offset=0`, calculating `total_area_sq_m` and `total_length_m` in Python over the 10 returned features would present misleading numbers for the survey. In `app/storage/repository.py`, `get_measurements_summary()` executes an aggregate query (`COUNT(*)`, `SUM(area_sq_m)`, `SUM(length_m)`) over all features where `file_id = ?`, ensuring the summary remains an accurate representation of the whole file across all paginated pages.

### 16. How is thread safety ensured with SQLite in FastAPI?
**Answer:**
SQLite connection objects cannot be safely shared across different threads. In `app/storage/repository.py`, we implement a connection-per-call pattern via `_get_connection()`, creating a fresh connection per repository call that is closed upon completion. Furthermore, we activate Write-Ahead Logging (`PRAGMA journal_mode = WAL;`) and `PRAGMA busy_timeout = 30000;`, allowing concurrent reader threads without locking the database during writes.

### 17. How is streaming upload size enforced?
**Answer:**
Instead of reading `file.read()` directly into memory (which could exhaust RAM on multi-gigabyte uploads), `IngestService.ingest_stream` reads the file in 1 MB chunks (`chunk_size_bytes`). We track cumulative bytes read; as soon as `total_bytes > max_upload_bytes` (50 MB), the stream is halted and a `PayloadTooLargeError` (HTTP 413) is raised before memory is compromised.

### 18. Why use pure-Python `pyshp` instead of GDAL/Fiona?
**Answer:**
GDAL and Fiona require heavy C/C++ shared libraries (`libgdal`, `PROJ`, `GEOS`) with complex platform-specific installation prerequisites that frequently break Docker builds and local developer setups. `pyshp` is pure Python, zero-dependency, and lightweight. Paired with `shapely >= 2.0` (which wheels compile with GEOS bundled) and `pyproj` (which bundles PROJ), we get the full power of modern GIS math with zero OS-level dependency friction.

### 19. How does the service handle polar features (latitudes > 84°N or < -80°S)?
**Answer:**
Standard UTM zones are only defined between $80^\circ\text{S}$ and $84^\circ\text{N}$. Beyond these limits, Transverse Mercator projections suffer extreme distortions. In `app/services/projection.py`, `utm_epsg_for` dynamically selects the Universal Polar Stereographic (UPS) projection:
- $\text{Latitude} > 84.0^\circ$: UPS North (`EPSG:32661`).
- $\text{Latitude} < -80.0^\circ$: UPS South (`EPSG:32761`).

### 20. How would you migrate this architecture to PostGIS?
**Answer:**
Because all persistence logic is encapsulated behind the `Repository` abstraction (`app/storage/repository.py`), the domain and service layers have zero knowledge of SQLite or SQL dialect:
1. Replace SQLite queries with SQLAlchemy or asyncpg targeting PostgreSQL with the PostGIS extension.
2. Replace JSON geometry text fields with PostGIS `GEOMETRY(Geometry, 4326)` or `GEOGRAPHY` columns.
3. Replace manual UTM reprojection in Python with PostGIS native spatial functions (e.g. `ST_Area(ST_Transform(geom, target_srid))` or `ST_Area(geog)` directly on the spheroid).
4. Add spatial R-Tree indexing (`GIST(geom)`) for bounding box queries.

---

## 5 Things I Would Change / Add with More Time

1. **Asynchronous Worker Queue (Celery / RQ with Redis)**:
   For massive survey files (> 100 MB, > 50,000 features), transition `POST /api/files/` to return HTTP 202 Accepted with a task ID and polling endpoint (`GET /api/tasks/{task_id}`).
2. **Object Storage for Raw Uploads (AWS S3 / GCP GCS / MinIO)**:
   Store original uploaded files in durable object storage rather than discarding after ingestion, enabling file reprocessing, raw download, and audit trails.
3. **Support for KMZ, GeoJSON, and GeoPackage**:
   Extend parser registry to support zipped KML (`.kmz`), GeoJSON FeatureCollections, and OGC GeoPackage (`.gpkg`) files using the existing `ParsedFile` domain abstraction.
4. **Client-Configurable Geodesic vs Planar Mode**:
   Provide a query parameter or header allowing callers to request pure ellipsoidal geodesic measurements (via `pyproj.Geod`) rather than planar UTM projections when analyzing global or continental flight trajectories.
5. **Observability, OpenTelemetry & Prometheus Metrics**:
   Add Prometheus metrics instrumenting file upload sizes, parsing latency, UTM zone distribution, and failure rates, alongside OpenTelemetry tracing for reprojection bottlenecks.
