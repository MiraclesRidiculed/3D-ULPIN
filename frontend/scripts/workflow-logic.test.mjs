/**
 * Unit tests for the pure workflow logic in `lib/workflow.ts`.
 *
 * No server and no browser: `deriveWorkflow` is a function of an API snapshot, so
 * the states the UI renders can be checked directly. Run with:
 *   node --test --experimental-strip-types scripts/workflow-logic.test.mjs
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  deriveWorkflow,
  emptySnapshot,
  resultKind,
  sourceRow,
  describeProvenance,
  NO_ML_STATEMENT,
  GEOMETRIC_QUALITY_NOTE,
} from "../lib/workflow.ts";

const snap = (over = {}) => ({ ...emptySnapshot, ...over });
const byId = (steps, id) => steps.find((s) => s.id === id);

test("an empty scene reports every step, and none of them as done", () => {
  const steps = deriveWorkflow(snap());
  assert.equal(steps.length, 9);
  assert.deepEqual(
    steps.map((s) => s.id),
    ["sources", "parcel", "extraction", "floors", "volumes", "ulpin", "validation", "review", "approved"],
  );
  assert.ok(steps.every((s) => s.state !== "ready"), "nothing is 'done' on an empty scene");
  assert.ok(steps.every((s) => s.state !== "error"), "nothing has failed, because nothing ran");
});

test("a failed job is an error, and a missing job is not", () => {
  const ran = deriveWorkflow(
    snap({ sources: [{ id: "DS-1" }], jobs: [{ id: "J-1", job_type: "BUILDING_EXTRACTION", status: "FAILED", error: "no points" }] }),
  );
  assert.equal(byId(ran, "extraction").state, "error");
  assert.match(byId(ran, "extraction").detail, /no points/);

  // The same stage with no job at all is *not* a failure. Inventing a failure
  // that did not happen is the mirror of inventing a confidence that was not
  // measured.
  const never = deriveWorkflow(snap({ sources: [{ id: "DS-1" }] }));
  assert.notEqual(byId(never, "extraction").state, "error");
});

test("a running job is reported as running", () => {
  const steps = deriveWorkflow(
    snap({ jobs: [{ id: "J-1", job_type: "FLOOR_SEGMENTATION", status: "RUNNING" }] }),
  );
  assert.equal(byId(steps, "floors").state, "running");
});

test("a source with no pipeline output blocks the stages that depend on it", () => {
  const steps = deriveWorkflow(snap({ sources: [{ id: "DS-1" }] }));
  assert.equal(byId(steps, "extraction").state, "blocked");
  assert.match(byId(steps, "extraction").detail, /Run building extraction first/);
});

test("a pipeline result is ready, and attention when it is flagged for review", () => {
  const clean = deriveWorkflow(
    snap({ extractedBuildings: [{ id: "XB-1", method: "algorithmic_geometric", requires_human_review: false }] }),
  );
  assert.equal(byId(clean, "extraction").state, "ready");

  const flagged = deriveWorkflow(
    snap({ extractedBuildings: [{ id: "XB-1", method: "algorithmic_geometric", requires_human_review: true }] }),
  );
  assert.equal(byId(flagged, "extraction").state, "attention");
  assert.match(byId(flagged, "extraction").detail, /flagged for human review/);
});

test("the ULPIN step reports issued-of-total, not a percentage", () => {
  const partial = deriveWorkflow(
    snap({
      parcels: [{ id: "P-1", prototype_ulpin: "VC-LP-P001" }],
      properties: [{ id: "PV-1" }, { id: "PV-2" }],
    }),
  );
  const ulpin = byId(partial, "ulpin");
  assert.equal(ulpin.count, 1);
  assert.equal(ulpin.state, "attention");
  assert.match(ulpin.detail, /1 of 3/);

  const complete = deriveWorkflow(
    snap({ parcels: [{ id: "P-1", prototype_ulpin: "VC-LP-P001" }], properties: [{ id: "PV-1", prototype_ulpin: "VC-VP-P001-X" }] }),
  );
  assert.equal(byId(complete, "ulpin").state, "ready");
});

test("validation reports critical findings as needing attention", () => {
  const steps = deriveWorkflow(
    snap({ issues: [{ id: "V-1", severity: "CRITICAL" }], validationSummary: { total: 2, by_severity: { CRITICAL: 1, WARNING: 1 } } }),
  );
  const validation = byId(steps, "validation");
  assert.equal(validation.count, 2);
  assert.equal(validation.state, "attention");
  assert.match(validation.detail, /1 critical/);
});

test("a pending review puts the review step in attention and gates the approved step", () => {
  const steps = deriveWorkflow(
    snap({
      issues: [{ id: "V-1", severity: "CRITICAL" }],
      validationSummary: { total: 1, by_severity: { CRITICAL: 1 } },
      reviewCases: [{ id: "RC-1", issue_id: "V-1", state: "PENDING" }],
    }),
  );
  assert.equal(byId(steps, "review").state, "attention");
  assert.equal(byId(steps, "approved").state, "blocked");
});

test("an approval completes the last step and says a human made it", () => {
  const steps = deriveWorkflow(
    snap({
      issues: [{ id: "V-1", severity: "WARNING", status: "APPROVED" }],
      validationSummary: { total: 1, by_severity: { WARNING: 1 } },
      reviewCases: [{ id: "RC-1", issue_id: "V-1", state: "APPROVED" }],
    }),
  );
  const approved = byId(steps, "approved");
  assert.equal(approved.state, "ready");
  assert.equal(approved.count, 1);
  assert.match(approved.detail, /approved by a reviewer/);
});

test("nothing approved yet is not reported as blocked-by-error", () => {
  const steps = deriveWorkflow(snap({ issues: [{ id: "V-1", status: "REJECTED" }], validationSummary: { total: 1 } }));
  assert.equal(byId(steps, "approved").state, "empty");
  assert.match(byId(steps, "approved").detail, /Nothing approved yet/);
});

test("every step names the endpoint its count came from", () => {
  for (const step of deriveWorkflow(snap({ sources: [{ id: "DS-1" }] }))) {
    assert.match(step.endpoint, /^(GET|POST) \//, `${step.id} must cite an endpoint`);
  }
});

test("the nine steps are numbered in data-flow order", () => {
  const steps = deriveWorkflow(snap());
  assert.deepEqual(steps.map((s) => s.index), [1, 2, 3, 4, 5, 6, 7, 8, 9]);
});

test("a record with a method is algorithmic, never ML", () => {
  assert.equal(resultKind({ method: "algorithmic_geometric" }), "algorithmic");
  assert.equal(resultKind({ extractor: "DeterministicBuildingExtractor" }), "algorithmic");
});

test("only a real model name yields the ML classification", () => {
  assert.equal(resultKind({ method: "x", model_name: "an-actual-model" }), "ml");
  assert.notEqual(resultKind({ method: "algorithmic_geometric" }), "ml");
});

test("a result is human-approved only once a person decided", () => {
  assert.equal(resultKind({ method: "algorithmic_geometric" }, "APPROVED"), "human-approved");
  assert.notEqual(resultKind({ method: "algorithmic_geometric" }, "REJECTED"), "human-approved");
});

test("a review flag does not change what produced a result", () => {
  // `resultKind` answers "what produced this", so an algorithmic result stays
  // algorithmic while it is flagged. The pending-review state is conveyed
  // separately, by the FLAGGED FOR REVIEW badge, and the two are not conflated.
  assert.equal(resultKind({ method: "algorithmic_geometric", requires_human_review: true }), "algorithmic");
  // A record with no method at all has no recorded provenance, and is reported
  // as unreviewed rather than being credited to a pipeline.
  assert.equal(resultKind({ id: "P-1" }), "unreviewed");
  assert.equal(resultKind(null), "unreviewed");
});

test("geometric quality is described as regularity, not accuracy", () => {
  const described = describeProvenance({ geometric_quality: 0.732 });
  assert.equal(described.quality, 0.732);
  assert.match(GEOMETRIC_QUALITY_NOTE, /regularity measure/i);
  assert.match(GEOMETRIC_QUALITY_NOTE, /not an accuracy/i);
});

test("the ML statement says plainly that no model is used", () => {
  assert.match(NO_ML_STATEMENT, /No stage in this pipeline uses a trained model/);
});

test("a source row reads the point count from ingestion metadata, not a guess", () => {
  const cloud = sourceRow(
    { id: "DS-2", source_type: "Point cloud", crs: "EPSG:32643", acquisition_date: "2026-09-26", metadata: { point_count: 12100, point_cloud: true } },
    { status: "VOLUMES_GENERATED" },
  );
  assert.equal(cloud.point_count, "12,100");
  assert.equal(cloud.status, "VOLUMES_GENERATED");

  const geojson = sourceRow({ id: "DS-1", source_type: "Synthetic GeoJSON", crs: "EPSG:4326", acquisition_date: "2026-09-01", metadata: {} });
  assert.equal(geojson.point_count, "—", "a source with no points shows an em dash, never a zero");
  assert.equal(geojson.status, "REGISTERED");
});

test("an undeclared CRS is shown as UNKNOWN, never defaulted to WGS84", () => {
  const row = sourceRow({ id: "DS-3", crs: "UNKNOWN_CRS", metadata: {} });
  assert.equal(row.crs, "UNKNOWN_CRS");
});

test("a snapshot with a missing collection does not crash the derivation", () => {
  const steps = deriveWorkflow({ sources: [{ id: "DS-1" }] });
  assert.equal(steps.length, 9);
  assert.equal(byId(steps, "sources").count, 1);
});
