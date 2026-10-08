# Application Flow & Data Architecture (APP_FLOW.md)
## Geospatial File Measurement & Orbital Telemetry Platform

This document describes the complete runtime lifecycle, data transformations, asynchronous job scheduling, and export flows of the platform.

---

## 1. End-to-End System Pipeline

```mermaid
graph TD
    A[Client User Interface] -->|Upload File .kml, .kmz, .zip, .geojson, .gpkg| B(FastAPI Gateway)
    
    subgraph Security & Validation
        B --> C{Size <= 50MB?}
        C -->|No| E1[HTTP 413 File Too Large]
        C -->|Yes| D{Allowed Extension?}
        D -->|No| E2[HTTP 415 Unsupported Media]
        D -->|Yes| E[MIME & Archive Quota Check]
        E -->|Uncompressed > 200MB| E3[HTTP 422 Archive Too Large]
    end

    subgraph Dual Ingestion Pathways
        E --> F{Sync or Async?}
        F -->|Sync| G[IngestService in Threadpool]
        F -->|Async| H[Submit to InMemory TaskQueue]
        H -->|Return 202| I[Client Polls /api/tasks/{id}/]
        H -.->|Background Worker| G
    end

    subgraph Processing Kernel
        G --> J[Format Parser: KML/KMZ/Shapefile/GeoJSON/GPKG]
        J --> K[Iterate Features WGS 84]
        K --> L[Calculate Feature Centroid]
        L --> M[Determine Local Metric UTM / UPS Zone]
        M --> N[Reproject Geometry via pyproj]
        N --> O[Compute Planar Area & Length via Shapely 2.0]
        O --> P[Compute Karney Geodesic Verification]
        P --> Q[Batch Insert into SQLite DB]
    end

    subgraph Presentation & Exports
        Q --> R[Update Workspace Map & Feature Registry]
        R --> S[Export GeoJSON]
        R --> T[Export CSV]
        R --> U[Export KML 2.2 ExtendedData]
    end
```

---

## 2. Ingestion Sequence Flow (Synchronous Mode)

```mermaid
sequenceDiagram
    autonumber
    actor User as Client Browser
    participant API as FastAPI Router
    participant Service as IngestService
    participant Parser as Format Parser (KML/SHP/GPKG)
    participant Proj as Projection / Geodesic Engine
    participant Repo as SQLite Repository

    User->>API: POST /api/files/ (multipart file upload)
    API->>API: Validate file size (<= 50MB) and extension
    API->>Service: ingest_stream(file_stream, filename)
    Service->>Parser: Parse file and extract geometry records
    Parser-->>Service: Yield List[RawFeature]
    Service->>Service: Check feature quota (<= 10,000 features)
    
    loop For Each Geometry Feature
        Service->>Proj: Get local metric UTM/UPS CRS from centroid
        Proj-->>Service: Return EPSG (e.g. EPSG:32643)
        Service->>Proj: Transform geometry WGS84 -> UTM
        Proj-->>Service: Projected Geometry
        Service->>Service: Calculate planar area (m²) and length (m)
        Service->>Proj: Calculate Karney ellipsoidal geodesics
        Proj-->>Service: Geodesic area and length
    end

    Service->>Repo: create_file(record) + insert_features(batch)
    Repo-->>Service: Commit transaction (WAL Mode)
    Service-->>API: Return Completed FileRecord
    API-->>User: 201 Created (FileInfo with summary metrics)
    User->>User: Auto-render GeoJSON on Leaflet Map
```

---

## 3. Asynchronous Queue & Polling Sequence Flow

```mermaid
sequenceDiagram
    autonumber
    actor User as Client Browser
    participant API as FastAPI Router
    participant Queue as TaskQueue (Worker Pool)
    participant Worker as Background Ingest Worker
    participant Repo as SQLite Repository

    User->>API: POST /api/async/ingest/ (multipart file)
    API->>Queue: submit(file_bytes, filename)
    Queue-->>API: Return task_id (UUID4)
    API-->>User: 202 Accepted { "task_id": "...", "status": "QUEUED" }

    par Background Execution
        Queue->>Worker: Dispatch task to available threadpool worker
        Worker->>Worker: Execute IngestService pipeline
        Worker->>Repo: Persist file and measured features
        Worker->>Queue: Mark task COMPLETED with file_id
    and Client Polling Loop
        loop Every 1.5 Seconds
            User->>API: GET /api/tasks/{task_id}/
            API->>Queue: get_status(task_id)
            Queue-->>API: Return TaskResponse
            API-->>User: Status ("QUEUED" / "PROCESSING")
        end
        User->>API: GET /api/tasks/{task_id}/
        API-->>User: Status "COMPLETED", file_id: "..."
        User->>API: GET /api/files/{file_id}/measurements/
        API-->>User: Measurements & Summary payload
    end
```

---

## 4. Interactive Live Vector Digitizing Flow

```mermaid
sequenceDiagram
    autonumber
    actor User as Surveyor / User
    participant Map as Leaflet Interactive Map
    participant ClientJS as Browser Event Engine
    participant MeasureAPI as POST /api/measure/geometry/
    participant Geod as pyproj / Shapely Kernel

    User->>Map: Clicks Polygon / Polyline tool (P / L key)
    User->>Map: Clicks vertices on map canvas
    ClientJS->>ClientJS: Draws dynamic rubber-band polyline
    User->>Map: Double-clicks or clicks first vertex to close
    ClientJS->>MeasureAPI: POST /api/measure/geometry/ { type, coordinates }
    MeasureAPI->>Geod: Project to local UTM + calculate ellipsoidal geodesic
    Geod-->>MeasureAPI: Planar area (m²), length (m), WGS84 geodesic
    MeasureAPI-->>ClientJS: 200 OK { area_sq_m, length_m, measurement_crs }
    ClientJS->>Map: Render polygon with glowing tactical styling
    ClientJS->>User: Update live HUD & Toast readout
```

---

## 5. SpaceX Orbital Fleet Real-Time Engine Flow

```mermaid
sequenceDiagram
    autonumber
    actor User as Operator
    participant UI as SpaceX Mission Control View
    participant Engine as Browser Keplerian Physics Loop
    participant Canvas as HTML5 RF Waveform Canvas
    participant Map as Leaflet Orbital Radar Map

    User->>UI: Selects "SpaceX Fleet" Navigation Pill
    UI->>Map: initSpaceXFleetMap() (Global Dark Projection)
    UI->>Engine: startSpaceXOrbitLoop() (100ms tick)
    
    loop Every 100ms Simulation Tick
        Engine->>Engine: Advance simulation time t += 1 * speedMultiplier
        Engine->>Engine: Calculate sub-satellite lat/lon with Earth rotation compensation
        Engine->>Map: Update satellite marker positions (Aurora-1, Starlink, Sentinel, ISS)
        Engine->>UI: Update Telemetry HUD (Lat, Lon, Altitude, Speed, Doppler Shift)
        Engine->>Canvas: Render Ku-Band RF Waveform (sine + noise harmonics)
    end

    User->>UI: Clicks "Starlink-17524" Target Button
    UI->>Engine: selectSpaceXTarget('starlink')
    Engine->>Map: Pan camera smoothly to Starlink coordinates
    Engine->>UI: Focus HUD readouts on Starlink-17524
```
