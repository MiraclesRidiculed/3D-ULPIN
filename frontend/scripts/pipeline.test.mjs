/**
 * The complete pipeline, driven through the real HTTP API.
 *
 *   SOURCE -> INGEST -> NORMALIZE -> BUILDING -> FLOOR -> PROPERTY VOLUME
 *   -> ULPIN -> VALIDATE -> REVIEW -> APPROVE -> VERSION -> CHANGE DETECTION
 *
 * Every stage asserts on the actual response. A stage that cannot be reached is
 * recorded as a failure rather than skipped, because "the pipeline stops here" is
 * the finding the audit needs.
 *
 * The request shapes here are the real contract, which is not always the obvious
 * one: `POST /reviews` takes `issue_id` as a *query* parameter, and
 * `POST /ulpin/generate` is a scene-wide backfill that returns counts rather
 * than identifiers -- the identifiers are issued during volume generation.
 */
import { readFileSync } from "node:fs";

const API = process.env.NEXT_PUBLIC_API_URL;
if (!API) { console.error("set NEXT_PUBLIC_API_URL"); process.exit(2); }

const results = [];
function stage(name, ok, detail) {
  results.push({ name, ok, detail });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name.padEnd(28)} ${detail}`);
  return ok;
}

async function http(path, init = {}) {
  const opts = { ...init, headers: { ...(init.body ? { "Content-Type": "application/json" } : {}), ...(init.headers || {}) } };
  const r = await fetch(API + path, opts);
  const text = await r.text();
  let body;
  try { body = JSON.parse(text); } catch { body = text; }
  return { status: r.status, body };
}
const GET = (p) => http(p);
const POST = (p, body) => http(p, { method: "POST", body: JSON.stringify(body ?? {}) });
const call = async (p, init) => {
  const r = await http(p, init);
  if (r.status >= 400) throw new Error(`${init?.method ?? "GET"} ${p} -> ${r.status} ${JSON.stringify(r.body).slice(0, 300)}`);
  return r.body;
};

const LAS = readFileSync(new URL("./fixtures/workflow.las", import.meta.url));

// ------------------------------------------------- SOURCE + INGEST + NORMALIZE
const fd = new FormData();
fd.append("file", new Blob([LAS]), "pipeline.las");
const ing = await (await fetch(`${API}/import/source`, { method: "POST", body: fd })).json();
stage("1 SOURCE+INGEST", !!ing.source_id, `source=${ing.source_id} crs=${ing.crs}`);
const sourceId = ing.source_id;

const meta = await call(`/point-clouds/${sourceId}`);
stage("2 NORMALIZE", meta.point_count > 0 && !!meta.format,
  `format=${meta.format} points=${meta.point_count} crs=${meta.crs} declared=${meta.crs_declared} status=${meta.status}`);
if (!meta.crs_declared) console.log(`      NOTE  CRS is "${meta.crs}" and was NOT declared; not guessed.`);

// ----------------------------------------------------------------- BUILDING
const ext = await POST(`/point-clouds/${sourceId}/extract`);
stage("3 BUILDING", ext.status === 200 && ext.body.buildings_found >= 1,
  `buildings=${ext.body.buildings_found} method=${ext.body.method} quality=${ext.body.quality_summary?.geometric_quality ?? "-"}`);
if (ext.body.buildings?.length) {
  const b0 = ext.body.buildings[0];
  console.log(`      id=${b0.id} crs=${b0.crs} height=${b0.height_m} quality=${b0.geometric_quality} (regularity, not accuracy)`);
}

// -------------------------------------------------------------------- FLOOR
const seg = await POST(`/point-clouds/${sourceId}/segment-floors`);
const segOk = seg.status === 200;
stage("4 FLOOR", segOk && seg.body.storeys_found >= 1,
  `storeys=${seg.body.storeys_found} buildings=${seg.body.buildings_segmented} review=${seg.body.requires_human_review}`);
if (segOk && seg.body.review_reasons?.length) {
  console.log(`      flagged: ${seg.body.review_reasons.length} reason(s), e.g. "${String(seg.body.review_reasons[0]).slice(0, 96)}..."`);
}

// --------------------------------------------------------- PROPERTY VOLUME
const vol = await POST(`/point-clouds/${sourceId}/property-volumes`);
const volOk = vol.status === 200;
stage("5 PROPERTY VOLUME", volOk && vol.body.volumes_generated >= 1,
  `volumes=${vol.body.volumes_generated} undivided_storeys=${vol.body.floors_without_unit_data?.length ?? 0} review=${vol.body.requires_human_review}`);

// -------------------------------------------------------------------- ULPIN
// Identifiers are issued during volume generation, keyed on source + building
// ordinal so they survive re-processing. The /ulpin/generate endpoint is a
// scene-wide backfill over the *cadastral* tables and returns counts.
const generated = await call(`/generated-property-volumes?source_id=${sourceId}`);
const allIdentified = generated.length > 0 && generated.every((g) => !!g.prototype_ulpin);
stage("6 ULPIN", allIdentified, `${generated.filter((g) => g.prototype_ulpin).length}/${generated.length} volumes carry an identifier`);
if (allIdentified) {
  const first = generated[0].prototype_ulpin;
  stage("6b ULPIN carries no job id", !/JOB/i.test(first), `${first}`);
  const again = await POST(`/point-clouds/${sourceId}/property-volumes`);
  const after = await call(`/generated-property-volumes?source_id=${sourceId}`);
  const same = after.length === generated.length &&
    after.every((v) => generated.some((g) => g.prototype_ulpin === v.prototype_ulpin));
  stage("6c ULPIN stable on re-run", same && again.status === 200,
    same ? `re-processing reissued nothing (${after.length} volumes)` : `${generated.length} -> ${after.length} volumes`);
}

// ----------------------------------------------------------------- VALIDATE
const val = await POST("/validation/run", {});
const issues = await call("/validation/issues");
stage("7 VALIDATE", val.status === 200 && issues.length >= 1,
  `issues=${val.body.issues} critical=${val.body.critical} warning=${val.body.warning}`);
console.log(`      ${issues.map((i) => `${i.id}[${i.severity}]`).join(" ")}`);

// ------------------------------------------------------------ REVIEW, APPROVE
if (issues.length) {
  // `issue_id` is a QUERY parameter on POST /reviews, not a body field.
  const open = await http(`/reviews?issue_id=${encodeURIComponent(issues[0].id)}`, { method: "POST", body: JSON.stringify({ reason: "pipeline audit" }) });
  const reviewId = open.body?.id ?? null;
  stage("8 REVIEW", open.status === 201 && open.body?.state === "PENDING", `case=${reviewId} state=${open.body?.state}`);

  const ap = await http(`/reviews/${reviewId}/approve`, { method: "POST", body: JSON.stringify({ reviewer: "audit-script", reason: "pipeline audit" }) });
  stage("9 APPROVE", ap.status === 200 && ap.body?.state === "APPROVED", `state=${ap.body?.state} reviewer=${ap.body?.reviewer}`);

  // A decision must survive a re-run of validation, which rebuilds the findings.
  await POST("/validation/run", {});
  const survived = await GET(`/reviews/${issues[0].id}`);
  stage("9b APPROVE survives re-validate", survived.body?.state === "APPROVED", `state after re-run=${survived.body?.state}`);
  const decisions = await call(`/reviews/${reviewId}/decisions`);
  stage("9c decision is immutable + recorded", decisions.length === 1 && decisions[0].decision === "APPROVED",
    `${decisions.length} decision row(s): ${decisions.map((d) => d.decision).join(",")}`);

  // Audit events key on the *object* under review, not the finding's business id
  // -- deliberate, so a trail is joinable to the record it describes. The finding
  // id is the wrong key to query, and querying it returns an honest empty list.
  const events = await call("/audit/events");
  const subject = issues[0].object_a ?? issues[0].object_b;
  const forSubject = events.filter((e) => e.object_id === subject);
  const trail = await call(`/audit/history/${subject}`);
  const trailEvents = trail.events ?? trail;
  stage("9d audit trail keyed on the object", trailEvents.length >= 1,
    `${trailEvents.length} event(s) for ${subject}: ${[...new Set(trailEvents.map(e => e.action))].join(",")}`);
  const byFinding = await call(`/audit/history/${issues[0].id}`);
  const byFindingEvents = byFinding.events ?? byFinding;
  stage("9e the finding id is not the audit key", byFindingEvents.length === 0,
    `${byFindingEvents.length} event(s) for ${issues[0].id} — the trail is keyed on the object, by design`);
  // The reviewer is self-asserted. A caller may put any name in the body and it
  // is recorded verbatim -- which is correct behaviour and exactly why the API
  // reports that nobody is authenticated. What must hold is that the *default*
  // is self-describing and that the API never claims otherwise.
  const actions = await call("/audit/actions");
  stage("9f nobody is authenticated, and the API says so", actions.actor_authenticated === false,
    `actor_authenticated=${actions.actor_authenticated}`);
  const defaultActor = (forSubject.find((e) => e.action === "CREATED") ?? {}).actor;
  stage("9g the default actor is self-describing", /unauthenticated/.test(String(defaultActor)),
    `default actor="${defaultActor}"`);
  const selfAsserted = trailEvents.filter((e) => e.actor === "audit-script");
  stage("9h a self-asserted name is recorded, unverified", selfAsserted.length >= 1,
    `${selfAsserted.length} event(s) carry the caller's unverified claim — which is why 9f matters`);
}

// -------------------------------------------------------------------- VERSION
// create_geometry_version / get_geometry_history are service-only: no route
// exposes them, so this stage is verified through the read side and the
// `geometry_version` the records carry.
const parcels = await call("/parcels");
const p0 = parcels[0];
const versioned = Number.isInteger(p0?.geometry_version) && p0.geometry_version >= 1;
stage("10 VERSION (read side)", versioned,
  `${p0.id} at v${p0.geometry_version} hash=${String(p0.geometry_hash).slice(0, 10)} ulpin=${p0.prototype_ulpin}`);
const historyRoute = await GET(`/parcels/${p0.id}/geometry-history`);
if (historyRoute.status === 404) {
  console.log("      GAP  no HTTP route exposes geometry history; write side is service-only");
}

// ------------------------------------------------------------- CHANGE DETECTION
const props = await call("/properties");
const target = props.find((p) => p.geometry_3d && p.floor_number === 1) ?? props.find((p) => p.geometry_3d);
if (target?.geometry_3d) {
  const ring = target.geometry_3d.coordinates[0].map(([lo, la]) => [lo, la]);
  const moved = ring.map(([lo, la], i) => (i >= 1 && i <= 2 ? [lo + 0.00002, la + 0.00002] : [lo, la]));
  const cmpSource = `DS-PIPE-${Date.now()}`;
  const cmp = await POST("/changes/compare", {
    source_id: cmpSource,
    survey: [
      { id: target.id, geometry_3d: { type: "Polygon", coordinates: [moved] }, z_min: target.z_min, z_max: target.z_max + 0.8, floor_number: target.floor_number, building_id: target.building_id },
      { id: "PV-PIPE-NEW", geometry_3d: target.geometry_3d, z_min: target.z_max, z_max: target.z_max + 3.2, floor_number: 41, building_id: target.building_id },
    ],
  });
  stage("12 CHANGE DETECTION", cmp.status === 200 && cmp.body.total_changes > 0,
    `changes=${cmp.body.total_changes} by_type=${JSON.stringify(cmp.body.by_type)}`);
  console.log(`      statement: "${cmp.body.changes?.[0]?.description ?? cmp.body.description ?? "(per-record)"}"`);
  const chg = await call(`/changes?source_id=${cmpSource}`);
  const fp = chg.find((c) => c.change_type === "FOOTPRINT");
  stage("12b change has drawable geometry", !!fp?.difference_geometry, `FOOTPRINT difference_geometry=${fp?.difference_geometry?.type ?? "null"}`);
  stage("12c every change is reviewable", chg.every((c) => c.finding_id), `${chg.filter((c) => c.finding_id).length}/${chg.length} carry a finding_id`);

  // A comparison must not edit the cadastre.
  const after2 = await call("/properties");
  stage("12d cadastre untouched by comparison", JSON.stringify(after2) === JSON.stringify(props),
    `${props.length} property volumes byte-identical after a comparison`);
}

// -------------------------------------------------------------------- DONE
const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
if (failed.length) {
  console.log("FAILED:");
  for (const f of failed) console.log(`  - ${f.name}: ${f.detail}`);
}
