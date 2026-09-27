# AGENTS.md — V-CAD (3D ULPIN / volumetric cadastre prototype)

SIH 2026 prototype for Problem Statement 26011. Property = footprint + `z_min`/`z_max`, governed as a
spatial volume. `frontend/` Next.js 14 + CesiumJS command centre, `backend/` FastAPI + Shapely.

## Hard constraint: identifiers are not official

Every `VC-LP-…` / `VC-VP-…` value is a **deterministic prototype format invented for this demo**. It is
not a Government of India ULPIN and implies no government integration. Never rename the `VC-` prefix,
present these as real ULPINs, or describe the system as connected to a land-record service. The
`prototype_ulpin` field name is deliberate — keep it.

## Commands

```bash
# Full stack (PostGIS + migrations + API + frontend)
docker compose up --build          # frontend :3000, API/docs :8000, db :5432
                                  # a `migrate` service runs `alembic upgrade head` first
docker compose down                # add -v ONLY to discard the PostGIS volume

# Backend (from backend/) — Python 3.12+
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000

# Frontend (from frontend/) — Node 20+
npm install      # postinstall copies Cesium assets; required, see below
npm run dev
```

### Storage: in-memory vs PostGIS

Selected by environment, with identical API responses:

| Config | Backend |
|---|---|
| no `DATABASE_URL` | in-memory demo store (default) |
| `DATABASE_URL=postgresql+psycopg://…` | PostGIS, one transaction per request |
| `VCAD_REPOSITORY=memory` | forces in-memory even if a URL is set |

**Migrations own the schema.** `database/init.sql` only enables extensions;
`backend/alembic/versions/` creates the tables.

```bash
cd backend
alembic upgrade head        # ALEMBIC_DATABASE_URL, or DATABASE_URL, or -x url=…
alembic revision --autogenerate -m "…"
```

**Never measure on a 4326 column.** `ST_Area` there returns *square degrees*
(3.8e-07 for the demo parcel). Every geometry column has a generated,
GIST-indexed companion in SRID 32643 holding the same shape in metres — use that
(`ST_Area(geometry_metric)` gives 4146.7 m²). Topological predicates
(`ST_Intersects`) are fine in 4326.

### Verification (there is no `test` script — none exists)

```bash
cd backend && python -m pytest                   # 799 tests (58 integration skip w/o a DB)
cd frontend && npx tsc --noEmit                  # this IS the typecheck; strict:true, noEmit:true
cd frontend && npm test                        # 62 unit + 21 live-API tests (start the backend first)
cd frontend && npm run test:pipeline           # 23 checks, the whole pipeline over HTTP
cd frontend && npm run build
cd backend  && python -c "import app.main"      # import-time smoke check
curl localhost:8000/health
```

Integration tests need a migrated database and are skipped otherwise:
`VCAD_TEST_DATABASE_URL=postgresql+psycopg://vcad:vcad@localhost:5432/vcad python -m pytest tests/test_postgres_integration.py`.

`npm run lint` runs `next lint` but **no ESLint config is committed** — it drops into Next's
interactive setup prompt and will hang a non-interactive agent. Don't call it; don't add a config
unless asked.

**A derived record's identity must not contain a processing job id.** The extraction
job id is a uuid, so any key derived from it changes every run. `volume_key` used to
embed the job-scoped `building_id` (`XB-{job}-001`), which meant re-processing one
source re-issued the ULPIN of an unchanged volume -- `VC-VP-P001-XBJOBaaaa...` then
`VC-VP-P001-XBJOBbbbb...` -- and defeated `_upsert_volume`'s replace-not-accumulate
discipline, so a second run accumulated duplicate volumes *and* duplicate
identifiers. Identity now derives from `stable_building_ref(source_id, ordinal)`:
**which source found this building, and in what order**. The ordinal is read from
the building's own id suffix, never re-indexed against the accumulated building list
-- that mistake was made once during this audit and reintroduced the exact
instability it was meant to remove. A job id belongs in `processing_job_id` and
nowhere else. `test_the_identifier_carries_no_processing_job_id` and
`test_reprocessing_a_source_does_not_reissue_identifiers` pin it.

**`geometry_version` must be published with the `geometry_hash` it counts.** A
revision number that cannot be matched to the geometry it describes says nothing
about whether a shape moved. The hash was written and stripped by
`response_model=Parcel` while the version was returned; both are now public.

**Both adapters must raise the same error for the same input.** `InMemoryRepository.add`
used to append unconditionally while the PostGIS adapter let the primary key fail at
commit, so one request produced two rows in memory and an unhandled
`IntegrityError` -- a 500 -- under PostGIS. Both now raise `DuplicateRecord` from
`repositories/base.py`. This is the third time a storage-parity divergence has been
found here, and the pattern is always the same: one adapter grew a guard and the
other did not. Test a behaviour against **both** backends, not one.

**A primary key derived from a *segment* of an id is not a primary key.** The storey
id used `building_id.split("-")[-1]` as its building discriminator. An extracted
building id *ends* in its ordinal, so that segment is `001` for the first building of
every job -- and one segmentation job is handed buildings left by several extraction
jobs, so the same storey id was produced twice. The full building id is used now.
Never take a substring of a compound identifier as a key component.

**Never put a fabricated figure on screen, and never serve one over HTTP.** The rule
was originally applied only to the UI, and the API kept every figure the UI had
dropped: `demo.py` wrote `confidence: 0.974` onto the seeded building and `0.918`
onto all 17 property volumes (real columns, so persisted on both backends),
`GET /floors` returned eight hardcoded storeys each carrying `0.918`, the three
`POST /processing/*` adapters returned 0.974 / 0.918 / 0.991 plus a `model` key
naming a model that does not exist, and `/analytics/summary` served
`average_confidence: 91.8`. `Building.confidence` and `PropertyVolume.confidence`
were **required** schema fields, which is *why* the earlier pass could not clean
the API -- the contract demanded a number nobody measured. All removed; the columns
remain (nullable, never written) because dropping them is a destructive migration
for no benefit. `make_volume()` no longer even accepts a `confidence` argument: a
required parameter invites a fabricated value. The command center used to show `97.4% / 91.8% / 99.1%`
under "AUTOMATION PIPELINE" labelled "Model/prototype confidence", an `AVG AI` tile, and a
`Confidence` row in the Inspector. All three were literals, not measurements, and all three are
gone. Every count in the UI comes from a named endpoint in `lib/api.ts` `ENDPOINTS`, and every
workflow step cites the route behind it (`StepView.endpoint`). If you add a figure, name the
request it came from; a number with no endpoint behind it does not belong on the screen.

**`geometric_quality` is a regularity measure and must be labelled as one.** It is not an accuracy
and not a confidence. `lib/workflow.ts` exports `GEOMETRIC_QUALITY_NOTE` for exactly this and
`NO_ML_STATEMENT` for the "no stage uses a trained model" claim. `resultKind()` returns `ml` **only**
when a real `model_name` is present, which today it never is -- do not add a fallback that labels
anything ML.

**The workflow derivation must tolerate a missing collection.** `loadWorkspace()` returns `null` for
a failed read, and `deriveWorkflow()` has to degrade that one panel rather than blank the screen. It
routes every collection through `rows()`. A direct `snapshot.x.filter(...)` will crash the command
centre the first time one endpoint 500s -- which is what happened during this milestone.

**Two test suites, two purposes.** `npm run test:logic` runs `lib/workflow.ts` directly with Node's
type stripping (no server, no browser). `npm run test:workflow` drives the **real** API in the same
order `page.tsx` does, against a backend you must start first. The second is what catches a panel
wired to a plausible-but-wrong path, which a typecheck cannot.

**Multipart uploads must not go through `api<T>()`.** It sets `Content-Type: application/json`,
which breaks a `FormData` body; `uploadFile()` uses bare `fetch` for that reason. The workflow test
re-implements the client rather than importing it, precisely so this divergence is caught instead of
inherited.

**`scripts/fixtures/workflow.las` is committed on purpose** and is gitignore-allowed. It is a real
LAS written by `laspy`, and the test needs fixed bytes: a hand-rolled LAS header in the test had the
scale/offset fields at the wrong byte offsets and the API rejected it with "read length must be
non-negative". Do not "tidy" the fixture away like the retained uploads under `backend/data/`.

## Traps

**Storage is swappable; do not hard-code either backend.** `app/repositories/` holds two
implementations of the same `CadastreRepository` contract: `memory.py` (in-memory dicts) and
`postgres.py` (SQLAlchemy + PostGIS). Routers depend on `request_repository`, which returns the
PostGIS repository bound to the request's session when `DATABASE_URL` is set, and the in-memory store
otherwise. Both return **identical record shapes** — that parity is deliberate and test-enforced, so
if you add a field, add it to both.

**Never measure on a 4326 column.** `ST_Area` on the geographic column returns *square degrees*
(`3.8e-07` for the demo parcel) — off by ~10 orders of magnitude. Each geometry column has a
generated, GIST-indexed companion in SRID 32643 (WGS84 / UTM 43N) holding the same shape in metres
(`4146.7 m²`). Use `ST_Area(geometry_metric)` / `repo.metric_area_m2()`. Topological predicates
(`ST_Intersects`, `ST_Contains`) are CRS-correct in 4326 and are what `spatial_query()` uses.

**The local planar transform must never be persisted.** `app/services/geometry.py` converts to a fake
metre plane for maths only. The PostGIS repository adapts the GeoJSON **directly** via
`shapely.geometry.shape`, bypassing that transform. Storing it would corrupt every coordinate.

**Alembic owns the schema; `database/init.sql` only enables extensions.** Two sources of truth is the
drift problem this project already suffered from. After changing a model, run
`alembic revision --autogenerate` and check the generated file — an *empty* revision means models and
migrations agree.

**The in-memory store is still non-durable.** `bootstrap()` seeds it when empty, so
`uvicorn --reload` wipes all state on every file save. PostGIS survives restarts. The integration
tests skip automatically when no database is reachable, so the unit suite runs anywhere.

**All geometry lives in `backend/app/services/geometry.py`, all CRS work in
`backend/app/services/crs.py`.** `geometry.py` is the only module that measures; it delegates every
coordinate operation to `crs.py` (PROJ via `pyproj`). The old fixed-divisor shortcut
(`x / 98000`, `y / 111000`) is **gone** — it was wrong by ~14 cm over 80 m.

Internal polygons are in **local projected metres**: the processing CRS (UTM, derived from the
anchor) translated so the anchor sits at (0, 0). GeoJSON is always WGS84. Use
`local_to_wgs84()` / `wgs84_to_local()` to cross between them — never treat local coordinates as
absolute UTM, which places geometry at the projection's false origin.

**Point clouds are inspected, not just registered.** `app/services/point_cloud.py` reads **headers
only** — cost is independent of point count. LAS/LAZ via `laspy` (lazy `laspy.open`, lazrs for LAZ);
PLY headers are parsed incrementally because `plyfile` has no header-only API. Never default a
point cloud's CRS to WGS84: an undeclared file is `UNKNOWN_CRS` with a recorded reason. The
extraction stages (building footprint, floor segmentation) are recorded as `NOT_IMPLEMENTED` jobs
rather than silently skipped — do not report a cloud as `COMPLETE` when only metadata ran.

**Uploads are streamed, never buffered.** The pipeline spools to a temp file in chunks while
hashing, and enforces the size cap *during* the stream. Keep it that way for any new ingest path.

**Ingested point clouds are retained on disk, deliberately.** Metadata inspection only needed the
header, so ingestion used to delete its spooled copy — but extraction must re-read the points.
`app/services/point_cloud_store.py` moves the upload to `backend/data/point_clouds/<source_id>/`
and records the relative path in the source metadata. `backend/data/` is gitignored. Do not "tidy
this up" by deleting on completion.

**Building extraction is algorithmic, and must never be described otherwise.**
`app/services/point_cloud_extraction.py` is a grid ground filter → DBSCAN(XY) → concave-hull →
simplify pipeline. No model is trained or applied. Three rules are load-bearing:

- **No `confidence` field, anywhere.** The schema has no such column on purpose. The honest score is
  `geometric_quality` — a *regularity* measure. The pre-existing simulated adapters in
  `app/services/processing.py` still return literal confidences (`0.974` etc.); those are the
  anti-pattern these milestones replaced.
- **Never guess a CRS.** A cloud with no CRS cannot be interpreted and is refused with 422, not
  assumed to be WGS84.
- **Property ownership is not implemented and must not be implied.** `generated_property_volumes`
  has no owner, tenant or title column, and generation never writes the cadastral
  `property_volumes` table. A source's best reachable status is `VOLUMES_GENERATED`, never
  `COMPLETE` — the volumes are derived geometry, and no right is asserted.

**Never invent an apartment boundary.** `app/services/property_volumes.py` accepts exactly two
inputs for unit data — supplied unit outlines, or supplied dividing walls turned into regions by a
planar subdivision. With neither, the output is **one volume per storey** marked
`volume_scope="FLOOR"`, `unit_label=None`, `units_inferred=False`. That is a claim about extent,
not about ownership. `units_inferred` exists as a column purely so a consumer can *verify* the
claim instead of trusting the code.

**The identifier must include the storey.** ULPINs are `VC-VP-{parcel}-{volume_key}` and the key is
`{building}-F{storey}[-{unit_label}]`. Floor plans routinely label every floor's first unit `01`;
without the storey those collide, and the unique constraint turns that into a failed run. Identity is
derived only from stable attributes, so **re-generating replaces** a volume rather than
accumulating a second one — see `_upsert_volume`, which deletes then inserts because an update
cannot clear a field that has become unset.

Two numerical traps in `planar_subdivide`, both paid for in bugs and both silent:

- **Subdividing a reprojected surface loses divisions.** Reprojection transforms a surface's
  corners independently, so its edges tilt by nanometres; a plan wall in the target CRS then shares
  no exact node, and `polygonize` returns the **whole plate as one face** — an occupied storey
  reported as undivided, with no error. Both operands are snapped to
  `SUBDIVISION_GRID` (1 mm) first. A wall that already reaches the boundary is then extended to its
  exact chord; one that stops short is left alone, so a half-height partition is never promoted into
  an invented division.
- **Containment tests need a tolerance.** A footprint round-tripped into the local plane leaves
  nanometre slivers, so an exact zero-area difference test reports every building as *overlapping*
  its own parcel.

`extracted_buildings`, `extracted_floors` and `generated_property_volumes` are **separate tables**
from `buildings`, `floors` and `property_volumes` on purpose: a cadastral row is a legal object
tied to a parcel, while a generated one is derived geometry with no legal standing. Never merge
them, and never write to the cadastral tables from a derived-data service.

**Storey levels are measured, never assumed.** `app/services/point_cloud_floors.py` derives them
from the elevation histogram: floor and ceiling slabs are scanned densely, so each storey boundary
is a histogram peak, and **peak spacing is the storey height**. There is no floor count and no
storey height literal in that module — a test enforces this. The detected peaks *are* the level
stack, so a double-height storey survives as a real level instead of being averaged away. A
caller may pass `floor_height=`, and the result then records `levels_from="supplied_by_caller"`
because a supplied height is not a measurement.

Two traps in that module, both paid for in bugs:

- **The histogram range must be padded above the data.** A local-maximum test cannot see a peak in
  the final bin, and a building's top slab lands exactly there — so an unpadded histogram silently
  loses a building's top storey.
- **A peak below one minimum storey is the base slab, not a storey boundary.** Heights are relative
  to the base, and the base datum is estimated, so the ground-floor slab shows up as a peak a few
  centimetres up. Counting it fabricates a sliver storey in every building and, for a single-storey
  one, invents a storey height out of slab-thickness noise.

`base_z` is an **absolute elevation**, not a height. Passing a height fails silently by making every
storey a whole datum too tall; the engine detects and corrects that case, and flags it as
`DATUM_CORRECTED`.

**Storey plan geometry is the convex hull, not a concave hull.** A storey's points include its floor
slab, which densely fills the interior, and Shapely's `concave_hull` is unstable over a densely
filled point set — measured returning 265 m² where the convex hull was 616 m². The building
footprint is preferred when available.

**One reprojection, and know which direction each helper goes.** `polygon_to_geojson()` applies
*local-plane → WGS84*; `geojson_to_polygon()` applies the inverse and returns the **local plane**,
not degrees. For a geometry read back from storage use `wgs84_geojson_to_polygon()` and pass an
explicit `source_crs`. Double-transforming a footprint does not error — it silently relocates it
hundreds of kilometres and collapses it to a point.

**Do not invent a government CRS.** `select_processing_crs()` derives a UTM zone from the data —
a technical choice about arithmetic, not a claim about any official cadastral reference system.
No such requirement has been supplied by the data, so none is assumed. A caller with an
authoritative CRS must pass `processing_crs=` and it is used verbatim.

**The demo scene's conflicts are intentional — do not "fix" them.** `seed_demo()` seeds three
showcase findings on purpose: the floor-2 apartment overlap (`right = box(35, 8, 70, 44)`), the
floor-5 parcel encroachment (`right = box(40, 8, 86, 44)`, past the parcel edge at x=80), and the
utility corridor colliding with the basement. Floor 5 also carries
`status="HUMAN REVIEW REQUIRED"`. These are the demo's payload — the README's demo workflow depends
on the viewer focusing them. Treat any change to these numbers as a regression.

**Never fabricate model information.** `provenance_links` has `model_name` and `model_version`
columns, and **both are `None` for everything this system does.** Every stage is algorithmic or
geometric -- a progressive grid ground filter, DBSCAN, a concavity hull, an elevation-histogram peak
search, a planar subdivision -- and there is no trained model anywhere. The columns exist so the day
a model *is* used the information lands somewhere queryable instead of being dropped.

`provenance.ALGORITHMIC_METHODS` holds the **real** method identifiers the pipeline emits
(`algorithmic_geometric`, `derived_geometric`, `registered-source`), taken from the `method` /
`SEGMENTATION_METHOD` / `GENERATION_METHOD` constants. Do not add a descriptive name that looks more
authoritative than the real one -- that is the invention the guard exists to prevent, and it happened
once during this milestone. `create_provenance_record()` **raises** if a model name is attached to a
known algorithm, and `test_no_model_information_is_fabricated` asserts no link anywhere names a model.

**A lineage that does not begin at a data source reports `complete: false`, and the gap is never
filled.** The demo's cadastral records are seeded synthetic geometry with no survey behind them, and
so is anything hand-entered. Inventing a plausible parent makes the lineage look complete while being
wrong, which is worse than admitting ignorance. A test asserts a seeded record is never given a
fabricated origin. Note the chain is walked through **graph edges**, so nulling the denormalised
`source_id` does *not* sever it -- the source-to-job edge has to be removed.

**Provenance is a graph of links, and a pipeline output gets two of them.** One edge to the job that
produced it, one to the object it came from. A segmented storey has a real parent in *both* the job
and the extracted building, and a single `parent_id` cannot hold both -- with one edge the chain dead
ends at the building and never reaches the source. `link_pipeline_output()` also calls
`record_job()`, which is idempotent, so the source-to-job edge is never missed.

**`provenance._walk()` direction and stage labelling are both load-bearing.** Walking *up* follows
`parent_id` from links whose `object_id` is the current node, and the stage of the node reached comes
from `parent_stage`; walking *down* is the mirror and the stage comes from `stage`. Getting either
wrong is silent: the first version had the direction inverted and labelled every ancestor with its
child's stage, which collapsed a six-stage chain into one repeated stage with no error. The
`test_a_volume_traces_back_to_its_data_source` test exists to catch exactly that.

**`get_object_lineage` includes the object itself.** An object queried by id seeds its own stage into
the chain, or a volume's lineage lists the storey it came from and the ULPIN identifying it but never
the volume. `processing_job_ids` comes from graph-reachable ancestors **and** from the
`processing_job_id` column of the object's own links, because a storey's segmentation job is not a
graph ancestor of it.

**`provenance_links` is append-only**, enforced by an empty set in `storage.UPDATABLE_FIELDS`, on the
same reasoning as the audit log: a rewritten provenance record is a false record. The same applies to
`cadastral_changes`.
**A detected change is a measured difference, never an allegation.** `services/change_detection.py`
reports that two geometries disagree. It cannot see who changed what or whether they were entitled
to, so it must not say. Concretely, and all of these are enforced by tests:

- `ChangeStatus` has **no** "illegal" / "unauthorised" / "violation" value. Every record starts at
  `REQUIRES_VERIFICATION` and the message is the single constant
  `CHANGE_REQUIRES_VERIFICATION` = "Change detected — requires verification."
- Change findings are `WARNING`, **never `CRITICAL`**. CRITICAL asserts a defect; a difference is an
  observation.
- `REMOVED_FLOOR` says "not covered by this survey", never "demolished" or "removed the storey" —
  absence from one survey is not a demolition.
- `test_no_change_module_claims_wrongdoing` **scans the module's own source** for illegal /
  unauthorised / trespass / encroach / violation / fraud. That test is the mechanism by which the
  constraint survives future edits; do not delete it, and if you must use one of those words, add a
  comment explaining why the scan strips it.

**`geometric_quality` on a change is an agreement score, not a confidence.** It is in `[0, 1]` and
derived from geometry alone — footprint uses intersection-over-union, height and volume use
`1 - |delta| / max(|a|,|b|)`. `1.0` means the two geometries agree. It is **not** a confidence in the
survey, a probability the change is genuine, or a judgement about who made it. There is no
`confidence` field or column anywhere in this project, and that rule applies here too. A test asserts
no change field is named "confidence".

**A re-survey supersedes rather than replaces.** `compare_approved_vs_survey()` never edits a
cadastral record. It writes to `cadastral_changes`, `issues`, `review_cases` and `audit_events`, and
touches nothing in `parcels`/`buildings`/`properties`. The approved geometry has to stay readable
after a change is found, or a reviewer cannot tell what was approved. A test asserts the geometry is
byte-identical after a deliberately enormous survey.

**Change types are not mutually exclusive.** Expanding a footprint necessarily changes the enclosed
volume; raising a storey's height does too. One object legitimately yields `FOOTPRINT` + `VOLUME`, or
`HEIGHT` + `VOLUME`. Do not "deduplicate" these into a single record — they are two facts a reviewer
judges separately, and each gets its own finding and review case. Any test that expects a specific
change count must account for this.

**Footprint detection uses the symmetric difference, not the area delta.** Two outlines of identical
area in different places produce a zero area delta and a large symmetric difference; only the latter
reveals that the boundary moved. A test covers exactly that case.

**`cadastral_changes`, `review_decisions` and `audit_events` are append-only**, enforced by empty
sets in `storage.UPDATABLE_FIELDS`. A change record keeps **both** geometries, not just the deltas,
so a reviewer can check the comparison months later. `cadastral_changes` stores them as JSONB
deliberately: they are evidence of a comparison, not a queryable location, and geometry columns would
imply a spatial index nothing uses.

**`detected_at`, `occurred_at` and `decided_at` must be in `storage.TIMESTAMP_FIELDS`.** A timestamp
column missing from that set comes back as a `datetime` under PostGIS and an ISO string in memory —
a silent backend-dependent shape difference. This has now bitten twice (`occurred_at`/`decided_at`,
then `detected_at`); when you add a timestamp column, add it there in the same change.

**Every detected change gets a finding, and the finding type is mapped in
`change_detection.FINDING_ISSUE_TYPE`.** A change with no finding has no issue, so
`create_review_case()` raises `IssueNotFound` and nothing becomes reviewable — the change would
report "requires verification" with no queue to be verified in. `NEW_FLOOR` maps to the specific
`UNREGISTERED_FLOOR` type; the rest map to the corresponding `IssueType`.

**Findings filed by change detection are not rule-engine output.** They carry
`rule_id="CHANGE-DETECTED"` (or `CHANGE-UNREGISTERED-FLOOR`) and `category="CHANGE"`, and they live
in the same `issues` collection. Telling a changed object from an unchanged one needs a baseline,
which the rule engine deliberately does not have — it evaluates the current scene alone. Do not
register these as rules.
**The audit log and review decisions are append-only, and that is enforced in the storage layer.**
`storage.UPDATABLE_FIELDS` gives `review_decisions` and `audit_events` an **empty** set, so a write
attempt is dropped rather than applied. Widening those sets would silently destroy the only property
that makes them evidence. `review_cases` is deliberately *mutable* -- it is the queue -- but its
`issue_id` is not updatable, because re-pointing a case would make its whole history describe the
wrong finding. There is no route that writes, edits or deletes an audit event, and a test asserts
that from the OpenAPI document.

**`InMemoryRepository.update()` now applies `normalise_changes()`, and must keep doing so.** It used
to apply the change set raw while the PostGIS repository filtered it, so the in-memory store accepted
writes the SQL one silently dropped. The append-only guarantee was true on one backend only, and the
parity is deliberate and test-enforced. The same reasoning drives the rest of `storage.py`: **never
hard-code a field name**, because a value the filter does not allow is dropped in silence.

**`validate()` rebuilds the issues collection, so a review must survive it.** Validation clears
`issues` and regenerates it, which would reset every `status` to `OPEN` and silently discard a human's
approval. `reviews.restore_review_outcomes()` carries decided states onto the findings that still
exist, and `validate()` calls it. If you add a way to run validation, keep that call. Findings the
rules no longer produce are left alone on purpose -- a review must not resurrect a finding.

**A change set drops `None`, so clearing a field needs delete-then-insert.** `reopen_validation_issue`
clears `decision`/`decided_at`/`decided_by` by deleting the row and re-adding it, because
`normalise_changes` filters out `None` and an update would leave the stale `APPROVED` on a case
claiming to be pending. Same reason as `property_volumes._upsert_volume`. Do not "simplify" it back
to an update.

**Audit and decision ids carry a microsecond prefix, and the sort order depends on it.** Two events
written in the same clock tick tie on `occurred_at`, and a tie broken on a random suffix gives an
arbitrary order that can differ between two reads of the same data -- which would make a decision log
unreadable. Keep the time-ordered prefix.

**`str()` on a `str`-Enum is not the value under Python 3.11+.** The validation engine stores
`IssueSeverity.WARNING` as an *enum member*, so `str()` yields `"IssueSeverity.WARNING"`, not
`"WARNING"`. Any dict lookup keyed on severity must use `getattr(sev, "value", sev)`, or every finding
silently ranks as unknown. `reviews._severity_rank` does this.

**`RESURVEY_REQUESTED` is not a flavour of `REJECTED`.** Rejecting says the *finding* is wrong;
requesting a re-survey says the *geometry* is wrong and only the second sends someone out with a
theodolite. Keep them apart. A review is decided **once**; to revisit, reopen the issue, which
preserves the decision rows.

**The reviewer is self-asserted, and nothing may imply otherwise.** Authentication is out of scope, so
`DEVELOPMENT_ACTOR` stands in and every `reviewer`/`actor` is whatever the caller supplied (body
field or `X-Reviewer` header). The default is deliberately self-describing
(`"development-actor (unauthenticated)"`) rather than a plausible name, and `/audit/actions` reports
`actor_authenticated: false`. Never present a recorded actor as proof of identity.

**Audit events key on the record's primary key, not its business id.** The parcel's row id is
`parcel-001` while its `parcel_id` is `P-001`. A finding's `object_a`/`object_b` carry business ids,
so the two are not interchangeable and conflating them makes the trail unjoinable to the records.

**Validation rule order is behaviour, not preference.** `app/services/validation_engine.py` holds 30
rules and they run in registration order, because the original implementation's two crashes depend
on it: `GEOM-INVALID` must precede `PARCEL-PROPERTY-OUTSIDE`, or the containment rule never reaches
the bow-tie polygon and the preserved `GEOSException` stops being raised. Same for
`CAD-DUPLICATE-ULPIN`, which serialises an intersection that is a `LineString` when two footprints
merely share an edge, raising `AttributeError`. Both are asserted in `tests/test_services.py`. Do not
reorder or "fix" them without also changing those tests and saying why in the commit.

**Findings are persisted as they are produced, never batched at the end.** `validate()` passes an
`on_result` callback so a rule that raises leaves the findings before it already recorded. Collecting
into a list and writing afterwards loses the partial write and breaks
`test_invalid_geometry_is_appended_then_raises`.

**Every new validation rule must stay silent on the seeded demo scene.** `seed_demo()` produces
exactly three findings on purpose and `test_seeded_scene_reports_exactly_three_findings` pins that.
`test_every_new_rule_stays_silent_on_the_demo_scene` is parametrised over all 22 non-legacy rules so
a future rule inherits the check. Note the demo's `floors` are vertical bands with
`geometry_3d = None`, so a rule reading floor *plan* geometry must skip them rather than assume a
footprint exists.

**A finding must carry its evidence.** `ValidationResult.evidence` holds the numbers that produced
the finding and is persisted as JSONB in `validation_issues.evidence`; `rule_id` and `category` are
indexed. The original eight issue fields are the stored-record contract and are unchanged - the new
columns are additive and nullable. `/validation/summary` reads *stored* findings and never runs the
rules, so it is safe on an empty store; the rules themselves are not.

**`validate()` and `/floors` are still hardcoded to the demo scene.** `validate()` indexes
`parcels[0]` and `infrastructure[0]` (IndexError on an empty store) and hardcodes the literal
`"P-001"` in the containment issue. Overlap detection is still same-floor-only, so genuine
inter-floor 3D overlaps are not detected. `/floors` returns a fixed 8-floor x 3.2 m stack for
`B-001` regardless of what was actually imported. None of this generalises to imported data - the
import endpoints only register a source record, they never mutate the scene.


**`demo-data/demo-city.geojson` is never read.** The demo city is hardcoded in `seed_demo()`; the
backend opens no files at all. The GeoJSON is a reference artifact mirroring the seed (its
coordinates are exactly the `xy_to_ll` output), useful for checking the transform — not a data source.

**`frontend/public/cesium` is generated and gitignored.** It is copied from
`node_modules/cesium/Build/Cesium` by `scripts/copy-cesium.mjs` via `postinstall`. If it is missing
the 3D view breaks with no useful error. Never commit it; if the map is blank, run
`npm run copy-cesium`. `CESIUM_BASE_URL` is pinned to `/cesium` in `next.config.mjs` *and* set on
`window` in `CesiumMap.tsx` — both are load-bearing. There is no Cesium Ion token by design
(`baseLayer: false`), so the demo works offline; don't add Ion-hosted assets casually.

## Layout

```
backend/app/main.py       wiring only: app, CORS, startup seed, /health, /demo/load, routers
backend/app/config.py     env-driven settings
backend/app/models/       Pydantic schemas (the API contract) + domain vocabulary
backend/app/services/     domain logic; geometry.py is the ONLY module importing Shapely;
                          crs.py does all coordinate work; point_cloud.py reads headers only;
                          point_cloud_extraction.py = algorithmic building extractor;
                          point_cloud_floors.py = algorithmic storey segmentation;
                          property_volumes.py = property-volume generation;
                          validation_engine.py = the 30-rule validation engine;
                          reviews.py = the human review workflow; audit.py = the audit log;
                          change_detection.py = survey-vs-approved comparison;
                          provenance.py = source-to-object lineage graph
                          (none of the four import Shapely)
backend/app/repositories/ storage: base.py (ABC), memory.py, postgres.py
backend/app/db/           SQLAlchemy models, session/transaction handling, PostGIS column types
backend/alembic/          migrations; 0001 = 8 core tables, 0002 = processing_jobs,
                          0003 = extracted_buildings, 0004 = extracted_floors,
                          0005 = generated_property_volumes;
                          0006 = validation_issues rule_id/category/evidence;
                          0007 = review_cases, review_decisions, audit_events;
                          0008 = cadastral_changes
                          0009 = provenance_links
backend/data/             retained point-cloud uploads (gitignored, never commit)
frontend/app/page.tsx     command-centre shell (client component); wires the workflow in
frontend/lib/workflow.ts    the 9 steps, result-kind rules, and the no-ML statements
frontend/lib/compare.ts      the 7 verification tools, neutral statuses, metric formatting
frontend/components/Workflow.tsx  workflow rail, job panel, source table, issue actions
frontend/scripts/*.test.mjs  workflow unit tests + live-API integration tests
frontend/components/CesiumMap.tsx  Cesium viewer; dynamic-imported with ssr:false
frontend/lib/api.ts       fetch wrapper; base URL from NEXT_PUBLIC_API_URL
database/init.sql         PostGIS/pgcrypto extensions ONLY; tables come from Alembic
```

`page.tsx` fetches six endpoints in parallel on mount, so a backend change that alters a response
shape surfaces as a blank panel plus a notice string, not a stack trace.

## Conventions

- Geometry contract is `GeoJSON footprint` + `z_min`/`z_max` + `geometry_hash` + `version`. Keep new
  records on it. `geometry_hash` rounds to 3 dp before hashing, so it is stable across float noise.
- ULPIN format: `VC-{LP|VP}-{parent}-{unit}-{spatial_code}-{hash[:6]}`, from
  `generate_ulpins()`. It must stay deterministic — the duplicate-ULPIN validation check depends on it.
- Copy `.env.example` → `.env` only to change defaults. `CORS_ORIGINS` must include the frontend
  origin or the browser calls fail while curl still succeeds.
- Volume overlap = `Shapely(footprint A ∩ footprint B).area × vertical-overlap`; containment uses
  polygon difference. Thresholds are `0.01` (m²/m³) throughout — keep them consistent.
- The compose frontend has **no Dockerfile**: it is `node:20-bookworm-slim` with `./frontend` bind-
  mounted and runs `npm install && npm run dev` on every `up`. Installs land in the host tree.
- `@app.on_event("startup")` is deprecated in the pinned FastAPI; use a lifespan handler if you touch
  `bootstrap()`.
- Single-commit history (`Init`), clean tree, no CI. Match surrounding style: the backend is
  deliberately dense (semicolon-packed one-liners, no comments except where behaviour is
  non-obvious) — don't reformat it wholesale.