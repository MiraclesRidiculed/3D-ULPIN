/**
 * The cadastral processing workflow, as data.
 *
 * Every step here is derived from a real backend endpoint. Nothing is inferred
 * from a hard-coded percentage, and no step reports a number the API did not
 * return -- the previous `AUTOMATION PIPELINE` panel showed a literal `97.4%`
 * labelled "Model/prototype confidence", which is exactly the kind of invented
 * accuracy figure this system refuses to publish.
 *
 * The one thing this file is opinionated about is the distinction between the
 * four kinds of result, because conflating them is the failure mode that matters:
 *
 *   algorithmic     measured by deterministic geometry (grid filter, DBSCAN,
 *                   concavity hull, elevation histogram, planar subdivision)
 *   ml              **nothing in this system produces one.** There is no trained
 *                   model anywhere in the pipeline, so no step may ever be
 *                   labelled ML, and a record with no `model_name` is never
 *                   treated as though a model produced it
 *   validation      a rule-based finding, with the rule that fired
 *   human-approved  a person resolved the finding; the only state that carries
 *                   a human judgement
 *
 * `geometric_quality` is a *regularity* measure, not an accuracy, and is labelled
 * as such wherever it is shown.
 */

export type AnyRecord = Record<string, any>;

/** The nine stages, in the order the data flows. */
export type StepId =
  | "sources"
  | "parcel"
  | "extraction"
  | "floors"
  | "volumes"
  | "ulpin"
  | "validation"
  | "review"
  | "approved";

/**
 * `empty`      nothing yet, and nothing has been attempted
 * `ready`      done
 * `running`    a real processing job for this stage is RUNNING
 * `attention`  done, but something needs a person
 * `error`      a real processing job for this stage FAILED
 * `blocked`    an earlier stage has not run, so this one cannot have either
 */
export type StepState = "empty" | "ready" | "running" | "attention" | "error" | "blocked";

export type ResultKind = "algorithmic" | "ml" | "validation" | "human-approved" | "unreviewed";

export interface StepView {
  id: StepId;
  index: number;
  label: string;
  /** What this step actually does, in one line, for the tooltip / detail row. */
  blurb: string;
  /** The endpoint that backs the count. Surfaced so the UI never asserts a
   *  number it cannot trace to a request. */
  endpoint: string;
  state: StepState;
  /** The real count, or `null` when the step does not produce a countable thing. */
  count: number | null;
  detail: string;
}

/** Everything the workflow needs, one field per endpoint the UI reads. */
export interface WorkflowSnapshot {
  sources: AnyRecord[];
  pointClouds: AnyRecord[];
  parcels: AnyRecord[];
  buildings: AnyRecord[];
  properties: AnyRecord[];
  extractedBuildings: AnyRecord[];
  extractedFloors: AnyRecord[];
  generatedVolumes: AnyRecord[];
  jobs: AnyRecord[];
  issues: AnyRecord[];
  reviewCases: AnyRecord[];
  validationSummary: AnyRecord | null;
}

export const emptySnapshot: WorkflowSnapshot = {
  sources: [], pointClouds: [], parcels: [], buildings: [], properties: [],
  extractedBuildings: [], extractedFloors: [], generatedVolumes: [],
  jobs: [], issues: [], reviewCases: [], validationSummary: null,
};

/** The job type that produces each pipeline step's output. */
const STEP_JOB: Partial<Record<StepId, string>> = {
  extraction: "BUILDING_EXTRACTION",
  floors: "FLOOR_SEGMENTATION",
  volumes: "PROPERTY_VOLUME_GENERATION",
};

/** Job types that are inputs to the pipeline rather than outputs of a step. */
const INPUT_JOB = "METADATA_EXTRACTION";

/**
 * The point-cloud status chain the backend actually reports, most complete last.
 * A source never reads `COMPLETE`: derived geometry is not a cadastral record.
 */
export const SOURCE_STATUS_ORDER = ["METADATA_ONLY", "EXTRACTED", "SEGMENTED", "VOLUMES_GENERATED"] as const;

const has = (rows: AnyRecord[] | undefined | null) => (Array.isArray(rows) ? rows.length : 0);

/** Rows as an array, whatever came back. A failed read yields `null`, and one
 *  unreachable endpoint must degrade its own panel rather than blank the screen,
 *  so nothing downstream may assume a collection is present. */
const rows = (value: AnyRecord[] | undefined | null): AnyRecord[] => (Array.isArray(value) ? value : []);

/** Jobs for one stage, newest first, from the real `processing_jobs` feed. */
function jobsFor(snapshot: WorkflowSnapshot, step: StepId): AnyRecord[] {
  const type = STEP_JOB[step];
  if (!type) return [];
  return rows(snapshot.jobs)
    .filter((job) => job?.job_type === type)
    .sort((a, b) => String(b?.started_at ?? "").localeCompare(String(a?.started_at ?? "")));
}

/**
 * Whether a stage is running, failed, or simply never attempted.
 *
 * A FAILED job is an error, a RUNNING job is running, and anything else means
 * the step has not been attempted -- so a missing job is reported as *empty*,
 * never as *failed*. Inventing a failure that did not happen is the mirror image
 * of inventing a confidence that was not measured.
 */
function jobState(snapshot: WorkflowSnapshot, step: StepId): "running" | "error" | null {
  const jobs = jobsFor(snapshot, step);
  if (jobs.some((job) => job?.status === "RUNNING")) return "running";
  if (jobs.some((job) => job?.status === "FAILED")) return "error";
  return null;
}

/** How far ingestion has got, from the real job feed rather than a guess. */
function ingestionReached(snapshot: WorkflowSnapshot): boolean {
  return jobsFor(snapshot, "extraction").length > 0 || has(snapshot.extractedBuildings) > 0;
}

function identifiers(snapshot: WorkflowSnapshot): { issued: number; of: number } {
  const candidates = [...rows(snapshot.parcels), ...rows(snapshot.properties), ...rows(snapshot.generatedVolumes)];
  return {
    issued: candidates.filter((r) => r?.prototype_ulpin).length,
    of: candidates.length,
  };
}

function reviewCounts(snapshot: WorkflowSnapshot) {
  const cases = rows(snapshot.reviewCases);
  const decided = cases.filter((c) => c && c.state !== "PENDING" && c.state !== "IN_REVIEW");
  return {
    pending: cases.filter((c) => c?.state === "PENDING" || c?.state === "IN_REVIEW").length,
    decided: decided.length,
  };
}

function approvedCounts(snapshot: WorkflowSnapshot) {
  const findings = rows(snapshot.issues);
  const approved = findings.filter((i) => i?.status === "APPROVED");
  return { approved: approved.length, decided: findings.filter((i) => i && i.status && i.status !== "OPEN").length };
}

function step(id: StepId, index: number, label: string, endpoint: string, blurb: string): StepView {
  return { id, index, label, endpoint, blurb, state: "empty", count: null, detail: "" };
}

/**
 * Derive the nine steps from a snapshot.
 *
 * Pure, and the only place the workflow's meaning is decided, so it is testable
 * without a browser and without a network.
 */
export function deriveWorkflow(snapshot: WorkflowSnapshot): StepView[] {
  const s = snapshot;
  const steps: StepView[] = [];

  // 1. Data sources
  const sources = step("sources", 1, "Data Sources", "GET /data-sources", "Files registered for processing");
  sources.count = has(s.sources);
  sources.state = sources.count ? "ready" : "empty";
  const pointClouds = has(s.pointClouds);
  sources.detail = sources.count
    ? `${sources.count} registered, ${pointClouds} point cloud${pointClouds === 1 ? "" : "s"}`
    : "No source ingested yet";
  steps.push(sources);

  // 2. Parcel
  const parcel = step("parcel", 2, "Parcel", "GET /parcels", "Registered land parcels");
  parcel.count = has(s.parcels);
  parcel.state = parcel.count ? "ready" : "empty";
  const buildings = has(s.buildings);
  parcel.detail = parcel.count ? `${parcel.count} parcel${parcel.count === 1 ? "" : "s"}, ${buildings} building${buildings === 1 ? "" : "s"}` : "No parcel registered";
  steps.push(parcel);

  // 3-5. The three pipeline stages, each backed by its own job type.
  const pipeline: Array<[StepId, string, string, AnyRecord[], string]> = [
    ["extraction", "Building Extraction", "GET /extracted-buildings", rows(s.extractedBuildings), "Footprints recovered from points"],
    ["floors", "Floor Segmentation", "GET /extracted-floors", rows(s.extractedFloors), "Storey levels detected from elevation"],
    ["volumes", "Property Volumes", "GET /generated-property-volumes", rows(s.generatedVolumes), "Volumetric rights generated per storey"],
  ];
  for (const [id, label, endpoint, rows, blurb] of pipeline) {
    const view = step(id, 0, label, endpoint, blurb);
    const state = jobState(s, id);
    const count = has(rows);
    view.count = count;
    if (state === "running") { view.state = "running"; view.detail = "Processing job running"; }
    else if (state === "error") {
      view.state = "error";
      const failed = jobsFor(s, id).find((j) => j?.status === "FAILED");
      view.detail = failed?.error ? `Job failed: ${failed.error}` : "Processing job failed";
    } else if (count) {
      const flagged = rows.filter((r) => r?.requires_human_review).length;
      view.state = flagged ? "attention" : "ready";
      view.detail = flagged ? `${count} produced, ${flagged} flagged for human review` : `${count} produced`;
    } else if (has(s.sources) && !ingestionReached(s)) { view.state = "blocked"; view.detail = "Run building extraction first"; }
    else { view.state = "empty"; view.detail = "Not run yet"; }
    steps.push(view);
  }

  // 6. ULPIN
  const ulpin = step("ulpin", 6, "ULPIN", "POST /ulpin/generate", "Stable identifiers, independent of geometry");
  const ids = identifiers(s);
  ulpin.count = ids.issued;
  ulpin.state = ids.of === 0 ? "blocked" : ids.issued === ids.of ? "ready" : "attention";
  ulpin.detail = ids.of ? `${ids.issued} of ${ids.of} objects carry a prototype ULPIN` : "No eligible object yet";
  steps.push(ulpin);

  // 7. Validation
  const validation = step("validation", 7, "Validation", "GET /validation/summary", "30 topology and volumetric rules");
  const summary = s.validationSummary;
  const critical = Number(summary?.by_severity?.CRITICAL ?? rows(s.issues).filter((i) => i?.severity === "CRITICAL").length);
  const findingCount = Number(summary?.total ?? has(s.issues));
  validation.count = findingCount;
  validation.state = findingCount ? (critical ? "attention" : "ready") : has(s.sources) ? "empty" : "blocked";
  validation.detail = findingCount
    ? `${findingCount} finding${findingCount === 1 ? "" : "s"}, ${critical} critical`
    : "No findings recorded";
  steps.push(validation);

  // 8. Human review
  const review = step("review", 8, "Human Review", "GET /reviews", "A person resolves each finding");
  const reviews = reviewCounts(s);
  review.count = reviews.pending;
  review.state = reviews.pending ? "attention" : reviews.decided ? "ready" : findingCount ? "empty" : "blocked";
  review.detail = reviews.decided ? `${reviews.decided} decided, ${reviews.pending} awaiting a reviewer` : reviews.pending ? `${reviews.pending} awaiting a reviewer` : "No finding opened for review";
  steps.push(review);

  // 9. Approved cadastre
  const approved = step("approved", 9, "Approved Cadastre", "GET /reviews?state=APPROVED", "Findings a person has confirmed");
  const approval = approvedCounts(s);
  approved.count = approval.approved;
  approved.state = approval.approved ? "ready" : approval.decided ? "empty" : "blocked";
  approved.detail = approval.approved
    ? `${approval.approved} finding${approval.approved === 1 ? "" : "s"} approved by a reviewer`
    : "Nothing approved yet — a human decision is required";
  steps.push(approved);

  return steps.map((view, index) => ({ ...view, index: index + 1 }));
}

/**
 * Classify a record by what actually produced it.
 *
 * `ml` is returned only when a `model_name` is present, which today it never is.
 * A record that merely has a `method` is `algorithmic`; one flagged for review is
 * `unreviewed` until a person decides, and `human-approved` only once they have.
 */
export function resultKind(record: AnyRecord | null | undefined, issueStatus?: string | null): ResultKind {
  if (issueStatus === "APPROVED") return "human-approved";
  if (record?.model_name) return "ml";
  if (record?.method || record?.extractor) return "algorithmic";
  if (record?.requires_human_review || issueStatus === "HUMAN REVIEW REQUIRED") return "unreviewed";
  return "unreviewed";
}

/** Human-readable, honest label for each result kind. */
export const RESULT_KIND_LABEL: Record<ResultKind, string> = {
  algorithmic: "ALGORITHMIC",
  ml: "ML MODEL",
  "human-approved": "HUMAN APPROVED",
  "unreviewed": "UNREVIEWED",
  validation: "VALIDATION",
};

/**
 * The explanation shown next to a `geometric_quality` figure.
 *
 * It is a regularity measure of the recovered geometry, not an accuracy, and
 * saying otherwise in a tooltip is cheaper than a user repeating it downstream.
 */
export const GEOMETRIC_QUALITY_NOTE =
  "Geometric quality is a regularity measure of the recovered shape. It is not an accuracy, and it is not a confidence in a model.";

/** The one statement this UI must always be able to make about ML. */
export const NO_ML_STATEMENT =
  "No stage in this pipeline uses a trained model. Every derived result is algorithmic and geometric, and is labelled as such.";

/** Per-record provenance a reviewer would want next to a result. */
export function describeProvenance(record: AnyRecord | null | undefined): { method: string | null; quality: number | null; flagged: boolean; model: string | null } {
  return {
    method: record?.method ?? null,
    quality: typeof record?.geometric_quality === "number" ? record.geometric_quality : null,
    flagged: Boolean(record?.requires_human_review),
    model: record?.model_name ?? null,
  };
}

/** Review decisions the UI can offer for a finding, in the order a reviewer takes them. */
export const ISSUE_ACTIONS = [
  { id: "approve", label: "Approve", endpoint: "approve", tone: "positive" },
  { id: "reject", label: "Reject", endpoint: "reject", tone: "negative" },
  { id: "resurvey", label: "Request resurvey", endpoint: "request-resurvey", tone: "warn" },
  { id: "expected", label: "Mark expected", endpoint: "mark-expected", tone: "muted" },
] as const;

export type IssueActionId = (typeof ISSUE_ACTIONS)[number]["id"];

/** Field labels for the source metadata table, in display order. */
export const SOURCE_FIELDS = [
  { key: "id", label: "Source" },
  { key: "source_type", label: "Type" },
  { key: "crs", label: "CRS" },
  { key: "point_count", label: "Point count" },
  { key: "acquisition_date", label: "Acquired" },
  { key: "status", label: "Status" },
] as const;

/**
 * Flatten a source record for the metadata table.
 *
 * `point_count` and `status` are not columns on a source: the count lives in the
 * ingestion metadata, and the status is the pipeline position the source has
 * reached. Reading them from `metadata` rather than inventing them keeps the table
 * honest -- a source with no point cloud shows an em dash, not a zero.
 */
export function sourceRow(source: AnyRecord, cloud?: AnyRecord | null): Record<string, string> {
  const metadata = source?.metadata ?? {};
  return {
    id: String(source?.id ?? "—"),
    source_type: String(source?.source_type ?? "—"),
    crs: String(source?.crs ?? "UNKNOWN"),
    point_count: metadata.point_count != null ? Number(metadata.point_count).toLocaleString() : "—",
    acquisition_date: String(source?.acquisition_date ?? "—"),
    status: String(cloud?.status ?? (metadata.point_cloud ? "INGESTED" : "REGISTERED")),
  };
}
