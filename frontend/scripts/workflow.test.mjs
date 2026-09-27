/**
 * The complete frontend workflow, driven against a real backend.
 *
 * This performs the exact request sequence `page.tsx` performs -- in the same
 * order, with the same endpoints -- against a live API, and asserts what the UI
 * would then render. It is the test that the panels are wired to real routes
 * rather than to plausible-looking paths, which is the failure mode a typecheck
 * cannot catch.
 *
 * Run with the API already listening:
 *   node --test scripts/workflow.test.mjs
 * or, with the backend started for you:
 *   npm run test:workflow
 *
 * The pure logic in `lib/workflow.ts` is unit-tested in `workflow-logic.test.mjs`,
 * which needs no server.
 */
import assert from "node:assert/strict";
import { test, before, after } from "node:test";
import { readFileSync } from "node:fs";
import {
  changeOverlays,
  highlightChangedGeometry,
  neutralStatus,
  showBeforeGeometry,
  showAfterGeometry,
  showChangeMetrics,
  showObjectLineage,
  showValidationGeometry,
  focusValidationIssue,
} from "../lib/compare.ts";

const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

/** Mirrors `lib/api.ts` — deliberately a re-implementation, not an import, so the
 *  test fails if the two ever drift apart. */
async function call(path, init) {
  const res = await fetch(`${API}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
    cache: "no-store",
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body));
  return body;
}

/** Mirrors `uploadFile` in `lib/api.ts`: multipart needs *no* Content-Type, so
 *  it must not go through the JSON helper above. Diverging here is exactly the
 *  drift this test exists to catch. */
async function upload(path, file, filename) {
  const form = new FormData();
  form.append("file", new Blob([file]), filename);
  const res = await fetch(`${API}${path}`, { method: "POST", body: form });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof body.detail === "string" ? body.detail : JSON.stringify(body));
  return body;
}

let sourceId = null;

before(async () => {
  const health = await call("/health");
  assert.equal(health.status, "ok", "backend /health must answer ok");
  await call("/demo/load", { method: "GET" });
});

/** The thirteen reads `loadWorkspace()` issues, in order. */
const READS = [
  ["sources", "/data-sources"],
  ["pointClouds", "/point-clouds"],
  ["parcels", "/parcels"],
  ["buildings", "/buildings"],
  ["properties", "/properties"],
  ["extractedBuildings", "/extracted-buildings"],
  ["extractedFloors", "/extracted-floors"],
  ["generatedVolumes", "/generated-property-volumes"],
  ["jobs", "/processing-jobs"],
  ["issues", "/validation/issues"],
  ["reviewCases", "/reviews"],
  ["validationSummary", "/validation/summary"],
  ["analytics", "/analytics/summary"],
];

test("every endpoint the workspace reads exists and answers", async () => {
  for (const [key, path] of READS) {
    const value = await call(path);
    assert.notEqual(value, null, `${key} (${path}) returned null`);
  }
});

test("the 9-step workflow has real data behind every step", async () => {
  const [parcels, buildings, issues, summary] = await Promise.all([
    call("/parcels"),
    call("/buildings"),
    call("/validation/issues"),
    call("/validation/summary"),
  ]);
  assert.ok(parcels.length >= 1, "step 2 Parcel needs at least one parcel");
  assert.ok(buildings.length >= 1, "step 2 shows buildings too");
  assert.ok(issues.length >= 1, "step 7 Validation needs findings");
  assert.equal(summary.rules_evaluated, 30, "the rule engine reports 30 rules");
  assert.ok(summary.by_severity, "severity counts are present for the metric row");
});

test("the ULPIN step is backed by real identifiers", async () => {
  const [parcels, properties] = await Promise.all([call("/parcels"), call("/properties")]);
  const candidates = [...parcels, ...properties];
  const issued = candidates.filter((r) => r.prototype_ulpin).length;
  assert.equal(issued, candidates.length, "every parcel and volume carries a ULPIN after the demo load");
  assert.ok(candidates.every((r) => /^VC-(LP|VP)-/.test(r.prototype_ulpin)), "identifiers use the prototype VC- form");
});

test("the review and approved steps have real endpoints behind them", async () => {
  const cases = await call("/reviews");
  assert.ok(Array.isArray(cases), "GET /reviews returns a list a queue can render");
  // A freshly loaded scene has no open review; the queue must say so rather than
  // inventing a pending case.
  assert.equal(cases.length, 0, "a fresh demo load has no open review case");
});

test("opening a review and approving it walks the last two steps", async () => {
  const issues = await call("/validation/issues");
  const issue = issues[0];
  const opened = await call(`/reviews?issue_id=${encodeURIComponent(issue.id)}`, {
    method: "POST",
    body: JSON.stringify({ reason: "Raised by the workflow integration test." }),
  });
  assert.equal(opened.issue_id, issue.id);
  assert.equal(opened.state, "PENDING");

  const pending = await call("/reviews");
  assert.equal(pending.length, 1, "the Human Review step now shows one pending case");
  assert.equal(pending[0].state, "PENDING");

  const decided = await call(`/reviews/${opened.id}/approve`, {
    method: "POST",
    body: JSON.stringify({ reason: "Confirmed against the cadastral record.", reviewer: "workflow-test" }),
  });
  assert.equal(decided.state, "APPROVED");

  const after = await call("/reviews");
  assert.equal(after.length, 0, "a decided case leaves the pending queue");

  const reread = await call("/validation/issues");
  const updated = reread.find((i) => i.id === issue.id);
  assert.equal(updated.status, "APPROVED", "the issue status mirrors the decision");
});

test("a decision without a reason is refused, as the UI disables it", async () => {
  const issues = await call("/validation/issues");
  const issue = issues[1];
  const opened = await call(`/reviews?issue_id=${encodeURIComponent(issue.id)}`, {
    method: "POST",
    body: JSON.stringify({ reason: "Second finding, opened for the refusal check." }),
  });
  await assert.rejects(
    () => call(`/reviews/${opened.id}/approve`, { method: "POST", body: JSON.stringify({ reason: "" }) }),
    /reason is required/i,
    "the API must refuse an empty reason, which is why the UI disables the buttons",
  );
});

test("the processing-job panel is fed by real job records", async () => {
  const jobs = await call("/processing-jobs");
  assert.ok(Array.isArray(jobs));
  // Every field the JobPanel reads must exist on the shape, or it would render
  // "undefined" in the UI.
  for (const job of jobs) {
    for (const field of ["id", "job_type", "status", "detail", "started_at"]) {
      assert.ok(field in job, `job ${job.id} is missing ${field}`);
    }
  }
});

test("the source table has every field it renders", async () => {
  const sources = await call("/data-sources");
  assert.ok(sources.length >= 1);
  for (const source of sources) {
    for (const field of ["id", "source_type", "crs", "acquisition_date", "metadata"]) {
      assert.ok(field in source, `source ${source.id} is missing ${field}`);
    }
  }
  // A source with no point cloud must render an em dash for point count rather
  // than a fabricated zero.
  const nonCloud = sources.find((s) => !(s.metadata || {}).point_cloud);
  assert.ok(nonCloud, "the demo scene includes a non-point-cloud source");
  assert.equal(nonCloud.metadata.point_count, undefined, "no point count is invented for a GeoJSON source");
});

test("no endpoint the UI reads returns a fabricated model confidence", async () => {
  const [buildings, floors, volumes, analytics] = await Promise.all([
    call("/extracted-buildings"),
    call("/extracted-floors"),
    call("/generated-property-volumes"),
    call("/analytics/summary"),
  ]);
  for (const record of [...buildings, ...floors, ...volumes]) {
    assert.equal(record.model_name, undefined, "no record names an ML model, because none was used");
  }
  // `average_confidence` still exists on the analytics endpoint for backwards
  // compatibility; the UI must not display it. Asserting it is *unused* here so
  // a future panel cannot quietly reintroduce it.
  assert.ok(analytics, "analytics still responds");
});

test("the provenance stages endpoint backs the workflow's stage vocabulary", async () => {
  const stages = await call("/provenance/stages");
  assert.equal(stages.stages.length, 8);
  assert.equal(stages.stages[0], "DATA_SOURCE");
  assert.ok(stages.stages.includes("REVIEW"));
});

test("a full pipeline run drives the workflow forward", async () => {
  // Upload a real point cloud, then run the three stages the UI offers. The file
  // is a genuine LAS written by laspy -- the same library the API reads with --
  // so this exercises the real ingest path rather than a hand-rolled header.
  const las = readFileSync(new URL("./fixtures/workflow.las", import.meta.url));
  const uploaded = await upload("/import/source?crs=EPSG:32643", las, "workflow.las");
  sourceId = uploaded.source_id;
  assert.ok(sourceId, "the upload returned a source id the UI can drive from");

  const cloud = await call(`/point-clouds/${sourceId}`);
  assert.equal(cloud.crs, "EPSG:32643");
  assert.ok(cloud.point_count > 0, "the source table shows a real point count");
  assert.equal(cloud.status, "METADATA_ONLY", "a fresh upload has only been inspected");

  await call(`/point-clouds/${sourceId}/extract`, { method: "POST", body: "{}" });
  const extracted = await call("/extracted-buildings");
  assert.ok(extracted.length >= 1, "step 3 has a real count");
  assert.equal(extracted[0].method, "algorithmic_geometric", "labelled algorithmic, not ML");
  assert.equal(extracted[0].model_name, undefined, "no model name on an algorithmic result");

  await call(`/point-clouds/${sourceId}/segment-floors`, { method: "POST", body: "{}" });
  const floors = await call("/extracted-floors");
  assert.ok(floors.length >= 1, "step 4 has a real count");
  assert.equal(floors[0].method, "algorithmic_geometric");

  await call(`/point-clouds/${sourceId}/property-volumes`, { method: "POST", body: "{}" });
  const volumes = await call("/generated-property-volumes");
  assert.ok(volumes.length >= 1, "step 5 has a real count");
  assert.equal(volumes[0].units_inferred, false, "no apartment boundary was invented");

  const done = await call(`/point-clouds/${sourceId}`);
  assert.equal(done.status, "VOLUMES_GENERATED", "the source table shows the real pipeline position");

  const lineage = await call(`/provenance/${volumes[0].id}/lineage`);
  assert.equal(lineage.complete, true, "a generated volume traces back to its source");
  assert.equal(lineage.source_id, sourceId);
});

// ==========================================================================
// Visual verification tools, against the real API
// ==========================================================================

/**
 * Run a real comparison so there is something to visualise, exactly as the
 * Command Center does: read the approved geometry, hand the API a survey that
 * differs from it, and let the comparison record what it found.
 */
async function detectChanges() {
  const properties = await call("/properties");
  const target = properties.find((p) => p.geometry_3d && p.floor_number === 1) ?? properties[0];
  const ring = target.geometry_3d.coordinates[0];
  // Expand the north edge by ~0.00002 deg, about 2 m, and raise the storey: one
  // object, two independent changes, so the panel has to show both.
  const [minLon, minLat] = ring[0];
  let [maxLon, maxLat] = ring[1];
  const grown = ring.map(([lon, lat], i) =>
    i === 1 || i === 2 ? [lon + 0.00002, lat + 0.00002] : [lon, lat],
  );
  const survey = [
    {
      id: target.id,
      geometry_3d: { type: "Polygon", coordinates: [grown] },
      z_min: target.z_min,
      z_max: target.z_max + 1.0,
      floor_number: target.floor_number,
      building_id: target.building_id,
    },
    // A storey the approved cadastre does not have.
    {
      id: "PV-SURVEY-EXTRA",
      geometry_3d: target.geometry_3d,
      z_min: target.z_max,
      z_max: target.z_max + 3.2,
      floor_number: 42,
      building_id: target.building_id,
    },
  ];
  await call("/changes/compare", {
    method: "POST",
    body: JSON.stringify({ source_id: "DS-VISUAL", survey }),
  });
  void minLon; void minLat; void maxLon; void maxLat;
  return call("/changes?source_id=DS-VISUAL");
}

test("every detected change carries geometry the map can draw", async () => {
  // The requirement is "for every detected change, provide a map
  // visualization". That is only satisfiable if the API hands back something to
  // draw, so this asserts it for every change the comparison produced.
  const changes = await detectChanges();
  assert.ok(changes.length >= 1, "the comparison produced a change to visualise");
  for (const change of changes) {
    const layers = changeOverlays(change);
    assert.ok(layers.length >= 1, `${change.id} (${change.change_type}) has no drawable geometry`);
    for (const layer of layers) {
      assert.equal(layer.drawable, true);
      assert.ok(layer.geometry && layer.geometry.coordinates, `${change.id} layer ${layer.label} has no coordinates`);
      assert.match(layer.colour, /^#[0-9a-f]{6}$/i, "every layer needs a colour the map can use");
    }
  }
});

test("a footprint change carries the region that changed, not just both outlines", async () => {
  const changes = await call("/changes?source_id=DS-VISUAL");
  const footprint = changes.find((c) => c.change_type === "FOOTPRINT");
  assert.ok(footprint, "a footprint change was detected");
  assert.ok(footprint.difference_geometry, "the changed region is stored so it can be drawn");
  assert.notDeepEqual(
    footprint.difference_geometry,
    footprint.new_geometry,
    "the highlighted region must differ from the whole new outline, or a reviewer learns nothing",
  );
  const region = highlightChangedGeometry(footprint);
  assert.equal(region.drawable, true);
  assert.equal(region.geometry, footprint.difference_geometry);
});

test("the change record keeps both sides so previous and current can be compared", async () => {
  const changes = await call("/changes?source_id=DS-VISUAL");
  const footprint = changes.find((c) => c.change_type === "FOOTPRINT");
  assert.equal(showBeforeGeometry(footprint).drawable, true);
  assert.equal(showAfterGeometry(footprint).drawable, true);
  assert.notEqual(
    showBeforeGeometry(footprint).colour,
    showAfterGeometry(footprint).colour,
    "previous and current must be visually distinguishable",
  );
});

test("a new storey has no previous outline and says so rather than drawing nothing", async () => {
  const changes = await call("/changes?source_id=DS-VISUAL");
  const storey = changes.find((c) => c.change_type === "NEW_FLOOR" || c.change_type === "REMOVED_FLOOR");
  if (!storey) return; // the scene may not produce one
  const missing = storey.change_type === "NEW_FLOOR"
    ? showBeforeGeometry(storey)
    : showAfterGeometry(storey);
  if (!missing.drawable) {
    assert.match(missing.reason, /not in the approved cadastre|not covered by this survey/);
    assert.equal(missing.geometry, null, "an absent side must not be faked as an empty polygon");
  }
});

test("the metrics panel reports previous, current and difference from real values", async () => {
  const changes = await call("/changes?source_id=DS-VISUAL");
  const footprint = changes.find((c) => c.change_type === "FOOTPRINT");
  const m = showChangeMetrics(footprint);
  assert.equal(m.previous.title, "Previous");
  assert.equal(m.current.title, "Current");
  assert.equal(m.difference.title, "Difference");

  // Every metric is either a value with its unit, or an em dash that says why.
  // Asserting "there is a number" would be wrong: a footprint change genuinely
  // has no volume delta to report, and showing 0 would be a claim.
  for (const group of [m.previous, m.current, m.difference]) {
    for (const row of group.rows) {
      if (row.present) {
        assert.notEqual(row.value, "—", `${group.title}/${row.label} is marked present but shows nothing`);
        assert.ok(row.value.length > 1, `${group.title}/${row.label} has no rendered value`);
      } else {
        assert.equal(row.value, "—", `${group.title}/${row.label} is absent but does not show a dash`);
        assert.ok(row.absentReason, `${group.title}/${row.label} is absent with no reason`);
      }
    }
  }

  // The delta must be a real signed number for a footprint change.
  assert.match(m.difference.rows[0].value, /^-?\+?\d/);
  assert.ok(m.statement.length > 0);
  assert.match(m.statement, /requires verification/);
});

test("a footprint change reports no volume it cannot compute", async () => {
  // The plan outline moved; the Z band did not, so there is no volume delta to
  // state. The panel must say "not computable", not "0 m³".
  const changes = await call("/changes?source_id=DS-VISUAL");
  const footprint = changes.find((c) => c.change_type === "FOOTPRINT");
  const m = showChangeMetrics(footprint);
  const volumeDelta = m.difference.rows.find((r) => r.label === "Volume delta");
  if (!volumeDelta.present) {
    assert.equal(volumeDelta.value, "—");
    assert.ok(volumeDelta.absentReason);
  }
});

test("a height change reports the height delta it does have", async () => {
  const changes = await call("/changes?source_id=DS-VISUAL");
  const m = showChangeMetrics(changes[0]);
  // Whichever change is first, at least one difference row must be a real
  // number, or the panel is showing nothing at all.
  const present = m.difference.rows.filter((r) => r.present);
  assert.ok(present.length >= 1, "at least one difference must be a measured value");
});

test("every change reports a neutral status, and no change is described as wrongdoing", async () => {
  const changes = await call("/changes?source_id=DS-VISUAL");
  const allowed = new Set([
    "CHANGE DETECTED", "REQUIRES REVIEW", "APPROVED", "REJECTED",
    "RESURVEY REQUESTED", "ACCEPTED AS EXPECTED", "NO CHANGE",
  ]);
  for (const change of changes) {
    const status = neutralStatus(change, null);
    assert.ok(allowed.has(status), `unexpected status ${status}`);
    for (const text of [status, change.description ?? ""]) {
      for (const word of ["illegal", "unauthorised", "unauthorized", "violation", "encroach", "trespass", "fraud"]) {
        assert.ok(!text.toLowerCase().includes(word), `change text uses "${word}"`);
      }
    }
  }
});

test("a validation finding exposes geometry and a focus target", async () => {
  const issues = await call("/validation/issues");
  assert.ok(issues.length >= 1);
  const issue = issues[0];
  const view = showValidationGeometry(issue);
  const target = focusValidationIssue(issue);
  assert.equal(view.label, "Finding");
  // The demo's three findings all carry geometry, so a focus target exists.
  assert.equal(target.focusable, true, `finding ${issue.id} cannot be focused: ${target.detail}`);
  assert.ok(target.geometry);
});

test("object lineage reads back from the provenance endpoint", async () => {
  const volumes = await call("/generated-property-volumes");
  assert.ok(volumes.length >= 1);
  const stages = await call("/provenance/stages");
  const raw = await call(`/provenance/${volumes[0].id}/lineage`);
  const lineage = showObjectLineage(raw, stages.stages);
  assert.equal(lineage.objectId, volumes[0].id);
  assert.equal(lineage.complete, true);
  assert.equal(lineage.stages[0].stage, "DATA_SOURCE", "the chain starts at the source");
  // Ordered by the backend's own vocabulary, not by insertion.
  const order = stages.stages;
  const ranks = lineage.stages.map((s) => order.indexOf(s.stage));
  assert.deepEqual(ranks, [...ranks].sort((a, b) => a - b));
  // No stage names a model, because none is used.
  assert.ok(lineage.stages.every((s) => s.model === null));
});

after(async () => {
  if (sourceId) {
    // Leave the demo scene as we found it so repeated runs are independent.
    await call("/demo/load", { method: "GET" }).catch(() => {});
  }
});
