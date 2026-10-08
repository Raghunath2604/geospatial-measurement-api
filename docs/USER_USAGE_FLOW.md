# End-User Step-by-Step Usage Guide (USER_USAGE_FLOW.md)
## Geospatial File Measurement & Orbital Telemetry Platform

This guide outlines step-by-step instructions for everyday users, GIS surveyors, and mission operators.

---

## 1. Quick Start (Running Locally)

### Option A: Local Python Virtual Environment
1. Ensure Python 3.10+ is installed.
2. Clone and navigate to the project directory:
   ```bash
   git clone https://github.com/Raghunath2604/geospatial-measurement-api.git
   cd geospatial-measurement-api
   ```
3. Create and activate a virtual environment:
   ```bash
   python -m venv .venv
   # Windows PowerShell:
   .\.venv\Scripts\Activate.ps1
   # Linux/macOS:
   source .venv/bin/activate
   ```
4. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
5. Launch the application server:
   ```bash
   uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000 --reload
   ```
6. Open your browser at **`http://127.0.0.1:8000/`**.

### Option B: One-Command Docker Compose
```bash
docker compose up --build
```
Access the application at `http://localhost:8000/`.

---

## 2. Ingesting Geospatial Files

1. Click the **"⬆ Ingest"** button in the top navigation header or sidebar rail.
2. In the modal that appears:
   - **Upload Tab**: Drag and drop any `.kml`, `.kmz`, `.zip` (Shapefile archive), `.geojson`, or `.gpkg` file.
   - For files larger than 10 MB, check the **"Background Queue Mode (async)"** checkbox to process asynchronously.
3. Watch the real-time progress bar stream your file (`0% → 100%`).
4. On completion:
   - The map automatically centers and fits bounds to your survey features.
   - The **Survey Registry Drawer** opens on the right, listing all individual parcels with their calculated areas and perimeters.
   - Click the **File History** tab inside the modal at any time to reload previously uploaded survey files with one click.

---

## 3. Interactive Vector Digitizing (Surveying on Map)

Use the top-right tool ribbon or keyboard shortcuts:

| Tool | Keyboard Shortcut | Action |
| :--- | :---: | :--- |
| **Polygon** | `P` | Click points to draw parcel boundary. Double-click to complete. Calculates planar area and Karney geodesic area. |
| **Line** | `L` | Click points along a path or corridor. Double-click to complete. Calculates survey length. |
| **Circle / Buffer** | `B` | Click center point, use radius slider (10m to 5,000m) to generate radial buffer zone. |
| **GPS Fix** | `X` | Queries device GPS and drops a fix marker with accuracy radius. |
| **Cancel / Exit** | `ESC` | Cancels active drawing mode. |

---

## 4. Switching Basemaps & Satellite Imagery

In the bottom-left corner of the Cockpit view:
- **Carto Dark**: High-contrast tactical dark basemap (fast, adblock-immune via Fastly CDN).
- **Esri Sat**: Multispectral optical sub-meter aerial satellite photography.
- **Streets**: OpenStreetMap road and street network.
- **Mapbox HD**: 512px @2x retina satellite stream (with automatic Esri optical fallback).

---

## 5. Exporting Measurements & Surveys

1. Open the **Survey Registry Drawer** (click the folder icon on the left sidebar).
2. At the top of the drawer, under **"Ingested Archive Export"**, choose your format:
   - **GeoJSON**: Full FeatureCollection containing geometry coordinates, feature properties, planar metrics, and WGS 84 ellipsoidal geodesics.
   - **CSV**: Tabular comma-separated values file listing feature index, geometry type, area in $m^2$, and length in meters.
   - **KML**: RFC-compliant Google Earth KML 2.2 file with embedded `<ExtendedData>` tags containing survey measurements.

---

## 6. SpaceX Satellite Fleet Operations

1. Click the **SpaceX Fleet** pill in the top header (or rocket icon on the left sidebar).
2. You will enter the **SpaceX Orbital Mission Control Deck**:
   - **Global Orbital Map**: Observe real-time Low Earth Orbit satellites (**Aurora-1**, **Starlink-17524**, **Sentinel-2B**, **ISS**) moving along their true orbital inclinations.
   - **Select Satellite Target**: Click any satellite button on the left panel to lock camera tracking and focus telemetry.
   - **Simulation Controls**: Use the control pill at the bottom-right of the map to **Pause**, run at **5x** or **20x** speed, or **Reset Camera**.
   - **RF Oscilloscope**: Observe the live Ku-band RF telemetry waveform monitor (14.25 GHz with sync lock and SNR metrics).

---

## 7. Metrics & API Documentation
- **Interactive Swagger OpenAPI Docs**: `http://127.0.0.1:8000/docs`
- **ReDoc Documentation**: `http://127.0.0.1:8000/redoc`
- **Prometheus Metrics**: `http://127.0.0.1:8000/metrics`
- **Service Health Probe**: `http://127.0.0.1:8000/health`
