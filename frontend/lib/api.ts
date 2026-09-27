export const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
export async function api<T>(path:string, init?:RequestInit):Promise<T> {
  const res = await fetch(`${API}${path}`, { ...init, headers:{"Content-Type":"application/json", ...(init?.headers || {})}, cache:"no-store" });
  if (!res.ok) { const detail = await res.json().catch(()=>({})); throw new Error(detail.detail || `Request failed (${res.status})`); }
  return res.json();
}

type AnyRecord = Record<string, any>;

/**
 * Every endpoint the command centre reads, in one place.
 *
 * The point of naming them is auditability: each field below is a real route on
 * the API, and `WorkflowSnapshot` cannot be assembled from anything else. A
 * figure with no endpoint behind it has no business on screen.
 */
export const ENDPOINTS = {
  sources: "/data-sources",
  pointClouds: "/point-clouds",
  pointCloud: (id: string) => `/point-clouds/${id}`,
  parcels: "/parcels",
  buildings: "/buildings",
  properties: "/properties",
  infrastructure: "/infrastructure",
  extractedBuildings: "/extracted-buildings",
  extractedFloors: "/extracted-floors",
  generatedVolumes: "/generated-property-volumes",
  jobs: "/processing-jobs",
  issues: "/validation/issues",
  validationSummary: "/validation/summary",
  reviews: "/reviews",
  changes: "/changes",
  reviewDecisions: (ident: string) => `/reviews/${ident}/decisions`,
  analytics: "/analytics/summary",
  demoLoad: "/demo/load",
  ulpinGenerate: "/ulpin/generate",
  validate: "/validation/run",
} as const;

/** Routes the workflow drives, keyed by the step they belong to. */
export const PIPELINE = {
  extract: (id: string) => `/point-clouds/${id}/extract`,
  segment: (id: string) => `/point-clouds/${id}/segment-floors`,
  volumes: (id: string) => `/point-clouds/${id}/property-volumes`,
} as const;

/** Review decisions, by the action id the UI uses. */
export const REVIEW_ACTIONS = {
  assign: (ident: string) => `/reviews/${ident}/assign`,
  approve: (ident: string) => `/reviews/${ident}/approve`,
  reject: (ident: string) => `/reviews/${ident}/reject`,
  resurvey: (ident: string) => `/reviews/${ident}/request-resurvey`,
  expected: (ident: string) => `/reviews/${ident}/mark-expected`,
  close: (issueId: string) => `/reviews/issues/${issueId}/close`,
  reopen: (issueId: string) => `/reviews/issues/${issueId}/reopen`,
  open: (issueId: string) => `/reviews?issue_id=${encodeURIComponent(issueId)}`,
} as const;

/**
 * Fetch everything the command centre shows, in parallel.
 *
 * `Promise.all` so one slow endpoint does not serialise the rest, but
 * `Promise.allSettled` semantics are applied per-endpoint below: a single failing
 * read degrades that panel rather than blanking the whole screen, which is the
 * difference between "the job list failed" and "the application is broken".
 */
export async function loadWorkspace(): Promise<{
  data: Record<string, AnyRecord[] | AnyRecord | null>;
  failures: Array<{ key: string; message: string }>;
}> {
  const reads: Array<[string, string]> = [
    ["sources", ENDPOINTS.sources],
    ["pointClouds", ENDPOINTS.pointClouds],
    ["parcels", ENDPOINTS.parcels],
    ["buildings", ENDPOINTS.buildings],
    ["properties", ENDPOINTS.properties],
    ["infrastructure", ENDPOINTS.infrastructure],
    ["extractedBuildings", ENDPOINTS.extractedBuildings],
    ["extractedFloors", ENDPOINTS.extractedFloors],
    ["generatedVolumes", ENDPOINTS.generatedVolumes],
    ["jobs", ENDPOINTS.jobs],
    ["issues", ENDPOINTS.issues],
    ["reviewCases", ENDPOINTS.reviews],
    ["validationSummary", ENDPOINTS.validationSummary],
    ["analytics", ENDPOINTS.analytics],
  ];
  const settled = await Promise.all(
    reads.map(async ([key, path]) => {
      try {
        return { key, value: await api<AnyRecord[] | AnyRecord>(path), error: null as string | null };
      } catch (error) {
        return { key, value: null, error: error instanceof Error ? error.message : "Request failed" };
      }
    }),
  );
  const data: Record<string, AnyRecord[] | AnyRecord | null> = {};
  const failures: Array<{ key: string; message: string }> = [];
  for (const entry of settled) {
    data[entry.key] = entry.value;
    if (entry.error) failures.push({ key: entry.key, message: entry.error });
  }
  return { data, failures };
}

/** Run one pipeline stage against a source. */
export async function runStage(path: string): Promise<AnyRecord> {
  return api<AnyRecord>(path, { method: "POST", body: "{}" });
}

/** Upload a file as a new data source. */
export async function uploadFile(file: File, crs?: string): Promise<AnyRecord> {
  const form = new FormData();
  form.append("file", file);
  const query = crs ? `?crs=${encodeURIComponent(crs)}` : "";
  const res = await fetch(`${API}/import/source${query}`, { method: "POST", body: form });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || "Import failed");
  return body as AnyRecord;
}

/** Record a review decision. `reason` is mandatory at the API, and should be here. */
export async function decide(
  path: string,
  reason: string,
  reviewer?: string,
): Promise<AnyRecord> {
  return api<AnyRecord>(path, {
    method: "POST",
    body: JSON.stringify({ reason, reviewer }),
  });
}

/** Fetch the per-source pipeline status, which carries the point count and position. */
export async function pointCloudStatus(sourceId: string): Promise<AnyRecord> {
  return api<AnyRecord>(ENDPOINTS.pointCloud(sourceId));
}
