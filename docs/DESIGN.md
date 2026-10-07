# Geospatial File Measurement API - Design & Architecture Review

## 1. Requirements Checklist

This checklist restates every requirement specified in the prompt and assignment specification:

1. **Framework & Runtime**:
   - [ ] Python 3.10+ with FastAPI.
   - [ ] Standard library SQLite behind an isolated `Repository` abstraction.
   - [ ] Pure-Python/C-extension spatial stack: `shapely >= 2.0`, `pyproj`, `pyshp` (shapefile), `lxml`.
2. **Accepted File Formats**:
   - [ ] KML (`.kml`) supporting KML 2.0, 2.1, 2.2 schemas.
   - [ ] Shapefile in ZIP (`.zip`), containing `.shp` and `.dbf` (with optional `.prj`, `.shx`, `.cpg`).
   - [ ] Reject all other extensions with HTTP 415.
3. **Core API Endpoints**:
   - [ ] `POST /api/files/`: Upload and process file synchronously, returning HTTP 201 on success or HTTP 422 on parse failure with persisted `FAILED` record.
   - [ ] `GET /api/files/{id}/`: File metadata, status (`COMPLETED`, `FAILED`, `PROCESSING`), CRS, feature count, error, warnings.
   - [ ] `GET /api/files/{id}/measurements/`: Paginated measurements for features, including whole-file SQL-aggregated summary.
   - [ ] `GET /api/files/{id}/features/`: Paginated features returning GeoJSON geometries in source CRS with properties.
   - [ ] `GET /health`: Healthcheck endpoint returning service status.
4. **Feature Extraction**:
   - [ ] Per feature: feature ID/index, geometry type, GeoJSON geometry, source CRS, properties/attributes.
   - [ ] Graceful handling of unsupported geometry types without crashing.
   - [ ] Skip null shapes and malformed placemarks with warning tracking (capped at 50 warnings).
5. **Geospatial Measurement Engine**:
   - [ ] Polygon / MultiPolygon -> calculate area in square metres (holes correctly subtracted).
   - [ ] LineString / MultiLineString -> calculate length in metres.
   - [ ] Point / MultiPoint -> status `NOT_APPLICABLE` (no area or length).
   - [ ] Unsupported geometry types / empty geometries -> status `UNSUPPORTED` with descriptive message.
   - [ ] Geometry calculation exceptions / invalid geometries (e.g. self-intersecting bow-ties) -> status `ERROR` or flagged without silent modification.
   - [ ] No calculations on geographic degrees (EPSG:4326); always project to a metric CRS first.
6. **CRS Selection & Handling**:
   - [ ] KML: Always treated as EPSG:4326.
   - [ ] Shapefile: Extracted from `.prj` ESRI WKT using `pyproj.crs.CRS`.
   - [ ] Missing `.prj`: Fallback to EPSG:4326 only if bounding box coordinates are within `[-180, 180, -90, 90]` (with a warning); otherwise reject with HTTP 422.
   - [ ] Projected inputs (e.g., Web Mercator, State Plane): Reproject to WGS84 to identify centroid, then project to appropriate local metric UTM/UPS zone.
   - [ ] Dynamic metric CRS selection: UTM Zone (`EPSG:32601-32660` for North, `EPSG:32701-32760` for South) based on feature centroid; Universal Polar Stereographic (UPS `EPSG:32661` North / `EPSG:32761` South) for latitudes beyond 84°N / 80°S.
   - [ ] Explicit axis order enforcement using `always_xy=True`.
7. **Security & Limits**:
   - [ ] Streaming upload with chunked size enforcement (HTTP 413 if exceeded).
   - [ ] Reject empty files (HTTP 422).
   - [ ] Zip-slip path traversal prevention.
   - [ ] Zip bomb prevention (decompression byte quota and member count limit).
   - [ ] XXE / XML entity expansion prevention using hardened `lxml` parser.
   - [ ] Configurable feature limit (HTTP 422 if exceeded).
8. **Storage & Transactional Integrity**:
   - [ ] SQLite with WAL mode and `foreign_keys=ON`.
   - [ ] `complete_file()` inserts all features and updates file status atomically in a single transaction.
   - [ ] Isolated short-lived connections per request for thread safety.
9. **Testing & Independent Verification**:
   - [ ] Unit tests for parsers, projection math, measurement engine, repository.
   - [ ] Independent oracle: Compare UTM measurements against geodesic calculations from `pyproj.Geod` across diverse global locations (Bengaluru, Sydney, London, polygons with holes, MultiPolygons, projected sources).
   - [ ] Guard test to guarantee calculations are never performed in degree units (detecting area < 100,000 m² for ~0.2 km² shapes).
   - [ ] Comprehensive error-path testing (bad XML, zip bombs, path traversals, missing files, corrupted geometries).
   - [ ] API integration tests via FastAPI `TestClient`.
10. **Delivery, Documentation & Packaging**:
    - [ ] `Dockerfile` and `.github/workflows/ci.yml`.
    - [ ] Comprehensive `README.md` with captured real execution outputs, architecture, design trade-offs, limitations, learnings, and future scope.
    - [ ] `docs/INTERVIEW_QA.md` with 20 technical deep-dive answers and 5 future improvement plans.
    - [ ] Clean git commit history reflecting progressive development milestones.

---

## 2. Hard Problems and Architectural Solutions

### 2.1 Projected CRS Selection
- **The Challenge**: Geographic coordinates (EPSG:4326 lat/lon) use angular degrees. Measuring Euclidean distance or area directly on lat/lon degrees results in nonsensical numbers (e.g. area in square degrees, where $1^\circ \times 1^\circ$ varies from $\approx 12,300\text{ km}^2$ at the equator to $0$ at the poles). Furthermore, global cylindrical projections like Web Mercator (EPSG:3857) have severe scale distortion away from the equator (Greenland looks bigger than Africa; area distortion is $(1/\cos(\text{lat}))^2$, exceeding $400\%$ at high latitudes).
- **Our Solution**:
  1. For each feature, determine its centroid in WGS84 geographic coordinates (reprojecting from the source CRS if the source is already projected).
  2. Normalize longitude into $[-180, 180)$.
  3. Determine the appropriate conformal projected CRS:
     - If latitude $> 84.0^\circ$ N: Use Universal Polar Stereographic North (`EPSG:32661`).
     - If latitude $< -80.0^\circ$ S: Use Universal Polar Stereographic South (`EPSG:32761`).
     - Otherwise: Calculate UTM zone: $\text{zone} = \lfloor(\text{lon} + 180) / 6\rfloor + 1$. Select `EPSG:32600 + zone` for Northern hemisphere ($\text{lat} \ge 0$) or `EPSG:32700 + zone` for Southern hemisphere ($\text{lat} < 0$).
  4. Transform coordinates using `pyproj.Transformer.from_crs(source_crs, target_crs, always_xy=True)` and evaluate Euclidean area/length on the metric planar coordinates.
- **Rejected Alternative**: Using a single global equal-area projection (such as Mollweide or Gall-Peters) or a single continental projection. Equal-area projections distort angles and shapes, which distorts LineString lengths and introduces significant non-conformal distortion across local drone surveys. Choosing UTM per feature gives $< 0.1\%$ distortion near the central meridian and $< 0.4\%$ at zone edges, which is the standard surveying industry approach.

### 2.2 Source CRS Handling
- **The Challenge**: KML files are strictly specified in WGS84 lon/lat (`EPSG:4326`). Shapefiles, however, store coordinate system definitions in a companion `.prj` file containing ESRI WKT or OGC WKT strings. In the wild, `.prj` files are frequently missing, non-standard, or specify local coordinate systems in US Survey Feet or State Plane coordinates.
- **Our Solution**:
  1. **KML**: Hardcode source CRS as `EPSG:4326`.
  2. **Shapefile with `.prj`**: Parse the WKT string using `pyproj.crs.CRS.from_user_input(wkt_content)`.
  3. **Shapefile without `.prj`**: Inspect the shapefile's overall bounding box (`sf.bbox`). If the bounding box is strictly bounded within $[-180, 180]$ for X and $[-90, 90]$ for Y, assume `EPSG:4326` with an explicit warning recorded in the file record. If coordinates exceed these ranges (e.g., values in hundreds of thousands indicative of projected false eastings/northings), reject the file with HTTP 422 indicating missing `.prj` for projected data.
  4. **Source in Projected Systems (e.g. EPSG:3857, EPSG:32643)**: Rather than assuming the source is already in the "correct" measurement system, inspect the source CRS. If it is already a local UTM zone matching the feature centroid, use it directly; otherwise reproject to WGS84 to identify the optimal local metric UTM/UPS zone before measuring.
- **Rejected Alternative**: Defaulting all missing `.prj` shapefiles blindly to `EPSG:4326`. If a user uploads a State Plane or UTM shapefile lacking a `.prj`, coordinates like `(500000, 4200000)` would be interpreted as degrees, either crashing pyproj or resulting in astronomical geometric errors.

### 2.3 Axis Order (EPSG:4326 lat,lon vs GIS lon,lat)
- **The Challenge**: The OGC and EPSG registries define EPSG:4326 with axis order `(latitude, longitude)`. However, KML, GeoJSON (RFC 7946), and Shapefile formats store coordinates as `(x, y)` which corresponds to `(longitude, latitude)`. Pyproj 2+ defaults to authority-compliant axis ordering, causing silent coordinate swapping if not properly controlled.
- **Our Solution**: Always initialize coordinate transformers with `always_xy=True`:
  `pyproj.Transformer.from_crs(source_crs, target_crs, always_xy=True)`.
  This enforces `(x, y) = (lon, lat)` input and `(x, y) = (easting, northing)` output, preventing axis-swap corruption.
- **Rejected Alternative**: Manually swapping coordinate tuples in Python before passing to transformers. This is fragile, error-prone when dealing with multi-dimensional geometries, and breaks when handling arbitrary source CRSs.

### 2.4 Failure Granularity: Bad Feature vs. Bad File
- **The Challenge**: A single malformed Placemark or unparseable geometry should not crash a survey file containing hundreds of valid drone plots. Conversely, an invalid zip archive or a file containing zero valid features should not be marked as successful.
- **Our Solution**:
  1. **File-level errors** (unsupported file extension, corrupt zip, missing mandatory `.shp`/`.dbf`, unparseable XML root, zero usable placemarks, unresolvable CRS): Fail fast, persist file record with `status=FAILED` and descriptive error message, and return HTTP 422 with the persisted file details.
  2. **Feature-level errors** (unsupported geometry type, self-intersecting polygon, null geometry, missing coordinates): Skip unparseable features with warnings during ingest (capped at 50 warnings to prevent memory blowup), or record the feature measurement with status `NOT_APPLICABLE`, `UNSUPPORTED`, or `ERROR`. The file still completes with `status=COMPLETED`.
- **Rejected Alternative**: Strict all-or-nothing validation (rejecting the entire file if 1 out of 10,000 features has an issue). In drone and GIS operations, real-world data often has incidental null geometries or unsupported annotation types; discarding the entire upload causes major usability frustration.

### 2.5 Sync vs. Async Processing, Storage & Pagination
- **The Challenge**: File parsing and reprojection are CPU-bound. If route handlers are defined as `async def`, CPU-bound loops in shapely/pyproj would block the asyncio event loop, starving all concurrent I/O.
- **Our Solution**: Define FastAPI route handlers as standard synchronous functions (`def upload_file(...)`). FastAPI automatically runs standard `def` routes in its internal threadpool (`anyio.to_thread`), keeping the main event loop responsive. Processing is synchronous from the client's perspective, returning HTTP 201 with `COMPLETED` file status upon response.
- **Database & Pagination**: SQLite with WAL mode (`PRAGMA journal_mode=WAL`) allows concurrent readers while a write occurs. Features and measurements are persisted in normalized tables (`files`, `features`). Endpoints paginate using `limit` and `offset`. Measurement summary statistics (`total_area_sq_m`, `total_length_m`, status counts) are calculated via a single SQL aggregate query over the entire file rather than just the paginated subset.
- **Rejected Alternative**: Background celery/redis worker queue for this MVP. While ideal for massive files (>100MB), adding Redis and an asynchronous worker process adds operational complexity and requires clients to poll. By keeping handlers synchronous in threadpools, we fulfill the prompt requirements cleanly while maintaining architectural boundaries for future async migration.

### 2.6 Security of Untrusted Uploads
- **The Challenge**: Uploaded spatial files can be vectors for XML Entity Expansion (Billion Laughs / XXE), Zip-Slip directory traversal, Zip Bombs (decompression denial of service), and excessive memory consumption.
- **Our Solution**:
  1. **Upload Streaming**: Stream incoming request files in 1 MB chunks to a temporary spool file, tracking total bytes. If the file exceeds `max_upload_bytes` (default 50 MB), abort immediately and return HTTP 413.
  2. **XXE Protection**: Instantiate `lxml.etree.XMLParser` with `resolve_entities=False`, `no_network=True`, `dtd_validation=False`, and `load_dtd=False`.
  3. **Zip-Slip Protection**: When reading `.zip` archives, inspect every member name using `os.path.normpath`. Disallow absolute paths and `..` traversals. Extract only required files (`.shp`, `.dbf`, `.prj`, `.shx`, `.cpg`) to fixed temporary filenames.
  4. **Zip Bomb Protection**: Enforce a maximum uncompressed byte quota (`max_uncompressed_bytes`, default 200 MB) and member count cap (`max_zip_members`, default 100). Sum `ZipInfo.file_size` headers before extraction and track decompressed bytes during read.
  5. **Feature Limit**: Enforce a maximum feature cap (`max_features`, default 10,000). If a file exceeds this count, fail with HTTP 422.
- **Rejected Alternative**: Using Python's standard `zipfile.ZipFile.extractall()` or default `xml.etree.ElementTree`. `extractall()` is vulnerable to zip-slip on older runtimes or misconfigurations, and standard `ElementTree` has had historical vulnerabilities to entity expansion attacks.

---

## 3. Risk List: 10 Failure Modes and Mitigations

| # | Risk | Real-World Impact | Mitigation Strategy |
|---|------|-------------------|---------------------|
| 1 | **Measuring in Degrees** | Area of a 20-hectare field calculated as $0.00002$ instead of $200,000 \text{ m}^2$. | Enforce strict transformation to metric UTM/UPS before invoking Shapely geometry metrics; unit test assertion verifies area is $> 100,000$ for test polygons. |
| 2 | **EPSG Axis Swapping** | Polygons flipped over the diagonal $(x, y) \to (y, x)$, landing in the Indian Ocean. | Always specify `always_xy=True` on every `pyproj.Transformer` instance. |
| 3 | **Zip-Slip Traversal** | Malicious zip archive overwrites host files (`../../evil.sh`). | Validate all member names, reject paths containing `..`, and extract only targeted extensions to designated temp directory. |
| 4 | **XML Entity Expansion (XXE)** | Server crash or SSRF via malicious KML external entities. | Configure `lxml.etree.XMLParser` with `resolve_entities=False` and `no_network=True`. |
| 5 | **Zip Decompression Bomb** | 10 KB zip file unpacks to 50 GB, exhausting disk and memory. | Inspect zip member headers and enforce strict byte decompression quotas during extraction. |
| 6 | **Invalid Geometries (Bow-ties)** | Self-intersecting polygons produce incorrect areas or crash spatial operations. | Validate geometries with `shapely.is_valid`; record descriptive warning/message in feature measurement rather than silently distorting geometry. |
| 7 | **Holes Counted as Additive Area** | Polygons with cutouts (donuts) have hole areas added rather than subtracted. | Shapely `Polygon(exterior, interiors).area` automatically subtracts hole areas; verify with dedicated test oracle comparing against donut ground truth. |
| 8 | **Memory Leaks / Blocking Event Loop** | Large file uploads block asynchronous web server, causing timeouts. | Stream file uploads in 1MB chunks and execute CPU-intensive parsing/measurements in threadpool via standard `def` routes. |
| 9 | **Database Inconsistency on Partial Failure** | File record left in `PROCESSING` or half-inserted features if worker crashes. | Wrap feature persistence and status update inside a single atomic SQLite transaction in `complete_file()`. |
| 10 | **Unclear or Untested Documentation** | Hiring reviewer attempts to run instructions in README and encounters missing dependencies or broken commands. | Strictly test every command in a clean environment, capture real output, and include verified copy-paste snippets. |

---

## 4. Test Strategy & Independent Verification

### 4.1 Independent Mathematical Oracle
To prove that our coordinate transformations and area/length calculations are mathematically sound and not biased by implementation bugs, we test against `pyproj.Geod` (geodesic calculations on the WGS84 ellipsoid):
- **Why Geod?** `pyproj.Geod` implements Karney's algorithms for geodesic distance and polygon area directly on the ellipsoid without projecting to a planar 2D grid.
- **Expected Divergence**: UTM projections are conformal (preserving angles locally) but introduce a small scale factor: $k_0 = 0.9996$ at the central meridian, increasing to $\approx 1.0010$ at zone edges ($6^\circ$ width). Therefore, planar UTM area and geodesic area are expected to agree within $\pm 0.5\%$ ($0.005$ relative tolerance) for features located within their proper UTM zone.
- **Test Scenarios**:
  1. Bengaluru, India (Zone 43N) - Equator-adjacent tropical zone.
  2. Sydney, Australia (Zone 56S) - Southern hemisphere mid-latitude.
  3. London, UK (Zone 30N) - Northern hemisphere high mid-latitude.
  4. Donut Polygon (Exterior square with an interior cutout hole).
  5. MultiPolygon (Disjoint plots measured and summed).
  6. Source already projected (EPSG:32643 UTM and EPSG:3857 Web Mercator inputs).

### 4.2 Degree Measurement Guard Test
A dedicated regression test computes the area of a known $\sim 0.2\text{ km}^2$ polygon. The test asserts that `area_sq_m > 100,000` and fails emphatically if the returned value is anywhere near $\approx 2 \times 10^{-5}$ (which indicates square degrees).

### 4.3 Hostile Input & Boundary Condition Testing
We test:
- Not valid XML / truncated KML.
- Missing KML Placemarks / root element mismatch.
- Out-of-bounds geographic coordinates ($> 180^\circ$ or $> 90^\circ$).
- Empty file (0 bytes).
- Oversized file (> configured maximum).
- Non-geospatial file extensions (`.txt`, `.json`, `.exe`).
- Corrupted zip file.
- Zip missing `.shp` or missing `.dbf`.
- Zip containing multiple `.shp` files (ambiguous input).
- Zip with malicious directory traversal (`../../etc/passwd`).
- Zip bomb exceeding decompression size quota.
- Shapefile missing `.prj` with lat/lon coordinates (should succeed with warning).
- Shapefile missing `.prj` with large projected coordinates (must reject with 422).
- Malicious KML with XXE entity expansion.
- Unsupported geometries (e.g. `GeometryCollection`).
- Self-intersecting (bow-tie) polygon.

### 4.4 API Integration Testing
Using `fastapi.testclient.TestClient`:
- Verify all endpoints: `POST /api/files/`, `GET /api/files/{id}/`, `GET /api/files/{id}/measurements/`, `GET /api/files/{id}/features/`, `GET /health`.
- Test pagination (`limit`, `offset`) on features and measurements.
- Assert that `summary` metadata in measurements reflects the whole file regardless of the pagination page size.

---

## 5. Notes on Design Trade-offs & Ambiguities

1. **Features spanning across UTM zones**:
   - Drone surveys typically span hundreds of metres to a few kilometres, staying comfortably inside a single $6^\circ$ UTM zone ($\approx 660\text{ km}$ wide at the equator). For regional features that cross zone boundaries, choosing the UTM zone of the centroid provides acceptable accuracy (distortion $< 1-2\%$ for adjacent zones). In future iterations, features spanning $>3^\circ$ could trigger an equal-area projection or geodesic measurement.
2. **KML MultiGeometry Homogeneity**:
   - KML allows `MultiGeometry` containing mixed types (e.g., a Point and a Polygon together). Standard GeoJSON represents these as `GeometryCollection`. Our measurement engine handles homogeneous groups as `MultiPolygon` or `MultiLineString` and marks heterogeneous collections as `UNSUPPORTED` with a clear explanation, as computing a single unified area/length metric for a mixed point-and-polygon feature is semantically ambiguous.
3. **Shapefile Encoding**:
   - Shapefiles traditionally lack character encoding metadata unless a `.cpg` file is present. Our parser defaults to UTF-8 with fallback to Latin-1/CP1252 to avoid crashes on accented property names.
