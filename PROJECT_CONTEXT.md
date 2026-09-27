# Project Context

Persistent working memory for this repo. **Read `AGENTS.md` first** â€” it holds the expanded command
list, layout and trap catalogue. This file holds architecture, state, decisions and debt, and does
not repeat them.

## Goal

**V-CAD** â€” 3D ULPIN Generation and Vertical Property Mapping System. SIH 2026 prototype, Problem
Statement 26011.

Model a property as a **footprint plus `z_min`/`z_max`** so apartments, basements, underground
utilities and elevated rights are governed as spatial volumes, not flat map features. Phase reached:
**feature-complete prototype** â€” the remaining goal is demo reliability, not new features.

## Architecture

Three units, coupled over HTTP only; no shared code between backend and frontend. The backend is
layered (api â†’ services â†’ repositories) and the repository is swappable: in-memory or PostGIS.

```
frontend/ (Next.js 14 + CesiumJS)  â”€â”€HTTPâ”€â”€>  backend/ (FastAPI + Shapely + SQLAlchemy)  â”€â”€>  PostGIS
```

**Backend layers** â€” routers depend on services, services depend only on the repository interface:

- **`app/main.py`** â€” wiring only: app + CORS, `bootstrap()` seed on startup, `/health`,
  `/demo/load`, and `include_router` for the 9 routers in original route order.
- **`app/api/`** â€” one router per resource (`parcels`, `buildings`, `properties`, `infrastructure`,
  `ulpin`, `processing`, `validation`, `analytics`, `imports`) plus `deps.py`. Routers depend on
  `request_repository`, which yields a PostGIS repository **bound to the request's session** (one
  transaction per request) when `DATABASE_URL` is set, and the in-memory store otherwise.
- **`app/services/`** â€” domain logic, each module receiving its repository explicitly:
  `geometry` (**the geometry engine â€” the only module allowed to import Shapely**), `ulpin`
  (stable identity), `geometry_versioning`, `validation` (8 rules), `demo` (seed), `ingestion`,
  `processing`, `analytics`, and `storage` (backend-agnostic field/JSON/date/timestamp maps shared
  by both repositories).
  `geometry` exposes conversion, validity (`validate_polygon`, `repair_polygon`), measures,
  Z handling, topology and identity (`calculate_geometry_hash`, `calculate_spatial_code`).
  `repair_polygon` exists but is **deliberately not wired into `validate()`** â€” see Known Issues.
- **`app/repositories/`** â€” `base.py` defines the `CadastreRepository` ABC and `RecordNotFound`;
  `memory.py` is the in-memory demo store; `postgres.py` is the PostGIS implementation (adds
  `spatial_query()` and `metric_area_m2()`). `__init__.py` holds the ambient repository plus a
  `storage()` context manager for startup/scripts.
- **`app/db/`** â€” `models.py` (17 SQLAlchemy models, one per collection), `session.py`
  (`get_db_session()` request-scoped transactions, `session_scope()`, `check_database_health()`),
  `types.py` (PostGIS column types and the two CRS constants), `base.py` (declarative Base).
- **`app/models/`** â€” `schemas.py` (Pydantic, the API contract) and `enums.py`. `Severity` stays a
  `Literal` alias so serialisation is unchanged; records store plain `str` values, not enum members.
- **`alembic/`** -- `0001_initial` creates the 8 core tables, 14 GIST indexes and the PostGIS
  extensions; `0002`-`0005` add `processing_jobs`, `extracted_buildings`,
  `extracted_floors` and `generated_property_volumes`; `0006` adds
  `validation_issues.{rule_id,category,evidence}`; `0007` adds `review_cases`,
  `review_decisions` and `audit_events`; `0008` adds `cadastral_changes`,
  `0009` adds `provenance_links`; `0010` adds `cadastral_changes.difference_geometry`.
- **`backend/tests/`** -- 799 tests: API contract, services, geometry engine,
  identity/versioning, the rule engine, review workflow, audit log, change detection,
  integration tests that skip automatically with no database.
- **`frontend/app/page.tsx`** â€” entire UI, one `"use client"` component. On mount fires **six
  parallel GETs** (`/parcels`, `/buildings`, `/properties`, `/infrastructure`,
  `/validation/issues`, `/analytics/summary`) via `reload()`; every demo button routes through
  `action()` â†’ `reload()`.
- **`database/init.sql`** â€” enables `postgis` + `pgcrypto` only. Tables come from Alembic.

**57 routes total.** `GET /ulpin/{ulpin}` is registered before `POST /ulpin/generate`, so a `GET` to
`/ulpin/generate` is treated as a search and 404s â€” this precedence is deliberate and test-locked.

Stack: Python 3.12 / FastAPI 0.115 / Pydantic 2.11 / Shapely 2.1 / SQLAlchemy 2.0 /
GeoAlchemy2 0.18 / Alembic 1.16 / psycopg 3 Â· Node 20 / Next 14.2 / React 18.3 / TS 5.7 `strict` /
Tailwind 3.4 / Cesium 1.145 Â· `postgis/postgis:16-3.4` (db/user/pass all `vcad`).
No ML model is loaded anywhere.

**Record contract** (keep new records on it): GeoJSON `Polygon` + `z_min`/`z_max` +
`geometry_hash` (12-char uppercase SHA-256, coords rounded to 3 dp so it survives float noise) +
`version`.

**Demo scene** (`seed_demo`): `P-001` = `box(0,0,80,52)`; `B-001` = `box(10,8,70,44)`, 25.6 m; 8 floors
Ã— 3.2 m; 16 apartments + basement (`z âˆ’3.2..0`) + corridor `INF-U-001` = `box(18,21,62,25)`,
`z âˆ’2.2..0`. 17 volumes.

## Current State

Working end to end. Deterministic ULPIN generation; Shapely topology validation across 8 issue types;
command-centre UI with ULPIN search, camera focus, floor isolation, underground toggle, validation
panel and analytics; `docker compose up --build` brings up db + API + frontend.

The three `/processing/*` endpoints are deterministic stubs returning fixed confidences
(0.974 / 0.918 / 0.991) â€” not inference. `seed_demo` yields 17 volumes and 3 intentional conflicts.

Backend is layered and has 152 passing tests. The frontend is unchanged and still a single
component per layer.

### Two CRS, and why it matters

| | CRS | Used for |
|---|---|---|
| `GEOGRAPHIC_SRID` = 4326 | WGS84 degrees | **authoritative storage**; SRID recorded per column. Topological predicates (`ST_Intersects`) are correct here. |
| `METRIC_SRID` = 32643 | WGS84 / UTM 43N, metres | **measurement and metric indexing**, via a generated `*_metric` column per geometry. |

`ST_Area` on the 4326 column returns **square degrees** (`3.8e-07` for the demo parcel); the metric
companion returns `4146.7 mÂ²`. Never measure on the geographic column. In production `METRIC_SRID`
must be chosen per survey area, not hard-coded.

The engine's local planar transform is **never persisted** â€” the PostGIS repository adapts GeoJSON
directly via `shapely.geometry.shape`.

### Identity, hash and version are three separate things

| Concept | Field | Behaviour |
|---|---|---|
| Stable cadastral identity | `prototype_ulpin` | `VC-LP-{parcel}` or `VC-VP-{parent}-{unit}`. Derived **only** from non-geometric attributes. Assigned once; `assign_ulpins()` never overwrites unless explicitly told to. |
| Current-geometry digest | `geometry_hash` | 12-char uppercase SHA-256. Changes when geometry changes. |
| Geometry revision | `geometry_version` | Monotonic integer, incremented by `create_geometry_version()`. |

Re-surveying an object therefore increments `geometry_version` and changes `geometry_hash`
**without minting a new identifier**. `record_to_polygon` reads `geometry_3d`/`geometry`/`footprint`
by shape (a building's `geometry_3d` is a wrapper, not a polygon). History lives in the
`geometry_versions` collection, one immutable row per revision.

## Recent Significant Changes

- **Backend refactored into layers** (the first code change since `Init`). The 263-line monolith
  `app/main.py` is now wiring-only, with logic in `app/services/`, HTTP in `app/api/`, storage behind
  `app/repositories/`, and the contract in `app/models/`.
- **Dedicated geometry engine** in `app/services/geometry.py`. All Shapely use is confined there;
  `validation.py`, `ulpin.py` and `demo.py` were migrated off raw geometry calls. Added
  `repair_polygon`, `validate_z_range`, `is_polygon_inside`, `calculate_height_difference`,
  `calculate_volume_difference`, `union_footprints` and `record_to_polygon` on top of the transform,
  hashing and intersection code that already existed.
- **Stable ULPIN identity + geometry versioning.** Identity was decoupled from geometry: the
  geometry-derived hash and centroid grid index were removed from the identifier, and
  `services/geometry_versioning.py` added (`create_geometry_version`, `get_current_geometry_version`,
  `get_geometry_history`, `compare_geometry_versions`, `snapshot_geometry`). New `geometry_versions`
  repository collection and a `geometry_version` field on all four record types. This intentionally
  changed `prototype_ulpin` values, added `geometry_version`, and gave buildings/infrastructure a
  `geometry_hash` they previously lacked; demo validation results are byte-identical.
- **Real point-cloud ingestion.** `services/point_cloud.py` inspects LAS/LAZ/PLY from headers only
  (never loads points); `services/point_cloud_ingestion.py` runs the 10-step pipeline, streaming the
  upload to disk while hashing and enforcing the size cap mid-stream. New `processing_jobs` table
  (migration 0002) plus `/point-clouds`, `/point-clouds/{id}` and `/processing-jobs` endpoints.
  `/import/source` now routes point clouds to the pipeline while CSV/plan-JSON stay register-only.
  Route count 21 â†’ 24; `test_import_point_cloud_is_registered_not_processed` was replaced by
  `test_import_point_cloud_is_inspected_not_just_registered`.
- **Real building extraction from point clouds â€” algorithmic/geometric, not ML.**
  `services/point_cloud_extraction.py` implements `classify_ground()` (progressive grid filter with
  ground dilation into roof-only cells), `extract_non_ground_points()`, `cluster_building_points()`
  (DBSCAN over XY via a spatial hash), `generate_building_footprints()` (concave hull, convex
  fallback), `simplify_building_footprint()`, `estimate_building_height()` (95th percentile, not
  max) and `create_building_from_point_cloud()`, behind a `BuildingExtractor` interface with one
  implementation, `DeterministicBuildingExtractor`. `services/point_cloud_read.py` streams points
  (laspy `chunk_iterator`; PLY is a full load, reported as such). `services/point_cloud_store.py`
  retains uploads so extraction can re-read them.
  **Honesty constraints, all test-enforced:** `method="algorithmic_geometric"` everywhere, **no
  `confidence` field or column** (the score is `geometric_quality`, a regularity measure, and the
  legacy simulated adapters' literal `0.974` is the anti-pattern this replaced), no ML accuracy
  number anywhere, a CRS-less cloud is refused rather than assumed, and `extracted_buildings` is a
  table separate from cadastral `buildings` because an inferred footprint has no legal standing.
  New migration `0003`, new endpoints `POST /point-clouds/{id}/extract` and
  `GET /extracted-buildings`; route count 24 â†’ 26. Building ids are keyed on the processing job so
  re-runs are retained rather than colliding.
- **Floor-level detection and segmentation â€” algorithmic/geometric, not ML.**
  `services/point_cloud_floors.py` implements `estimate_floor_height()`, `detect_floor_levels()`,
  `segment_points_by_floor()`, `create_floor_volume()` and `validate_floor_spacing()`.
  Storey heights come from the **elevation histogram**: floor and ceiling slabs are scanned densely,
  so each storey boundary is a peak, and peak spacing (median, so one irregular storey cannot move
  it) is the storey height. The detected peaks *are* the level stack, so a double-height storey is
  preserved rather than averaged away â€” verified on a synthetic 3.4/6.9/3.4/3.4 m building, where a
  uniform grid would have produced four even 3.55 m storeys.
  **Nothing is hard-coded**: no floor count and no storey height literal exists in the module, and a
  test greps for them. The demo's `FLOOR_COUNT = 8` / `FLOOR_HEIGHT = 3.2` stay in
  `app/services/demo.py` untouched, and `/floors` still reports them. A caller may pass
  `floor_height=`, which is recorded as `levels_from="supplied_by_caller"` and flagged, because a
  supplied height is not a measurement.
  Output carries `floor_number`, `z_min`/`z_max` (absolute elevation in the cloud's CRS) plus the
  heights above the building base, the storey's plan geometry, `geometric_quality`,
  `source_id` and `processing_job_id`. Uncertainty is **flagged, not smoothed**: a boolean
  `requires_human_review` with specific reasons (`IRREGULAR_SPACING`, `NO_LEVEL_EVIDENCE`,
  `TOP_LEVEL_AMBIGUOUS`, `LEVELS_SUPPLIED`, `SPARSE_PLAN_COVERAGE`, `TOO_FEW_POINTS`,
  `DATUM_CORRECTED`), at both storey and building level. New migration `0004`
  (`extracted_floors`, deliberately with **no** `confidence` column and **no** owner or property
  column), endpoints `POST /point-clouds/{id}/segment-floors` and `GET /extracted-floors`; route
  count 26 â†’ 28. All three pipeline stages are now `PENDING` after ingestion rather than
  `NOT_IMPLEMENTED`, and a fully processed source reports `SEGMENTED` â€” **not** `COMPLETE`, because
  property rights are not delineated. **No property ownership is implemented**: the demo scene's 17
  properties and 8-floor stack are unchanged by segmentation.
- **Vertical property-volume generation.** `services/property_volumes.py` implements
  `generate_property_volumes()`, `split_floor_into_units()`, `create_volume_from_footprint()`,
  `assign_parent_parcel()`, `assign_building()`, `assign_floor()`, `calculate_area()` and
  `calculate_volume()`. Each volume carries a stable prototype ULPIN, parent parcel, building,
  floor, unit label where available, `z_min`/`z_max`, geometry, area, volume, geometry hash,
  geometry version, **both** source and processing provenance, and a status.
  **Apartment boundaries are never invented.** Only two inputs are accepted â€” supplied unit
  outlines, or supplied dividing walls turned into regions by a planar subdivision. With neither,
  the output is one volume per storey: `volume_scope="FLOOR"`, `unit_label=None`,
  `units_inferred=False` â€” a claim about extent, not about ownership. The `units_inferred` column
  exists so a consumer can *verify* that rather than trust the code. ULPINs include the storey in
  their key because floor plans repeat labels such as `01` on every floor, and re-generating
  **replaces** a volume rather than accumulating a duplicate, since identity is stable by design.
  New migration `0005` (`generated_property_volumes`, deliberately with **no** `confidence`, owner,
  tenant or title column), endpoints `POST /point-clouds/{id}/property-volumes` and
  `GET /generated-property-volumes`; route count 28 â†’ 30. A fully processed source reports
  `VOLUMES_GENERATED` â€” **not** `COMPLETE`, because derived geometry is not a cadastral record.
  The cadastral `property_volumes` table is never written to.
- **Real PostGIS persistence.** `app/db/` (SQLAlchemy models, `get_db_session()` request-scoped
  transactions, `session_scope()`, `check_database_health()`), `repositories/postgres.py`, and Alembic
  owning the schema. `init.sql` reduced to extensions. All collections persist. Footprints stored
  in SRID 4326 with a generated SRID 32643 companion for measurement. **Verified**: migration runs
  from a clean database, autogenerate produces an empty revision, 33 integration tests pass, and a
  40-probe API capture is byte-identical between backends except `/health`.
- **CRS management** (`services/crs.py`): real PROJ transforms replaced the fixed-divisor
  approximation, which was wrong by ~14 cm over 80 m. `select_processing_crs()` derives a UTM zone
  from the data; no government CRS is assumed. `data_source` metadata now preserves CRS provenance.
- **Rule-based validation engine.** `services/validation_engine.py` declares **30 rules** across six
  categories as independent, individually-runnable objects; `services/validation.py` is now a thin
  adapter. `ValidationRule` / `ValidationResult` / `ValidationEngine` / `ValidationContext` plus
  `run_all_rules()`, `run_rule()`, `get_rule_definitions()`, `get_validation_summary()`.
  Categories: GEOMETRY 5, PARCEL 4, VERTICAL 6, CADASTRAL 5, INFRASTRUCTURE 5, CHANGE 5.
  Every finding now carries `rule_id`, `category` and an `evidence` dict of the numbers that
  produced it, persisted in the new nullable `validation_issues.{rule_id,category,evidence}` columns
  (migration `0006`, `evidence` as JSONB, both new columns indexed). The three deliberate demo
  findings are byte-for-byte unchanged and the 22 new rules are provably silent on the seeded
  scene (parametrised over all 22). **Behaviour preservation was the hard part and is enforced**:
  rule *order* is load-bearing (validity must precede containment, or the preserved `GEOSException`
  stops being raised), findings are persisted *as produced* rather than batched (or a crash leaves
  nothing behind, breaking `test_invalid_geometry_is_appended_then_raises`), and the catch-all
  infrastructure rule keeps its exact original scope. Three new endpoints: `GET /validation/rules`,
  `POST /validation/rules/{rule_id}/run`, `GET /validation/summary` (33 routes total).
- **Human verification and audit history.** `services/reviews.py` runs the workflow
  ``ISSUE -> REVIEW -> APPROVE / REJECT / REQUEST_RESURVEY / MARK_EXPECTED`` plus close/reopen, over
  three new collections: `review_cases` (mutable current state, one per finding, unique on
  `issue_id`), `review_decisions` (immutable, one row per decision) and `audit_events` (the
  cross-cutting append-only trail in `services/audit.py`). Every decision records reviewer,
  timestamp, decision, reason, issue, affected object, and both states. Ten new audit actions:
  CREATED, UPDATED, IMPORTED, PROCESSED, VALIDATED, APPROVED, REJECTED, RESURVEY_REQUESTED,
  GEOMETRY_CHANGED, ULPIN_GENERATED, plus five for review state transitions. The existing
  operations now record themselves: `validate()`, `create_geometry_version()` /
  `snapshot_geometry()`, `assign_ulpins()`, `register_source()` / point-cloud ingest, and the three
  processing stages. 13 new routes (46 total), migration `0007`. **The reviewer is self-asserted**
  (`DEVELOPMENT_ACTOR`) -- authentication is out of scope, and `/audit/actions` says so in the
  response. Four things this milestone had to get right, all of which bit during the build:
  `validate()` rebuilds the issues collection, so reviewed states are carried forward by
  `restore_review_outcomes()` or a re-run silently discards a human's approval; `RESURVEY_REQUESTED`
  is deliberately not a flavour of `REJECTED`; reopening clears a field with delete-then-insert
  because change sets drop `None`; and audit/decision ids carry a microsecond prefix so same-tick
  events have a total order. It also fixed a **latent repository parity bug**: `InMemoryRepository
  .update()` never applied `normalise_changes()`, so the in-memory store accepted writes PostGIS
  silently dropped -- which had made the append-only guarantee true on one backend only.
- **Cadastral change detection.** `services/change_detection.py` compares the approved cadastre
  against a new survey and reports *differences*, never wrongdoing.
  `compare_approved_vs_survey()` plus `detect_{new_floor,removed_floor,footprint_change,height_change,volume_change}()`,
  `calculate_geometry_change()`, `calculate_{volume,height}_delta()` and `generate_change_report()`.
  Five change types (FOOTPRINT, HEIGHT, VOLUME, NEW_FLOOR, REMOVED_FLOOR) are **not** mutually
  exclusive: a storey whose boundary moved and whose height changed yields two records, because
  they are two facts a reviewer must judge separately. Every record carries the fourteen required
  fields, both geometries, and `status="REQUIRES_VERIFICATION"`. A newly-appeared storey is filed as
  the specific `UNREGISTERED_FLOOR` finding, and every change opens a review case so
  "requires verification" is actionable. New `cadastral_changes` table (migration `0008`,
  append-only), 4 routes (50 total). Four deliberate decisions: (a) the brief asked for a
  "confidence/quality metric" and the answer is `geometric_quality` -- an **agreement score in [0,1]**
  (footprint: intersection-over-union), never a confidence, because the project has no `confidence`
  field anywhere and a number implying survey accuracy would be an invention; (b) `ChangeStatus` has
  no "illegal"/"unauthorised" value at all, findings are `WARNING` and never `CRITICAL`, and
  `test_no_change_module_claims_wrongdoing` scans the module's own text for those words so the
  constraint survives future edits; (c) `REMOVED_FLOOR` says "not covered by this survey", since
  absence from one survey is not a demolition; (d) a **re-survey supersedes rather than replaces** --
  the comparison never edits cadastral geometry, so what was approved stays readable. It also fixed a
  second parity bug: `detected_at` was missing from `storage.TIMESTAMP_FIELDS`, so it came back as a
  `datetime` under PostGIS and an ISO string in memory.
- **Source-to-cadastral provenance.** `services/provenance.py` records how every derived object
  came to exist, as a graph of parent-to-child links rather than a `source_id` column: a generated
  volume has one source but several things it also contributes to (a finding about it, a review of
  that finding), and the later stages are events with no row to hang a foreign key off. The chain is
  `DATA_SOURCE -> PROCESSING_JOB -> BUILDING -> FLOOR -> PROPERTY_VOLUME -> ULPIN -> VALIDATION ->
  REVIEW` (`ProvenanceStage`, exposed at `GET /provenance/stages` so the order is contract, not
  implementation). Eight functions: `create_provenance_record()`, `link_source_to_object()`,
  `link_processing_job_to_object()`, `get_provenance()`, `get_object_lineage()`,
  `get_derived_objects()`, `get_source_objects()`, `get_processing_history()`. Instrumented at the
  point each object comes into existence -- the three pipeline stages, `validate()` and
  `create_review_case()` -- so provenance is real rather than plumbing. New `provenance_links` table
  (migration `0009`, append-only), 7 routes (57 total). **No model information is fabricated**:
  `model_name`/`model_version` exist for the day a model is used and are empty for everything today,
  because every stage is algorithmic or geometric; `ALGORITHMIC_METHODS` holds the *real* method
  identifiers the pipeline emits, and the service layer *refuses* a model name attached to one.
  A test asserts no link anywhere names a model. Gaps are reported, never filled: the demo's seeded
  records were never derived from a survey, so their lineage says `complete: false` with a note, and
  a test asserts a fabricated origin is never attached. Two bugs this milestone found and fixed:
  `_walk()` had its direction inverted **and** labelled ancestors with the child's stage, so a
  six-stage chain collapsed to one; and `ALGORITHMIC_METHODS` initially contained a descriptive
  method name I had written by hand rather than the one the code emits -- exactly the invention the
  guard exists to prevent.
- **Frontend exposes the real processing workflow.** Additive: the Command Center, the Cesium view and
  the three-column layout are preserved; the bottom panel grew from 178px to 296px to hold the new
  panels. `lib/workflow.ts` is the pure model (nine steps, result-kind rules, source-row shaping) and
  `components/Workflow.tsx` is presentation only, so the workflow can be tested without a browser.
  The nine steps are all backed by a named endpoint: sources, parcels, the three pipeline stages
  (each keyed to its real `job_type`, so RUNNING and FAILED come from the job feed and a *missing* job
  is reported as not-run rather than as a failure), identifiers, validation summary, review queue, and
  approved findings. **Every fabricated figure is gone**: the literal `97.4% / 91.8% / 99.1%`
  "Model/prototype confidence" panel, the `AVG AI` tile and the Inspector's `Confidence` row were
  removed; `geometric_quality` is now shown as a regularity measure with its own note, and
  `NO_ML_STATEMENT` states outright that no stage uses a trained model. `resultKind()` returns `ml`
  only for a real `model_name`, which nothing in this system has. Issue actions (inspect, focus, open
  review, approve, reject, request resurvey, mark expected) disable themselves until a reason is
  entered, because the API requires one. 32 frontend tests: 21 unit tests over `lib/workflow.ts` run
  directly under Node's type stripping, and 11 that drive the real API in the same order the page does.
  Two findings from writing them: `deriveWorkflow()` crashed on a partial API response -- and
  `loadWorkspace()` returns `null` for a failed read, so one 500 would have blanked the screen, now
  routed through `rows()`; and a hand-rolled LAS header in the test had its scale/offset fields at
  the wrong byte offsets, so the fixture is a real 1 MB LAS written by laspy and committed for that
  reason.
- **Visual tools for cadastral verification.** Seven tools in `frontend/lib/compare.ts` --
  `showBeforeGeometry()`, `showAfterGeometry()`, `highlightChangedGeometry()`, `showChangeMetrics()`,
  `showObjectLineage()`, `showValidationGeometry()`, `focusValidationIssue()` -- each returning a plain
  description of *what to display* so they are testable without a browser, with
  `components/ChangeInspector.tsx` applying it and an additive `overlay` prop on `CesiumMap`. The
  Previous / Current / Difference panel shows footprint, height, volume and floor count on each side,
  with footprint / height / volume deltas and the storey difference as the Difference group. Three
  map layers per change, each reporting honestly when it has nothing to draw. **Wording is neutral
  throughout**: `neutralStatus()` projects backend state onto CHANGE DETECTED, REQUIRES REVIEW,
  APPROVED, REJECTED, RESURVEY REQUESTED and ACCEPTED AS EXPECTED, and two tests enforce it -- one
  over the vocabulary, and one that scans the component and the tool module for
  illegal/unauthorised/violation/encroach/trespass/fraud/suspicious/intrusion. One small backend
  addition was unavoidable: `cadastral_changes.difference_geometry` (migration `0010`), because a
  change cannot be *highlighted* without the region that changed, and recomputing it per read would
  show a different answer than the one recorded. That exposed a real gap -- `polygon_to_geojson` emits
  only the exterior ring, so a symmetric difference with a hole would have been filled back in and
  shown as the *opposite* of the region that changed; `geometry.shape_to_geojson()` now keeps interior
  rings and handles MultiPolygon. Three bugs the tests caught: `difference_geometry` was added to the
  Pydantic class but not to `from_record`, so it silently serialised as null everywhere;
  `showChangeMetrics(null)` reported CHANGE DETECTED because its `{}` fallback was truthy; and a zero
  height delta rendered as "0 m" beside "+6.40 m", implying a different unit. 83 frontend tests
  (62 unit + 21 live-API) and 825 backend tests.
- **All milestones were diffed against pre-change snapshots** so every intended change could be
  enumerated. 523 tests now cover the contract, services, geometry, CRS, identity, point-cloud
  inspection, building extraction, storey segmentation, property-volume generation, the rule engine
  and PostGIS (557 with a database, 34 skipped without).
- **Both changes were proven behaviour-preserving**: a 40-probe response snapshot plus the full
  OpenAPI document, captured from the original monolith, is byte-identical after each. 83 tests now
  cover the contract, the domain services and the geometry engine.- Single commit `4a4161a "Init"` (2026-09-13) is the baseline.

## Decisions

1. **Swappable storage behind one repository contract.** `InMemoryRepository` keeps the offline demo
   working with no database; `PostgresCadastreRepository` gives durability. Both return **identical
   record shapes** â€” verified by a 40-probe API capture diffed across backends (39/40 byte-identical;
   only `/health` differs, as it must). Backend chosen by `DATABASE_URL`, overridable with
   `VCAD_REPOSITORY=memory`.
2. **Alembic owns the schema; `init.sql` only enables extensions.** The original `init.sql` DDL and
   the application models had drifted on 6 points, and two schema sources is how that happened.
   An empty `--autogenerate` revision is the check that models and migrations agree.
3. **TEXT primary keys holding the app's own ids**, not the `init.sql` UUID + TEXT business key.
   Preserves the API contract exactly, and a survey reference is a better cadastral key than a
   random UUID. Deviation documented in the migration header.
4. **2D `geometry(Polygon,4326)` + scalar `z_min`/`z_max`**, not `POLYGONZ`/`POLYHEDRALSURFACEZ` as in
   `init.sql`. The record contract is a 2D footprint with explicit elevations; the app never built
   a 3D geometry, and 2D + scalars indexes and validates better.
5. **Generated metric companion columns.** `ST_Transform(geom, 32643)` STORED and GIST-indexed, so
   the metric projection cannot drift from the source geometry and cannot be forgotten at a call
   site.
2. **Deterministic inference adapters, not a model.** Same API boundary a PyTorch implementation
   would expose, without a large model download; keeps the demo reproducible offline.
3. **Deterministic ULPIN** `VC-LP-{parcel_id}` or `VC-VP-{parent}-{unit_label}`, derived from
   stable non-geometric attributes only. Required so that re-surveying a parcel does **not** mint a
   new identifier â€” the parcel is the same legal object either way. `assign_ulpins()` skips records
   that already have one.
4. **Two coordinate systems, used for different jobs.** WGS84 (4326) is the authoritative store and
   is correct for topology; a projected metre-based CRS (UTM, derived from the data) is used for
   every measurement. `crs.py` is the only module that transforms, so there is no second code path
   that could reintroduce a hand-rolled approximation. **Neither is a claim about an official
   cadastral CRS** â€” see the CRS section above.
5. **`prototype_ulpin` field name and `VC-` prefix** are deliberate. The format is invented for this
   demo and is **not** an official Government of India ULPIN; the naming prevents misrepresentation.
6. **Cesium `baseLayer: false`, no Ion token** â€” fully offline, no credential or network dependency.
7. **Docker frontend has no Dockerfile** â€” `node:20-bookworm-slim` with `./frontend` bind-mounted,
   running `npm install && npm run dev`, for fast live iteration. Installs land in the host tree.
8. **Cesium assets copied at postinstall** into gitignored `public/cesium`, avoiding both a large
   vendor commit and the Cesium CDN.
9. **Repositories raise domain errors, not `HTTPException`.** `RecordNotFound` and
   `IngestionError(status_code, detail)` are translated in `api/deps.py`, keeping storage and
   services transport-agnostic.
10. **Services take their repository as an argument** rather than reaching for a global, so they are
    unit-testable without an HTTP layer. The process-wide repository is injected via a FastAPI
    dependency.
11. **All geometry goes through `services/geometry.py`.** `validation.py`, `ulpin.py` and `demo.py`
    no longer import Shapely. This makes the engine the single place to fix CRS handling later, and
    keeps the validation rules readable as rules rather than geometry code.
12. **Identity, geometry hash and geometry version are owned by different services.** `ulpin.py`
    issues identity and never writes a hash; `geometry_versioning.py` owns the hash and the version
    and never writes an identifier. `compare_geometry_versions()` reports `ulpin_changed`, which is
    structurally always `False` â€” the machine-checkable form of the stability invariant.
13. **`calculate_geometry_hash` normalises Z bounds to `float`.** The payload is JSON, so an integer
    `0` and a float `0.0` would otherwise hash differently for identical geometry. This only became
    safe to fix once identity stopped depending on the hash.

## Constraints

- **Point clouds: header-only inspection.** `point_cloud.py` reads LAS/LAZ headers via lazy
  `laspy.open` and parses PLY headers incrementally; **points are never loaded**, so cost is
  independent of point count. Libraries: `laspy` + `lazrs` (PDAL has no Python wheel on this
  platform and its source build fails), `plyfile` for PLY. A file's CRS is reported as declared;
  undeclared stays `UNKNOWN_CRS`. Uploads stream to a temp file in chunks with the size cap enforced
  *during* the stream. Jobs: `METADATA_EXTRACTION` runs; `BUILDING_EXTRACTION` and
  `FLOOR_SEGMENTATION` are recorded `NOT_IMPLEMENTED`, so a cloud whose metadata succeeded reports
  `METADATA_ONLY`, never `COMPLETE`.
- **Representational:** never present `VC-*` as official ULPINs or imply government integration.
- Python 3.12+, Node 20+.
- **`requirements.txt` is pinned for Python 3.12** (the Docker image). `shapely==2.1.0`,
  `pydantic==2.11.3` and `psycopg[binary]==3.2.6` have no cp314 wheels, so on Python 3.14 a local venv
  needs `shapely>=2.1.2` / `pydantic>=2.11` / `psycopg>=3.2`. Verified working on 3.14 with
  fastapi 0.115.14 / pydantic 2.13.5 / shapely 2.1.2 / SQLAlchemy 2.1.1 / geoalchemy2 0.20 /
  alembic 1.20 / psycopg 3.3.6. Test tooling lives in `requirements-dev.txt` (pytest, httpx).
- **No CI, no ESLint config, no frontend `typecheck` script.** `npx tsc --noEmit` is the typecheck;
  `next lint` hangs on an interactive setup prompt.
- **The in-memory store is single-process global mutable state** â€” no concurrency safety, all state
  lost on restart or `--reload`. PostGIS removes both.
- Uploads capped at 10 MB; extensions limited to `.geojson .json .csv .las .laz .ply`.
- `CORS_ORIGINS` must include the frontend origin or browser calls fail while `curl` still succeeds.
- `public/cesium` is generated and gitignored; if absent the 3D view breaks with no useful error.

## Known Issues

**Intentional â€” leave alone:** the floor-2 overlap, floor-5 parcel encroachment and
utility/basement collision are seeded showcase findings (floor 5 also carries
`status="HUMAN REVIEW REQUIRED"`). `demo-data/demo-city.geojson` is never read; it is a reference
artifact mirroring the seed through the planar transform.

**Real tech debt:**

- `validate()` indexes `records("parcels")[0]` / `records("infrastructure")[0]` â†’ `IndexError` on an
  empty store, and hardcodes the literal `"P-001"` in the containment issue.
- **Invalid geometry crashes the validator.** `validate()` appends the `INVALID_GEOMETRY` finding and
  then falls straight into the footprint-difference check on the same invalid geometry â†’
  `GEOSException` â†’ HTTP 500. Pre-existing. `repair_polygon()` now exists and would fix this, but is
  **intentionally not wired in** to preserve behaviour â€” a one-line change if you want it.
- **Edge-touching duplicate identifiers crash too.** If two records share a `prototype_ulpin` but
  their footprints only touch along an edge, the intersection is a `LineString`, and the finding
  serialiser calls `polygon_to_geojson()` on it â†’ `AttributeError`. Pre-existing.
- **`calculate_geometry_hash` is no longer int/float sensitive** (normalised). It *is* still coupled
  to `HASH_PRECISION` and to the exact field order of its JSON payload â€” changing either rehashes
  every object. That is now harmless for identity, but it invalidates stored history hashes.
- **Geometry versioning is in-memory only.** `geometry_versions` is part of the demo store and is
  wiped on restart along with everything else. No ownership, authentication or audit logging yet;
  `created_by` is recorded but not enforced.
- A valid-JSON **non-object** body on `/import/geojson` (e.g. a bare array) raises `AttributeError`
  â†’ 500. Pre-existing; left as-is with a code comment.
- `/floors` returns a hardcoded 8 Ã— 3.2 m stack for `B-001`; `/analytics/summary`'s `distribution` is
  hardcoded too, unlike its other fields which derive from the store. `/processing/floor-segmentation`
  also hardcodes `floor_count: 8`.
- Import endpoints only register a source record; they never mutate the scene, so imported data can
  be neither validated nor displayed.
- `init.sql` has drifted from the API models (`unit_label` and `gap_m`/`location` missing;
  `infrastructure.owner` TEXT vs `owner_metadata` JSONB; `floor` table unused; SQL PKs are UUID while
  the app uses string ids) and references Alembic, which is not set up. **Resolved** â€” Alembic owns
  the schema now and the migration header documents each divergence.
- **No spatial index on `infrastructure`/`floors` z-ranges**, so vertical-overlap queries still scan.
  The current rules operate on `properties` only, so this is not yet a bottleneck.
- **No `select for update` / optimistic locking.** Concurrent geometry edits to one object would both
  read the same `geometry_version` and one would be lost. `pool_pre_ping` is set, but nothing
  prevents a write race.
- **`DataSource.created_at` and `Building.geometry_3d` are JSONB/timestamp passthroughs** rather than
  first-class columns, kept only to preserve the existing response contract.
- `@app.on_event("startup")` is deprecated in the pinned FastAPI; prefer a lifespan handler. Kept
  as-is to avoid behaviour drift during the refactor.
- No auth, no pagination, no rate limiting. `docs/` exists but is empty.
- **Frontend truthfulness debt** (unchanged, not yet addressed): the 9-item sidebar sets a highlight
  but switches no view, so 8 items do nothing; the Vertical Inspector is static markup; the
  automation percentages are hardcoded in JSX rather than read from the API; the "DEMO ENGINE ONLINE"
  indicator never polls `/health`; root is `min-w-[1120px]` with no responsive breakpoints.

## Important Files

| File | Role |
|---|---|
| `backend/app/repositories/base.py` | The storage seam â€” `CadastreRepository` ABC. |
| `backend/app/repositories/memory.py` | In-memory demo store + the substring search. |
| `backend/app/repositories/postgres.py` | PostGIS store; adds `spatial_query`, `metric_area_m2`. |
| `backend/app/db/models.py` | 17 SQLAlchemy models, one per collection. |
| `backend/app/db/session.py` | `get_db_session()`, `session_scope()`, `check_database_health()`. |
| `backend/app/db/types.py` | PostGIS column types + the two CRS constants. |
| `backend/alembic/versions/0001_*.py` | Creates the 8 core tables; documents every `init.sql` divergence. |
| `backend/alembic/versions/0006_*.py` | `validation_issues`: rule attribution + JSONB evidence. |
| `backend/app/services/validation_engine.py` | 30 declarative rules, the engine and the four entry points. |
| `backend/app/services/storage.py` | Backend-agnostic field/JSON/date/timestamp maps both repos share. |
| `backend/app/services/geometry.py` | **The geometry engine** - only module importing Shapely. |
| `backend/app/services/ulpin.py` | Stable identity issuance. Never writes a hash. |
| `backend/app/services/geometry_versioning.py` | Geometry history, hashes, versions, comparison. |
| `backend/app/repositories/base.py` | The PostGIS seam â€” `CadastreRepository` ABC. |
| `backend/app/repositories/memory.py` | In-memory demo store + the substring search. |
| `backend/app/services/validation.py` | The 8 topology rules (no geometry code). |
| `backend/app/services/demo.py` | Seed data, including the 3 deliberate conflicts. |
| `backend/app/services/ulpin.py` | Prototype identifier generation + disclaimer. |
| `backend/app/main.py` | Wiring: app, CORS, startup seed, router registration. |
| `backend/app/models/schemas.py` | The API contract; changes need a version bump. |
| `backend/tests/` | 799 tests locking the contract and the demo findings. |
| `frontend/app/page.tsx` | Whole UI; all API calls and demo actions. |
| `frontend/components/CesiumMap.tsx` | 3D rendering, picking, camera focus, verification overlay. |
| `frontend/lib/workflow.ts` | The 9 pipeline steps, result-kind rules, no-ML statements. Pure. |
| `frontend/lib/compare.ts` | The 7 verification tools, `neutralStatus()`, metric formatting. Pure. |
| `frontend/components/Workflow.tsx` | Workflow rail, job panel, source table, issue actions. |
| `frontend/components/ChangeInspector.tsx` | Previous / Current / Difference, change list, lineage. |
| `frontend/lib/api.ts` | Only place the API base URL is resolved. |
| `docker-compose.yml` | Defines the whole runtime topology. |
| `database/init.sql` | Target production schema, unused by the API. |

## Commands

See `AGENTS.md` for the full list. Shape: `docker compose up --build` (frontend :3000, API/docs
:8000, db :5432), or run the two apps separately from `backend/` and `frontend/`. Frontend needs
`npm install` (postinstall copies Cesium assets). `npm run copy-cesium` repairs a missing
`public/cesium`. **There is no `npm test`.**

Backend tests: `pip install -r requirements-dev.txt` then `python -m pytest` from `backend/`
(799 tests, ~60 s / 855 with PostGIS, ~80 s). The 58 PostGIS integration tests need a migrated database and skip without one:

```bash
docker run -d --name vcad-postgis -e POSTGRES_DB=vcad -e POSTGRES_USER=vcad \
  -e POSTGRES_PASSWORD=vcad -p 5432:5432 postgis/postgis:16-3.4
cd backend
ALEMBIC_DATABASE_URL=postgresql+psycopg://vcad:vcad@localhost:5432/vcad alembic upgrade head
VCAD_TEST_DATABASE_URL=postgresql+psycopg://vcad:vcad@localhost:5432/vcad python -m pytest
```

## Rejected Approaches

- **Do not import Shapely outside `app/services/geometry.py`.** It is the only module allowed to, so
  CRS work has exactly one place to land.
- **Do not add a second schema source.** Tables come from Alembic only; `database/init.sql` may only
  enable extensions. After changing a model, run `alembic revision --autogenerate` and confirm the
  diff is what you intended.
- **Do not measure on a 4326 column.** Use the `*_metric` companion (SRID 32643). `ST_Area` on degrees
  is off by ~10 orders of magnitude and will not error.
- **Do not persist the local planar transform.** The PostGIS repository adapts GeoJSON directly;
  routing it through `geojson_to_polygon` would corrupt every stored coordinate.
- **Do not let the two repositories return different record shapes.** Every field added to one must
  appear in the other; the cross-backend probe diff is the check.
- **Do not "fix" the seeded demo conflicts** in `seed_demo()` â€” they are the demo's payload, and the
  README demo workflow focuses them.
- **Do not derive `prototype_ulpin` from geometry, ever.** That was the original defect. Identity
  comes from `parent_parcel_id` + `unit_label` (or `parcel_id`), and nothing else.
- **Do not make `assign_ulpins()` overwrite by default** â€” that is what keeps live identifiers
  stable even if the derivation rules change. `overwrite=True` must stay a deliberate act.
- **Do not let `create_geometry_version()` touch `prototype_ulpin`.** A geometry revision is not a
  new object.
- **Do not store the geometry version in the `version` field.** `version` is the record row version
  (currently always 1); `geometry_version` is the geometry revision. They are independent.
- **Do not wire up PostGIS casually** â€” the in-memory store is a deliberate offline-demo boundary and
  the schema has already drifted.
- **Do not pass raw EPSG:4326 degrees to Shapely** â€” cross the boundary only via `geo_poly()` /
  `poly_geo()`; areas come out silently wrong.
- **Do not run `npm run lint`** (no config â†’ interactive prompt â†’ hang) or add one unasked.
- **Do not add Cesium Ion assets or require an Ion token.**
- **Do not commit `frontend/public/cesium/`.**
- **Do not rename the `VC-` prefix or `prototype_ulpin`**, or imply a government land-record link.
- **Do not reformat `page.tsx` wholesale** â€” the dense, semicolon-packed style with comments only
  where behaviour is non-obvious is intentional. (The old dense `main.py` style no longer applies:
  that file is now thin wiring, and the services are conventionally formatted.)
- **Do not give a Pydantic model a docstring** unless you intend it in the OpenAPI spec â€” Pydantic
  copies it to `description`, which changes the published document. `Geometry` has a comment for
  exactly this reason.
- **Do not convert stored record values to enum members.** Records hold plain `str` so serialisation
  is unchanged; the enums in `models/enums.py` are for comparisons and constants.
- **Do not assume state survives `uvicorn --reload`** â€” it reseeds and wipes on every save.
- **Do not add routes without a snapshot comparison.** The refactor was validated by diffing a
  40-probe response capture plus the full OpenAPI document against the pre-refactor monolith; repeat
  that before and after any contract change.

## Current Work

Four milestones are complete, verified and uncommitted: layered backend refactor, geometry engine,
stable identity + geometry versioning, and PostGIS persistence.

Working tree: `backend/app/` split into `api/`, `services/`, `repositories/`, `models/`, `db/`,
`config.py`, `utils.py`; new `backend/alembic/` + `alembic.ini`; `database/init.sql` reduced to
extensions. `docker-compose.yml` gained a `migrate` service; `backend/Dockerfile` ships the
migrations; `requirements.txt` gained `GeoAlchemy2`, `alembic`, `laspy[lazrs]`, `plyfile` and
`numpy`; `tests/` has 799 tests including
`test_postgres_integration.py`, `test_building_extraction{,_api}.py`,
`test_floor_segmentation{,_api}.py`, `test_property_volumes{,_api}.py` and `test_validation_engine.py`.
`AGENTS.md` and
`PROJECT_CONTEXT.md` are also still untracked.

## Next Work

Items 1-4 of the agreed order are done:

1. ~~CI + frontend typecheck~~ â€” backend has 799 tests; **still no GitHub Actions workflow**, and
   `npx tsc --noEmit` is not wired in. Now cheap: the integration suite can run against a
   `services: postgres` container.
2. ~~ULPIN stability~~ â€” identity decoupled from geometry, versioning added.
3. **Real ingest** â€” **most of it done.** Point clouds are inspected and yield building footprints,
   storeys and property volumes. The remaining gap is that generated geometry never becomes
   *cadastral* record, and GeoJSON/CSV/plan-JSON are still register-only.
   `create_geometry_version()` is the correct way to land a re-surveyed geometry once a human has
   confirmed the generated one.
4. ~~Building extraction~~ â€” **done, algorithmically.** `POST /point-clouds/{id}/extract` recovers
   footprints and heights from points.
5. ~~Floor segmentation~~ â€” **done, algorithmically.** `POST /point-clouds/{id}/segment-floors`
   detects storey levels from elevation data and flags uncertain results.
6. ~~Property-volume generation~~ â€” **done, for geometry only.** `POST
   /point-clouds/{id}/property-volumes` produces one volume per storey, or per unit where a floor
   plan supplies boundaries. The next substantial milestone is **ownership**: reconciling generated
   volumes with a register of rights, which is why nothing is currently called a cadastral record.
7. ~~PostGIS adapter~~ â€” done, with Alembic and generated metric geometry.
8. **CI + frontend typecheck** â€” still no GitHub Actions workflow and `npx tsc --noEmit` is not
   wired in. Now cheap: the integration suite runs against a `services: postgres` container.
9. **UI truthfulness** â€” wire or delete the 8 dead nav items, make the Vertical Inspector and
   confidence figures data-driven, poll `/health`, and qualify the DoLR branding. Note the sidebar's
   hardcoded automation percentages and the `/processing/*` adapters' literal confidences are now the
   *last* invented numbers left in the product.
10. ~~Rule-based validation engine~~ â€” **done.** 30 rules over six categories in
   `services/validation_engine.py`; `run_all_rules()`, `run_rule()`, `get_rule_definitions()`,
   `get_validation_summary()`; findings carry rule id, category and numerical evidence.
11. **The engine's two known limits, if they are ever in scope.** Overlap detection is still
   same-floor-only (`SAME_FLOOR_ONLY`), and `validate()` still indexes `parcels[0]` /
   `infrastructure[0]` and names the literal `P-001` in finding text, so it raises `IndexError` on
   an empty store. Both are preserved pre-existing behaviour with tests pinning them; fixing either
   is an observable change and must be a deliberate, separate milestone.

Also open: per-survey-area CRS selection (replace the hard-coded `METRIC_SRID` and the local planar
transform), write concurrency control on geometry versions, and indexes on z-ranges for vertical
overlap queries.

Also open, from the README's "Interoperability and future scale": authoritative projected CRS per
survey area, GeoPackage/WFS/WMS, DSM/DEM, 3D Tiles, paginated tile APIs, operator roles, a real
model behind `/processing/*`, and connecting only via formally approved land-record interfaces.
