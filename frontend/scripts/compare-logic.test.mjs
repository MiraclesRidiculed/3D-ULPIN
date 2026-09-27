/**
 * Unit tests for the seven verification tools in `lib/compare.ts`.
 *
 * No server and no browser: each tool returns a plain description of what to
 * display, so the decision can be checked directly. Run with:
 *   node --test --experimental-strip-types scripts/compare-logic.test.mjs
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import { readFileSync } from "node:fs";
import {
  COMPARISON_CAPTION,
  COLOURS,
  NEUTRAL_STATEMENT,
  STATUS_TONE,
  changeOverlays,
  focusValidationIssue,
  highlightChangedGeometry,
  neutralStatus,
  showAfterGeometry,
  showBeforeGeometry,
  showChangeMetrics,
  showObjectLineage,
  showValidationGeometry,
} from "../lib/compare.ts";

const POLY = { type: "Polygon", coordinates: [[[77.2, 28.6], [77.201, 28.6], [77.201, 28.601], [77.2, 28.601], [77.2, 28.6]]] };
const DIFF = { type: "Polygon", coordinates: [[[77.2, 28.6], [77.201, 28.6], [77.201, 28.6005], [77.2, 28.6005], [77.2, 28.6]]] };

const change = (over = {}) => ({
  id: "CHG-1",
  change_type: "FOOTPRINT",
  object_id: "GPV-1",
  previous_geometry: POLY,
  new_geometry: POLY,
  difference_geometry: DIFF,
  previous_z_min: 0,
  previous_z_max: 3.2,
  new_z_min: 0,
  new_z_max: 3.2,
  previous_area_m2: 120,
  new_area_m2: 140,
  previous_height_m: 3.2,
  new_height_m: 3.2,
  previous_volume_m3: 384,
  new_volume_m3: 448,
  area_delta: 20,
  height_delta: 0,
  volume_delta: 64,
  description: "Survey footprint expanded by 20.0 m². Change detected — requires verification.",
  status: "REQUIRES_VERIFICATION",
  ...over,
});

const STAGES = ["DATA_SOURCE", "PROCESSING_JOB", "BUILDING", "FLOOR", "PROPERTY_VOLUME", "ULPIN", "VALIDATION", "REVIEW"];

// ==========================================================================
// 1. showBeforeGeometry
// ==========================================================================

test("1. showBeforeGeometry returns the approved outline and its Z band", () => {
  const view = showBeforeGeometry(change());
  assert.equal(view.drawable, true);
  assert.equal(view.geometry, POLY);
  assert.equal(view.zMin, 0);
  assert.equal(view.zMax, 3.2);
  assert.equal(view.label, "Previous");
});

test("1. showBeforeGeometry says why there is nothing to draw", () => {
  const view = showBeforeGeometry(change({ previous_geometry: null }));
  assert.equal(view.drawable, false);
  assert.equal(view.geometry, null);
  assert.match(view.reason, /not in the approved cadastre/);
});

test("1. showBeforeGeometry handles no change at all", () => {
  const view = showBeforeGeometry(null);
  assert.equal(view.drawable, false);
  assert.match(view.reason, /no change selected/);
});

// ==========================================================================
// 2. showAfterGeometry
// ==========================================================================

test("2. showAfterGeometry returns the surveyed outline", () => {
  const view = showAfterGeometry(change());
  assert.equal(view.drawable, true);
  assert.equal(view.label, "Current");
});

test("2. showAfterGeometry says why a removed storey has no current outline", () => {
  const view = showAfterGeometry(change({ change_type: "REMOVED_FLOOR", new_geometry: null }));
  assert.equal(view.drawable, false);
  assert.match(view.reason, /not covered by this survey/);
});

// ==========================================================================
// 3. highlightChangedGeometry
// ==========================================================================

test("3. highlightChangedGeometry prefers the stored difference", () => {
  const view = highlightChangedGeometry(change());
  assert.equal(view.drawable, true);
  assert.equal(view.geometry, DIFF);
  assert.equal(view.label, "Changed region");
});

test("3. highlightChangedGeometry falls back and says so", () => {
  const view = highlightChangedGeometry(change({ difference_geometry: null }));
  assert.equal(view.drawable, true);
  assert.equal(view.geometry, POLY);
  assert.match(view.reason, /difference not recorded/);
});

test("3. highlightChangedGeometry reports nothing rather than drawing the whole footprint", () => {
  const view = highlightChangedGeometry(change({ previous_geometry: null, new_geometry: null, difference_geometry: null }));
  assert.equal(view.drawable, false);
  assert.match(view.reason, /neither side has a footprint/);
});

test("3. every detected change with geometry yields at least one drawable layer", () => {
  for (const record of [
    change(),
    change({ change_type: "NEW_FLOOR", previous_geometry: null }),
    change({ change_type: "REMOVED_FLOOR", new_geometry: null, difference_geometry: null }),
  ]) {
    assert.ok(changeOverlays(record).length >= 1, `${record.change_type} produced nothing to draw`);
    for (const view of changeOverlays(record)) {
      assert.equal(view.drawable, true);
      assert.ok(view.geometry, "a drawable layer must carry geometry");
    }
  }
});

test("3. a change with no geometry on either side yields no layers", () => {
  assert.deepEqual(
    changeOverlays(change({ previous_geometry: null, new_geometry: null, difference_geometry: null })),
    [],
    "a change with no geometry on any side has nothing to draw",
  );
});

test("3. the three layers use distinct colours", () => {
  const layers = changeOverlays(change());
  const colours = new Set(layers.map((l) => l.colour));
  assert.ok(colours.size >= 2, "previous, current and changed must be distinguishable");
  assert.ok(layers.every((l) => typeof l.colour === "string" && l.colour.startsWith("#")));
});

// ==========================================================================
// 4. showChangeMetrics
// ==========================================================================

test("4. showChangeMetrics reports previous, current and difference", () => {
  const m = showChangeMetrics(change());
  assert.equal(m.previous.title, "Previous");
  assert.equal(m.current.title, "Current");
  assert.equal(m.difference.title, "Difference");
  assert.deepEqual(m.previous.rows.map((r) => r.label), ["Footprint", "Height", "Volume", "Floor count"]);
  assert.deepEqual(m.difference.rows.map((r) => r.label), ["Footprint delta", "Height delta", "Volume delta", "Storeys"]);
});

test("4. values are formatted with their unit", () => {
  const m = showChangeMetrics(change());
  assert.equal(m.previous.rows[0].value, "120 m²");
  assert.equal(m.previous.rows[1].value, "3.20 m");
  assert.equal(m.previous.rows[2].value, "384 m³");
});

test("4. deltas are signed", () => {
  const up = showChangeMetrics(change());
  assert.equal(up.difference.rows[0].value, "+20.0 m²");
  const down = showChangeMetrics(change({ area_delta: -20, new_area_m2: 100 }));
  assert.equal(down.difference.rows[0].value, "-20.0 m²");
});

test("4. a zero delta reads as zero, not a missing value", () => {
  const m = showChangeMetrics(change({ height_delta: 0, new_height_m: 3.2 }));
  assert.equal(m.difference.rows[1].value, "0.00 m");
  assert.equal(m.difference.rows[1].present, true);
});

test("4. an absent value is an em dash, never a zero", () => {
  const m = showChangeMetrics(change({ previous_area_m2: null, previous_height_m: null, previous_volume_m3: null }));
  for (const row of m.previous.rows.slice(0, 3)) {
    assert.equal(row.value, "—", `${row.label} must not read as 0`);
    assert.equal(row.present, false);
    assert.ok(row.absentReason, "an absent value must say why");
  }
});

test("4. a new storey is described in words on the side where it does not exist", () => {
  const m = showChangeMetrics(change({ change_type: "NEW_FLOOR", previous_floor_count: null, new_floor_count: 9 }));
  assert.equal(m.previous.rows[3].value, "not in the approved cadastre");
  assert.equal(m.difference.rows[3].value, "new storey in the survey");
  assert.equal(m.current.rows[3].value, "9");
});

test("4. a storey missing from the survey is not called removed", () => {
  const m = showChangeMetrics(change({ change_type: "REMOVED_FLOOR", previous_floor_count: 7, new_floor_count: null }));
  assert.equal(m.current.rows[3].value, "not covered by this survey");
  assert.equal(m.difference.rows[3].value, "storey not covered by the survey");
});

test("4. a storey count difference is stated, not only numbered", () => {
  const m = showChangeMetrics(change({ previous_floor_count: 3, new_floor_count: 5 }));
  assert.equal(m.difference.rows[3].value, "+2 storey");
});

test("4. the statement is the API's own sentence, not reworded", () => {
  const m = showChangeMetrics(change());
  assert.match(m.statement, /Change detected/);
  assert.match(m.statement, /requires verification/);
});

test("4. provenance is carried through and stays algorithmic", () => {
  const m = showChangeMetrics(change({ method: "derived_geometric", geometric_quality: 0.732 }));
  assert.equal(m.provenance.method, "derived_geometric");
  assert.equal(m.provenance.quality, 0.732);
  assert.equal(m.provenance.kind, "algorithmic");
});

test("4. an empty comparison does not throw", () => {
  const m = showChangeMetrics(null);
  assert.equal(m.objectId, "—");
  assert.equal(m.status, "NO CHANGE");
  assert.ok(m.previous.rows.every((r) => r.present === false));
});

// ==========================================================================
// status vocabulary
// ==========================================================================

test("status is CHANGE DETECTED when nobody has reviewed", () => {
  assert.equal(neutralStatus(change()), "CHANGE DETECTED");
});

test("status is REQUIRES REVIEW once a case is open", () => {
  assert.equal(neutralStatus(change(), { state: "PENDING" }), "REQUIRES REVIEW");
  assert.equal(neutralStatus(change(), { state: "IN_REVIEW" }), "REQUIRES REVIEW");
});

test("status reflects the reviewer's decision", () => {
  assert.equal(neutralStatus(change(), { state: "APPROVED" }), "APPROVED");
  assert.equal(neutralStatus(change(), { state: "REJECTED" }), "REJECTED");
  assert.equal(neutralStatus(change(), { state: "RESURVEY_REQUESTED" }), "RESURVEY REQUESTED");
  assert.equal(neutralStatus(change(), { state: "EXPECTED" }), "ACCEPTED AS EXPECTED");
});

test("the status vocabulary is the flat, neutral set", () => {
  const allowed = new Set(["CHANGE DETECTED", "REQUIRES REVIEW", "APPROVED", "REJECTED", "RESURVEY REQUESTED", "ACCEPTED AS EXPECTED", "NO CHANGE"]);
  for (const state of ["PENDING", "IN_REVIEW", "APPROVED", "REJECTED", "RESURVEY_REQUESTED", "EXPECTED", undefined]) {
    assert.ok(allowed.has(neutralStatus(change(), state ? { state } : null)), `unexpected status for ${state}`);
  }
  assert.equal(Object.keys(STATUS_TONE).length, allowed.size, "every status has a tone");
});

test("no status or tone uses sensational wording", () => {
  const joined = [...Object.keys(STATUS_TONE), ...Object.values(STATUS_TONE)].join(" ").toLowerCase();
  for (const word of ["illegal", "unauthorised", "unauthorized", "violation", "encroach", "trespass", "fraud", "alert", "critical"]) {
    assert.ok(!joined.includes(word), `status vocabulary uses ${word}`);
  }
});

test("the caption states that a difference is not evidence of fault", () => {
  assert.match(COMPARISON_CAPTION, /not evidence of who changed what/);
  assert.equal(NEUTRAL_STATEMENT, "Change detected — requires verification.");
});

// ==========================================================================
// 5. showObjectLineage
// ==========================================================================

test("5. showObjectLineage returns stages in the backend's own order", () => {
  const lineage = showObjectLineage(
    {
      object_id: "GPV-1",
      complete: true,
      source_id: "DS-1",
      lineage: [
        { stage: "PROPERTY_VOLUME", object_id: "GPV-1", algorithm: "derived_geometric" },
        { stage: "DATA_SOURCE", object_id: "DS-1" },
        { stage: "FLOOR", object_id: "XF-1" },
      ],
    },
    STAGES,
  );
  assert.deepEqual(lineage.stages.map((s) => s.stage), ["DATA_SOURCE", "FLOOR", "PROPERTY_VOLUME"]);
  assert.equal(lineage.sourceId, "DS-1");
  assert.equal(lineage.complete, true);
});

test("5. an incomplete chain keeps its note rather than filling the gap", () => {
  const lineage = showObjectLineage({ object_id: "P-1", complete: false, lineage: [], note: "origin not inferred" }, STAGES);
  assert.equal(lineage.complete, false);
  assert.equal(lineage.note, "origin not inferred");
  assert.deepEqual(lineage.stages, []);
});

test("5. a model name would be shown, and there is none today", () => {
  const lineage = showObjectLineage(
    { object_id: "X", complete: true, lineage: [{ stage: "BUILDING", object_id: "B1", model_name: null }] },
    STAGES,
  );
  assert.equal(lineage.stages[0].model, null);
});

test("5. a missing lineage does not throw", () => {
  const lineage = showObjectLineage(null, STAGES);
  assert.equal(lineage.objectId, "—");
  assert.equal(lineage.complete, false);
});

// ==========================================================================
// 6. showValidationGeometry
// ==========================================================================

test("6. showValidationGeometry reads a finding's geometry", () => {
  const view = showValidationGeometry({ id: "VAL-1", geometry: POLY });
  assert.equal(view.drawable, true);
  assert.equal(view.geometry, POLY);
  assert.equal(view.label, "Finding");
});

test("6. a finding with no geometry says so", () => {
  const view = showValidationGeometry({ id: "VAL-1" });
  assert.equal(view.drawable, false);
  assert.match(view.reason, /no geometry/);
});

test("6. a finding's geometry is read from any of the geometry fields", () => {
  assert.equal(showValidationGeometry({ geometry_3d: POLY }).drawable, true);
  assert.equal(showValidationGeometry({ footprint: POLY }).drawable, true);
});

// ==========================================================================
// 7. focusValidationIssue
// ==========================================================================

test("7. focusValidationIssue prefers the finding's own geometry", () => {
  const target = focusValidationIssue({ id: "VAL-1", issue_type: "OVERLAP", geometry: POLY });
  assert.equal(target.focusable, true);
  assert.equal(target.geometry, POLY);
  assert.equal(target.label, "VAL-1");
  assert.equal(target.detail, "OVERLAP");
});

test("7. it falls back to the object the finding is about", () => {
  const target = focusValidationIssue({ id: "VAL-2", issue_type: "OUTSIDE", object_a: "PV-502" }, { geometry_3d: POLY });
  assert.equal(target.focusable, true);
  assert.equal(target.label, "PV-502");
  assert.match(target.detail, /about this object/);
});

test("7. it refuses rather than guessing when there is nothing to look at", () => {
  const target = focusValidationIssue({ id: "VAL-3" });
  assert.equal(target.focusable, false);
  assert.equal(target.geometry, null);
  assert.match(target.detail, /no geometry to focus on/);
});

test("7. no finding selected is handled", () => {
  assert.equal(focusValidationIssue(null).focusable, false);
});

// ==========================================================================
// the wording guard
// ==========================================================================

test("the verification UI contains no sensational wording", () => {
  // Scans the component and the tool module, so a future edit cannot quietly
  // reintroduce an accusation the comparison does not support.
  const files = ["../components/ChangeInspector.tsx", "../lib/compare.ts"];
  const forbidden = [
    "illegal", "unauthorised", "unauthorized", "violation", "encroach",
    "trespass", "fraud", "malicious", "suspicious", "breach", "intrusion",
  ];
  for (const file of files) {
    // Strip comments first: this test file names the forbidden words in order to
    // list them, and so does the module docstring in order to explain why it
    // avoids them. Scanning the raw text would fail on its own guard.
    const raw = readFileSync(new URL(file, import.meta.url), "utf8");
    const source = raw
      .replace(/\/\*[\s\S]*?\*\//g, " ")
      .replace(/^[ \t]*\/\/.*$/gm, " ")
      .toLowerCase();
    for (const word of forbidden) {
      assert.ok(!source.includes(word), `${file} uses "${word}"`);
    }
  }
});

test("the three comparison layers are visually distinguishable", () => {
  assert.equal(typeof COLOURS.before, "string");
  assert.equal(typeof COLOURS.after, "string");
  assert.equal(typeof COLOURS.changed, "string");
  assert.equal(new Set([COLOURS.before, COLOURS.after, COLOURS.changed]).size, 3);
});
