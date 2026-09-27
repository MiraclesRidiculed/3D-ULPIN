# V-CAD — 3D ULPIN & Volumetric Cadastre Platform

SIH 2026 prototype for Problem Statement 26011, **3D ULPIN Generation and Vertical Property Mapping System**. It models a property as a footprint plus `z_min` and `z_max`, so apartments, basements, underground utilities and air/elevated rights can be governed as spatial volumes rather than flat map features.

> **Identifier notice:** every identifier displayed as a “Prototype 3D ULPIN” is a deterministic prototype format created for this demo. It is not an official Government of India ULPIN format or government integration.

> **Affiliation notice:** this is a hackathon/research prototype built for
> Problem Statement 26011. It is **not** a Government of India or Department of
> Rural Development (DoLR) system, is **not** affiliated with or endorsed by
> DoLR or any government body, and has no connection to any official land-record
> service. References to the problem statement, DoLR or public-domain cadastral
> concepts are descriptive context for the problem, not a claim of status or
> approval.

## What works

- CesiumJS 3D map with selectable parcel, building, apartment/floor volumes and the underground utility corridor.
- **Stable ULPIN generation from parent parcel identity and unit identity only.** Geometry is deliberately *excluded* from identifier derivation, so re-surveying a property changes its `geometry_hash` and `geometry_version` but never its identifier — see [Identity vs geometry](#identity-vs-geometry).
- **Algorithmic, deterministic** extraction, storey segmentation and property-volume generation. No trained model is loaded and no inference is performed anywhere in the pipeline. The system reports **no accuracy percentage of any kind**; the only numeric score it produces is `geometric_quality`, a *regularity* measure of a recovered shape, which is not an accuracy.
- Real Shapely topology checks for 3D vertical overlaps, parent-parcel encroachment, invalid polygons and utility collisions.
- PostGIS persistence with generated per-location metric companions, an Alembic-owned schema, and byte-identical API responses from both storage backends.
- Point-cloud ingest for LAS/LAZ/PLY: header-only inspection, CRS resolution, extent and point count.
- A human review workflow with an append-only audit trail and immutable decision records.
- Change detection that presents measured differences for human review, never adjudicated findings.
- Command-center interface with ULPIN search, camera focus, vertical inspector, layer/floor isolation, validation panel and backend-sourced analytics.
- A synthetic P-001 / B-001 scene with 8 floors, 16 apartments, a basement and one utility corridor. It deliberately produces overlap, parcel-encroachment and underground-collision findings.

### Identity vs geometry

These are three separate concepts, and conflating them is the classic way to get
a cadastre wrong:

| Concept | What it is | Changes on re-survey? |
|---|---|---|
| `prototype_ulpin` | Stable identity, derived from **parent parcel + unit identity only** | **No** |
| `geometry_hash` | Digest of the current geometry and its Z band | Yes |
| `geometry_version` | Monotonic revision counter for that geometry | Yes |

A geometry change produces a new version and a new hash. It does not produce a
new identifier. `GET /geometry-versions/{id}/compare` reports `ulpin_changed` as
`false` on every comparison, which is the machine-checkable form of that
invariant.

## Architecture

```
frontend/         Next.js + TypeScript + Tailwind + CesiumJS command centre
backend/          FastAPI, Pydantic schemas, deterministic spatial pipeline, Shapely validation
backend/alembic/  11 migrations; the schema is owned here
database/         Database initialisation and supporting assets (enables PostGIS + pgcrypto)
demo-data/        Reference/demo artifact mirroring the seed; not read at runtime
```

**Two storage backends, one contract.** `app/repositories/` holds an in-memory
store and a PostGIS adapter behind the same interface, and both return
byte-identical records. With no `DATABASE_URL` the API runs entirely in memory so
the demo works with no database at all; with `DATABASE_URL` set it uses PostGIS
with one transaction per request. The parity is test-enforced, because the two
backends have silently diverged before.

**PostGIS persistence is complete, not a future boundary.** The current schema is
19 tables, 11 Alembic migrations, 17 GiST indexes and 10 generated metric columns,
with a PostGIS integration suite covering storage parity, geometry round-trips,
metric measurement, review, audit, change detection and provenance.

Every geometry column is `geometry(Polygon,4326)` with a generated companion
holding the same shape in the projected metre CRS selected from its own centroid,
so a Delhi record uses UTM 43N while a London record uses UTM 30N. `ST_Area` on
the 4326 column returns square degrees, which is why the companion exists.

`database/init.sql` only enables the PostGIS and `pgcrypto` extensions — it
creates no tables. Schema ownership is Alembic's alone, and
`alembic revision --autogenerate` producing an empty file is the check that the
two have not drifted.

`demo-data/demo-city.geojson` mirrors the seeded scene for reference and
coordinate checking. **The backend opens no files at all**; the demo city is
constructed in code.

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
2. Press **Generate 3D ULPIN** (or the API endpoint) to issue deterministic prototype identifiers. Re-running it issues no new identifiers, because identity is already assigned.
3. Click an apartment volume, choose a floor filter, or search `Apartment 201`, `B-001`, `P-001`, or a displayed `VC-VP-…` ID.
4. Toggle the underground layer to reveal the utility corridor `INF-U-001` at negative elevations, and relate it to the `VAL-INF-PV-B001` collision finding.
5. Press **RUN CHECKS**. The UI focuses any selected validation issue on its actual intersecting footprint.
6. Run a point-cloud through the real pipeline — **Import** a LAS/LAZ/PLY file, then run building extraction, storey segmentation and property-volume generation from the workflow rail. Each stage reports what it did *and what it could not establish*: `geometric_quality` (a regularity measure), a `requires_human_review` flag with specific reasons, and `volume_scope` / `units_inferred` so an undivided storey is never presented as a known apartment boundary.

### What the processing endpoints do and do not report

`POST /processing/building-extraction`, `/processing/floor-segmentation` and
`/processing/vertical-delineation` are thin **stand-ins** over the real
pipeline. They return a **record count read from the active store** and say so in
their `note`. They load no model, perform no inference, and **report no accuracy,
confidence or score of any kind** — there is no measured accuracy for them to
report.

For real results run the actual pipeline
(`POST /point-clouds/{id}/extract`, `/segment-floors`, `/property-volumes`),
which is deterministic and geometric, and whose only numeric output is
`geometric_quality`.

## API

| Area | Endpoint |
|---|---|
| Core data | `GET /parcels`, `/buildings`, `/floors`, `/properties`, `/infrastructure` |
| Individual/search | `GET /parcels/{id}`, `/buildings/{id}`, `/properties/{id}`, `GET /ulpin/{ulpin}` |
| Processing | `POST /ulpin/generate`, `/processing/building-extraction`, `/processing/floor-segmentation`, `/processing/vertical-delineation` |
| Point-cloud pipeline | `GET /point-clouds`, `/point-clouds/{id}`, `/extracted-buildings`, `/extracted-floors`, `/generated-property-volumes`, `/processing-jobs`; `POST /point-clouds/{id}/extract`, `/segment-floors`, `/property-volumes` |
| Geometry history | `GET /geometry-versions`, `/geometry-versions/{id}`, `/{id}/history`, `/{id}/versions/{n}`, `/{id}/compare` |
| Validation/analytics | `POST /validation/run`, `GET /validation/issues`, `/validation/summary`, `/analytics/summary` |
| Review/audit | `GET /reviews`, `/reviews/{id}`, `/reviews/{id}/decisions`; `POST /reviews`, `/reviews/{id}/approve`, `/reject`, `/request-resurvey`, `/mark-expected`; `GET /audit/events`, `/audit/history/{id}` |
| Change detection | `GET /changes`, `/changes/report`; `POST /changes/compare` |
| Provenance | `GET /provenance/stages`, `/provenance/{id}`, `/{id}/lineage`, `/{id}/sources`, `/{id}/processing` |
| Demo/import | `GET /demo/load`, `GET /data-sources`, `POST /import/geojson`, `POST /import/source` |

## Validation model

Volume intersection is calculated as `Shapely(footprint A ∩ footprint B).area × vertical-overlap`. Parent containment uses actual polygon difference. The scenario exposes: apartment 201/202 overlap, a floor-5 apartment outside P-001, and the corridor colliding with the basement. The API returns the conflict footprint and measurement so the viewer can focus it.

## Interoperability and future scale

The system accepts GeoJSON, CSV, plan JSON and LAS/LAZ/PLY. Point clouds are
genuinely inspected: the header is read for CRS, extent, point count and density,
and the points are retained on disk so extraction can re-read them. GeoJSON, CSV
and plan JSON are **registered only** — they are recorded as a source and are not
parsed into cadastral records.

Storage geometry is WGS84 (EPSG:4326). Metric work selects a projected metre CRS
per location, so there is no fixed UTM zone anywhere in the measurement path.

The following are **not implemented** and are listed as possible future work
only: GDAL/OGR CRS normalisation, GeoPackage, WFS/WMS import/export, DSM/DEM
ingestion, PDAL/Open3D point-cloud processing, 3D Tiles publishing, paginated
spatial-tile APIs, operator authentication and roles, and any connection to a
formally approved land-record interface. No official system connection is
claimed or attempted.

## Assumptions and limitations

- **No model is loaded and no inference is performed anywhere in this pipeline.** Extraction, storey segmentation and property-volume generation are deterministic geometric algorithms: a progressive grid ground filter, DBSAN clustering, a concavity hull, and an elevation-histogram peak search. The only numeric quality output is `geometric_quality`, a *regularity* measure of a recovered shape — not an accuracy, not a probability, and not a confidence.
- **No accuracy percentage is published anywhere**, on the API or in the UI, because none has been measured. A detected change is a measured difference between two observations, not a finding about entitlement or validity; it is presented for human verification.
- **Where no floor-plan unit data is supplied, one volume is emitted per storey**, marked `volume_scope="FLOOR"` with `units_inferred=False`. Apartment boundaries are never invented. `units_inferred` exists so a consumer can verify that claim instead of trusting it.
- **Derived records are stored separately from cadastral records.** `extracted_buildings`, `extracted_floors` and `generated_property_volumes` are distinct tables from `buildings`, `floors` and `property_volumes`; generation never writes the cadastral tables, and no owner, tenant or title is asserted anywhere.
- **A source with no declared CRS is refused, not guessed.** An undeclared point cloud is recorded as `UNKNOWN_CRS` with a reason rather than assumed to be WGS84.
- **Metric measurement is only valid inside the selected projected CRS.** The per-record metric companion is chosen from each geometry's own centroid, which is correct for UTM zones and the polar cases but is not a substitute for a national or authoritative cadastral reference system. None is assumed or claimed.
- **There is no authentication.** Any reviewer name recorded in the audit trail is a self-asserted claim supplied by the caller, not a verified identity, and the API reports `actor_authenticated: false`.
- **The demo scene's three findings are deliberate.** `seed_demo()` is constructed to produce a floor-2 apartment overlap, a floor-5 parcel encroachment and a utility/basement collision. They are showcase payload, not evidence of a broken rule.
- This is a prototype, not a legal cadastre and not a source of official ULPIN identifiers.
