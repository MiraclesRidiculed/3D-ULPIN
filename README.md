# V-CAD — 3D ULPIN & Volumetric Cadastre Platform

SIH 2026 prototype for Problem Statement 26011, **3D ULPIN Generation and Vertical Property Mapping System**. It models a property as a footprint plus `z_min` and `z_max`, so apartments, basements, underground utilities and air/elevated rights can be governed as spatial volumes rather than flat map features.

> **Identifier notice:** every identifier displayed as a “Prototype 3D ULPIN” is a deterministic prototype format created for this demo. It is not an official Government of India ULPIN format or government integration.

## What works

- CesiumJS 3D map with selectable parcel, building, apartment/floor volumes and underground corridor.
- Deterministic ULPIN generation from parent, unit identity, spatial centroid and geometry/Z hash.
- FastAPI REST API with generated synthetic records, Pydantic validation and input-size/type checking.
- Real Shapely topology checks for 3D vertical overlaps, parent-parcel encroachment, invalid polygons and utility collisions.
- Command-center interface with ULPIN search, camera focus, vertical inspector, layer/floor isolation, validation panel and backend-sourced analytics.
- A synthetic P-001 / B-001 scene with 8 floors, 16 apartments, a basement and one utility corridor. It deliberately produces overlap, parcel-encroachment and underground-collision findings.

## Architecture

```
frontend/       Next.js + TypeScript + Tailwind + CesiumJS command centre
backend/        FastAPI, Pydantic schemas, deterministic spatial pipeline, Shapely validation
database/       PostGIS logical schema and spatial-ready persistence target
demo-data/      EPSG:4326 seed parcel source
```

The local FastAPI process intentionally uses a deterministic in-memory demo store, so the complete presentation workflow works immediately even when PostGIS is unavailable. Docker starts PostGIS and creates the target schema; production persistence is the next adapter boundary, not a prerequisite for a live demo. The geometry contract is already consistent: GeoJSON footprint + `z_min`/`z_max` + stable geometry hash/version.

## Quick start — Docker

Prerequisites: Docker Desktop.

```bash
docker compose up --build
```

Open [http://localhost:3000](http://localhost:3000). API documentation is at [http://localhost:8000/docs](http://localhost:8000/docs). Use `docker compose down` to stop it; use `docker compose down -v` only if you also intend to discard the local PostGIS data volume.

## Local development

Prerequisites: Python 3.12+ and Node.js 20+.

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

In a second terminal:

```bash
cd frontend
npm install
npm run dev
```

`npm install` copies Cesium runtime assets into `frontend/public/cesium`; no Cesium Ion token is required for this local demo view. Copy the two `.env.example` files to `.env` only when changing defaults.

## Demo workflow

1. Open the command centre and press **Load Demo City**.
2. Press **Generate 3D ULPIN** (or the API endpoint) to regenerate deterministic prototype IDs.
3. Click an apartment volume, choose a floor filter, or search `Apartment 201`, `B-001`, `P-001`, or a displayed `VC-VP-…` ID.
4. Toggle the underground layer and inspect basement/utility at negative elevations.
5. Press **RUN CHECKS**. The UI focuses any selected validation issue on its actual intersecting footprint.
6. Run the three automation adapters to show building extraction (97.4%), floor segmentation (91.8%), and vertical delineation (99.1%) prototype confidence.

## API

| Area | Endpoint |
|---|---|
| Core data | `GET /parcels`, `/buildings`, `/floors`, `/properties`, `/infrastructure` |
| Individual/search | `GET /parcels/{id}`, `/buildings/{id}`, `/properties/{id}`, `GET /ulpin/{ulpin}` |
| Processing | `POST /ulpin/generate`, `/processing/building-extraction`, `/processing/floor-segmentation`, `/processing/vertical-delineation` |
| Validation/analytics | `POST /validation/run`, `GET /validation/issues`, `GET /analytics/summary` |
| Demo/import | `GET /demo/load`, `GET /data-sources`, `POST /import/geojson`, `POST /import/source` |

## Validation model

Volume intersection is calculated as `Shapely(footprint A ∩ footprint B).area × vertical-overlap`. Parent containment uses actual polygon difference. The scenario exposes: apartment 201/202 overlap, a floor-5 apartment outside P-001, and the corridor colliding with the basement. The API returns the conflict footprint and measurement so the viewer can focus it.

## Interoperability and future scale

The system accepts GeoJSON today and uses EPSG:4326 presentation geometries. The pipeline boundary is designed for GDAL/OGR CRS normalization, GeoPackage, WFS/WMS import/export, DSM/DEM ingestion, PDAL/Open3D point-cloud processing, 3D Tiles publishing and PostGIS spatial indexing. A production implementation would put the generated records in the supplied PostGIS tables, version geometries/audits through migrations, emit paginated spatial-tile APIs, authenticate operator roles, and connect only through formally approved land-record interfaces. No official system connection is claimed.

## Assumptions and limitations

- The extraction/segmentation endpoints are deterministic inference adapters with model/prototype confidence values; they expose the same API boundary as a PyTorch implementation without requiring a large model download.
- The demo uses a local metre-like planar transform for exact validation and emits nearby WGS84 coordinates for Cesium. A production pipeline must select the authoritative projected CRS per survey area.
- PostGIS DDL is provisioned in Docker, while the API’s persistence adapter remains deliberately lightweight for offline demonstration reliability.
- This is a prototype, not a legal cadastre or a source of official ULPIN identifiers.
