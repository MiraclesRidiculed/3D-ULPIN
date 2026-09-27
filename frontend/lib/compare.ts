/**
 * Visual tools for cadastral verification.
 *
 * The seven tools the Command Center exposes, as pure functions over an API
 * record. Each returns a plain description of *what to display* -- geometry, camera
 * target, metrics, status -- and the component applies it. That split is what
 * makes them testable: a browser is not needed to know that a change highlights
 * the right polygon, only to draw it.
 *
 * Three commitments hold throughout this file.
 *
 * **Absent is not zero.** A storey that does not exist in the current state has
 * no current footprint, no current volume and no current height. Rendering `0`
 * for those would tell a reviewer the building collapsed to nothing, which is a
 * claim the data does not support. Every field is `null` when it is genuinely
 * absent, and the UI shows an em dash.
 *
 * **Neutral wording only.** A detected change is a difference between two
 * measurements. It is not evidence of wrongdoing, and the status vocabulary here
 * is deliberately flat: CHANGE DETECTED, REQUIRES REVIEW, APPROVED, REJECTED,
 * RESURVEY REQUESTED, ACCEPTED AS EXPECTED. Nothing says "intrusion",
 * "encroachment", "fraud" or "illegal" -- the comparison did not observe who
 * changed what, or whether they were entitled to.
 *
 * **The four result kinds stay distinct.** Algorithmic, ML, validation and
 * human-approved are provenance categories, not confidence levels, and nothing
 * here invents an accuracy figure.
 */

import type { AnyRecord, ResultKind } from "./workflow.ts";
import { describeProvenance, resultKind } from "./workflow.ts";

export type { AnyRecord };

/** The neutral status vocabulary. Flat, factual, no adjectives. */
export type NeutralStatus =
  | "CHANGE DETECTED"
  | "REQUIRES REVIEW"
  | "APPROVED"
  | "REJECTED"
  | "RESURVEY REQUESTED"
  | "ACCEPTED AS EXPECTED"
  | "NO CHANGE";

/** A value that may legitimately be absent. Never coerced to zero. */
export type Maybe = number | null;

/** One metric row, ready to render. */
export interface MetricRow {
  label: string;
  value: string;
  /** `false` when the value is genuinely absent rather than zero. */
  present: boolean;
  /** Set when the value is missing, to explain *why* rather than just showing a dash. */
  absentReason?: string;
}

export interface MetricGroup {
  title: string;
  rows: MetricRow[];
}

/**
 * The full comparison: what was approved, what the survey found, and the
 * difference. This is the Previous / Current / Difference panel.
 */
export interface ChangeMetrics {
  objectId: string;
  changeType: string;
  status: NeutralStatus;
  previous: MetricGroup;
  current: MetricGroup;
  difference: MetricGroup;
  /** The full sentence the API produced, shown verbatim rather than reworded. */
  statement: string;
  provenance: { method: string | null; quality: number | null; kind: ResultKind };
}

// --------------------------------------------------------------------------
// formatting
// --------------------------------------------------------------------------

const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

const area = (v: Maybe): MetricRow => {
  if (!isNum(v)) return { label: "Footprint", value: "—", present: false, absentReason: "no footprint on this side" };
  return { label: "Footprint", value: `${v.toLocaleString(undefined, { maximumFractionDigits: 1 })} m²`, present: true };
};

const height = (v: Maybe): MetricRow => {
  if (!isNum(v)) return { label: "Height", value: "—", present: false, absentReason: "no Z range on this side" };
  return { label: "Height", value: `${v.toFixed(2)} m`, present: true };
};

const volume = (v: Maybe): MetricRow => {
  if (!isNum(v)) return { label: "Volume", value: "—", present: false, absentReason: "not computable on this side" };
  return { label: "Volume", value: `${v.toLocaleString(undefined, { maximumFractionDigits: 1 })} m³`, present: true };
};

const floors = (v: Maybe, changeType: string): MetricRow => {
  if (!isNum(v)) {
    // A new storey has no previous count and a removed one has no current count.
    // That is the *finding*, so it is stated rather than left blank.
    return {
      label: "Floor count",
      value: changeType === "NEW_FLOOR" ? "not in the approved cadastre" : changeType === "REMOVED_FLOOR" ? "not covered by this survey" : "—",
      present: false,
      absentReason: changeType === "NEW_FLOOR" ? "the storey is new" : changeType === "REMOVED_FLOOR" ? "the survey did not cover it" : "no storey recorded",
    };
  }
  return { label: "Floor count", value: String(v), present: true };
};

type Delta = Omit<MetricRow, "label">;

/** A signed difference. Deliberately carries no label of its own: each caller
 *  names the row, and a label baked in here would be overwritten by the spread. */
const signed = (v: Maybe, unit: string, digits = 1): Delta => {
  if (!isNum(v)) return { value: "—", present: false };
  const zero = Math.abs(v) < 10 ** -digits / 2;
  const rendered = zero ? (0).toFixed(digits) : `${v > 0 ? "+" : ""}${v.toFixed(digits)}`;
  return { value: `${rendered} ${unit}`, present: true };
};

// --------------------------------------------------------------------------
// status
// --------------------------------------------------------------------------

/**
 * The neutral status for a change, given what a reviewer has done about it.
 *
 * The change record and the review case are two different facts and both are
 * needed: the change says a difference was measured, the review says whether a
 * person has looked at it yet. Neither alone gives the right word.
 */
export function neutralStatus(change: AnyRecord | null | undefined, reviewCase?: AnyRecord | null): NeutralStatus {
  const decided = reviewCase?.state;
  if (decided === "APPROVED") return "APPROVED";
  if (decided === "REJECTED") return "REJECTED";
  if (decided === "RESURVEY_REQUESTED") return "RESURVEY REQUESTED";
  if (decided === "EXPECTED") return "ACCEPTED AS EXPECTED";
  if (decided === "PENDING" || decided === "IN_REVIEW") return "REQUIRES REVIEW";
  // No review case: the difference is measured but nobody has examined it.
  return change ? "CHANGE DETECTED" : "NO CHANGE";
}

/** Tailwind-ish token per status, so the palette stays in one place. */
export const STATUS_TONE: Record<NeutralStatus, string> = {
  "CHANGE DETECTED": "#d9b45c",
  "REQUIRES REVIEW": "#d9b45c",
  APPROVED: "#7fd6b4",
  REJECTED: "#c98a80",
  "RESURVEY REQUESTED": "#8fb8d8",
  "ACCEPTED AS EXPECTED": "#a9b6bd",
  "NO CHANGE": "#7c9b9b",
};

// --------------------------------------------------------------------------
// 1. showBeforeGeometry
// --------------------------------------------------------------------------

export interface GeometryView {
  geometry: AnyRecord | null;
  zMin: number | null;
  zMax: number | null;
  colour: string;
  label: string;
  /** False when there is nothing to draw, so the UI can say why. */
  drawable: boolean;
  reason?: string;
}

const NO_PREVIOUS = "not in the approved cadastre";
const NO_CURRENT = "not covered by this survey";

/** Colours are deliberately muted for the superseded side and clear for the new. */
export const COLOURS = {
  before: "#6f8a99",
  after: "#4fc9a6",
  changed: "#e0b25c",
  validation: "#d89936",
} as const;

const zRange = (change: AnyRecord, side: "previous" | "new"): [number | null, number | null] => {
  const lo = change[`${side}_z_min`];
  const hi = change[`${side}_z_max`];
  return [isNum(lo) ? lo : null, isNum(hi) ? hi : null];
};

/**
 * Tool 1. The approved geometry, for drawing underneath the new survey.
 *
 * Returns `drawable: false` with a reason rather than an empty geometry, so the
 * panel can say "not in the approved cadastre" instead of showing a shape that is
 * not there.
 */
export function showBeforeGeometry(change: AnyRecord | null | undefined): GeometryView {
  if (!change) return { geometry: null, zMin: null, zMax: null, colour: COLOURS.before, label: "Previous", drawable: false, reason: "no change selected" };
  const [zMin, zMax] = zRange(change, "previous");
  const geometry = change.previous_geometry ?? null;
  if (!geometry) {
    return { geometry: null, zMin, zMax, colour: COLOURS.before, label: "Previous", drawable: false, reason: NO_PREVIOUS };
  }
  return { geometry, zMin, zMax, colour: COLOURS.before, label: "Previous", drawable: true };
}

// --------------------------------------------------------------------------
// 2. showAfterGeometry
// --------------------------------------------------------------------------

/** Tool 2. The new survey geometry, for drawing over the approved outline. */
export function showAfterGeometry(change: AnyRecord | null | undefined): GeometryView {
  if (!change) return { geometry: null, zMin: null, zMax: null, colour: COLOURS.after, label: "Current", drawable: false, reason: "no change selected" };
  const [zMin, zMax] = zRange(change, "new");
  const geometry = change.new_geometry ?? null;
  if (!geometry) {
    return { geometry: null, zMin, zMax, colour: COLOURS.after, label: "Current", drawable: false, reason: NO_CURRENT };
  }
  return { geometry, zMin, zMax, colour: COLOURS.after, label: "Current", drawable: true };
}

// --------------------------------------------------------------------------
// 3. highlightChangedGeometry
// --------------------------------------------------------------------------

/**
 * Tool 3. The region that actually changed: the symmetric difference between the
 * two outlines.
 *
 * Falls back to the current outline when the record predates the stored
 * difference, and says so, rather than drawing the whole new footprint and
 * implying all of it changed. A reviewer looking at a whole building highlighted
 * as "changed" learns nothing.
 */
export function highlightChangedGeometry(change: AnyRecord | null | undefined): GeometryView {
  if (!change) return { geometry: null, zMin: null, zMax: null, colour: COLOURS.changed, label: "Changed region", drawable: false, reason: "no change selected" };
  const [zMin, zMax] = zRange(change, "new");
  if (change.difference_geometry) {
    return { geometry: change.difference_geometry, zMin, zMax, colour: COLOURS.changed, label: "Changed region", drawable: true };
  }
  const after = showAfterGeometry(change);
  if (after.geometry) {
    return { ...after, label: "Changed region", colour: COLOURS.changed, drawable: true, reason: "difference not recorded; showing the current outline" };
  }
  const before = showBeforeGeometry(change);
  if (before.geometry) {
    return { ...before, label: "Changed region", colour: COLOURS.changed, drawable: true, reason: "difference not recorded; showing the approved outline" };
  }
  return { geometry: null, zMin, zMax, colour: COLOURS.changed, label: "Changed region", drawable: false, reason: "neither side has a footprint" };
}

/** A :class:`GeometryView` that is known to have geometry, so the map can draw it. */
export type DrawableGeometryView = GeometryView & { geometry: AnyRecord; drawable: true };

/**
 * Every geometry a change can draw, narrowed so the map does not have to
 * re-check for null. Each detected change produces at least one of these when it
 * has geometry on either side, so nothing on the list is left undrawn.
 */
export function changeOverlays(change: AnyRecord | null | undefined): DrawableGeometryView[] {
  return [showBeforeGeometry(change), showAfterGeometry(change), highlightChangedGeometry(change)].filter(
    (v): v is DrawableGeometryView => v.drawable && v.geometry !== null,
  );
}

// --------------------------------------------------------------------------
// 4. showChangeMetrics
// --------------------------------------------------------------------------

/**
 * Tool 4. The Previous / Current / Difference panel.
 *
 * Floor count is the one metric that is not simply the other three: for a
 * `NEW_FLOOR` or `REMOVED_FLOOR` change the *count itself* is the finding, so it
 * is stated in words on the side where it does not exist rather than shown as a
 * blank.
 */
export function showChangeMetrics(change: AnyRecord | null | undefined, reviewCase?: AnyRecord | null): ChangeMetrics {
  const c = change ?? ({} as AnyRecord);
  const changeType = String(c.change_type ?? "UNKNOWN");
  const previousCount = c.previous_floor_count ?? null;
  const currentCount = c.new_floor_count ?? null;

  return {
    objectId: String(c.object_id ?? "—"),
    changeType,
    // The *original* argument, not the `{}` fallback: an empty object is
    // truthy, so passing it would report CHANGE DETECTED for no change at all.
    status: neutralStatus(change, reviewCase),
    previous: {
      title: "Previous",
      rows: [
        area(isNum(c.previous_area_m2) ? c.previous_area_m2 : null),
        height(isNum(c.previous_height_m) ? c.previous_height_m : null),
        volume(isNum(c.previous_volume_m3) ? c.previous_volume_m3 : null),
        floors(previousCount, changeType),
      ],
    },
    current: {
      title: "Current",
      rows: [
        area(isNum(c.new_area_m2) ? c.new_area_m2 : null),
        height(isNum(c.new_height_m) ? c.new_height_m : null),
        volume(isNum(c.new_volume_m3) ? c.new_volume_m3 : null),
        floors(currentCount, changeType),
      ],
    },
    difference: {
      title: "Difference",
      rows: [
        { label: "Footprint delta", ...signed(isNum(c.area_delta) ? c.area_delta : null, "m²") },
        { label: "Height delta", ...signed(isNum(c.height_delta) ? c.height_delta : null, "m", 2) },
        { label: "Volume delta", ...signed(isNum(c.volume_delta) ? c.volume_delta : null, "m³") },
        { label: "Storeys", ...storeyDelta(changeType, previousCount, currentCount) },
      ],
    },
    statement: String(c.description ?? "Change detected — requires verification."),
    provenance: { ...describeProvenance(c), kind: resultKind(c) },
  };
}

/** The storey difference, in words, because "new storey 9" is not "+1". */
function storeyDelta(changeType: string, previous: Maybe, current: Maybe): Omit<MetricRow, "label"> {
  if (changeType === "NEW_FLOOR") return { value: "new storey in the survey", present: true };
  if (changeType === "REMOVED_FLOOR") return { value: "storey not covered by the survey", present: true };
  if (isNum(previous) && isNum(current) && current !== previous) {
    return { value: `${current - previous > 0 ? "+" : ""}${current - previous} storey`, present: true };
  }
  return { value: "none", present: true };
}

// --------------------------------------------------------------------------
// 5. showObjectLineage
// --------------------------------------------------------------------------

export interface LineageStage {
  stage: string;
  objectId: string | null;
  method: string | null;
  model: string | null;
}

export interface ObjectLineage {
  objectId: string;
  stages: LineageStage[];
  sourceId: string | null;
  complete: boolean;
  note: string | null;
}

/**
 * Tool 5. Where an object came from, as an ordered chain.
 *
 * The backend's own stage vocabulary drives the order, so the panel cannot
 * disagree with `/provenance/stages`. A chain that does not begin at a data
 * source reports ``complete: false`` and keeps the note explaining why -- a gap is
 * shown, not filled.
 */
export function showObjectLineage(lineage: AnyRecord | null | undefined, stageOrder: readonly string[]): ObjectLineage {
  const l = lineage ?? {};
  const byStage = new Map<string, AnyRecord>();
  for (const node of Array.isArray(l.lineage) ? l.lineage : []) {
    if (node?.stage && !byStage.has(node.stage)) byStage.set(node.stage, node);
  }
  return {
    objectId: String(l.object_id ?? "—"),
    stages: stageOrder
      .filter((stage) => byStage.has(stage))
      .map((stage) => {
        const node = byStage.get(stage)!;
        return { stage, objectId: node.object_id ?? null, method: node.algorithm ?? null, model: node.model_name ?? null };
      }),
    sourceId: l.source_id ?? null,
    complete: Boolean(l.complete),
    note: l.note ?? null,
  };
}

// --------------------------------------------------------------------------
// 6. showValidationGeometry
// --------------------------------------------------------------------------

/** Tool 6. A finding's geometry, ready to draw. */
export function showValidationGeometry(issue: AnyRecord | null | undefined): GeometryView {
  if (!issue) return { geometry: null, zMin: null, zMax: null, colour: COLOURS.validation, label: "Finding", drawable: false, reason: "no finding selected" };
  const geometry = issue.geometry ?? issue.geometry_3d ?? issue.footprint ?? null;
  const zMin = isNum(issue.z_min) ? issue.z_min : null;
  const zMax = isNum(issue.z_max) ? issue.z_max : null;
  if (!geometry) {
    return { geometry: null, zMin, zMax, colour: COLOURS.validation, label: "Finding", drawable: false, reason: "this finding carries no geometry" };
  }
  return { geometry, zMin, zMax, colour: COLOURS.validation, label: "Finding", drawable: true };
}

// --------------------------------------------------------------------------
// 7. focusValidationIssue
// --------------------------------------------------------------------------

export interface FocusTarget {
  geometry: AnyRecord | null;
  label: string;
  detail: string;
  /** False when there is nothing to fly to; the caller should say so, not guess. */
  focusable: boolean;
}

/**
 * Tool 7. What the camera should look at for a finding.
 *
 * Prefers the finding's own geometry, then either object it concerns, so a
 * finding with no geometry still navigates to the parcel or volume it is about
 * rather than refusing to move.
 */
export function focusValidationIssue(issue: AnyRecord | null | undefined, context?: AnyRecord | null): FocusTarget {
  if (!issue) return { geometry: null, label: "—", detail: "no finding selected", focusable: false };
  const own = showValidationGeometry(issue);
  const id = String(issue.id ?? "—");
  if (own.drawable) {
    return { geometry: own.geometry, label: id, detail: String(issue.issue_type ?? "finding"), focusable: true };
  }
  const subject = (context ?? {}).geometry_3d ?? (context ?? {}).geometry ?? (context ?? {}).footprint ?? null;
  if (subject) {
    return {
      geometry: subject,
      label: String(issue.object_a ?? id),
      detail: `${String(issue.issue_type ?? "finding")} — about this object`,
      focusable: true,
    };
  }
  return { geometry: null, label: id, detail: "this finding has no geometry to focus on", focusable: false };
}

/** The wording shown above a comparison. Never editorial, never alarmist. */
export const COMPARISON_CAPTION =
  "Previous and current geometry are shown as recorded. A difference between two measurements is not evidence of who changed what.";

export const NEUTRAL_STATEMENT = "Change detected — requires verification.";
