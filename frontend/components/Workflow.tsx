"use client";
import { useState } from "react";
import { AlertTriangle, CheckCircle2, ChevronRight, CircleDashed, Loader2, Play, XCircle } from "lucide-react";
import { AnyRecord, GEOMETRIC_QUALITY_NOTE, ISSUE_ACTIONS, NO_ML_STATEMENT, RESULT_KIND_LABEL, SOURCE_FIELDS, StepState, StepView, describeProvenance, resultKind, sourceRow } from "../lib/workflow";

/**
 * The nine-step processing workflow.
 *
 * Presentation only. Every count, state and word comes from `deriveWorkflow` in
 * `lib/workflow.ts`, which is driven by the API responses, so the panel cannot
 * drift from the backend: if a number is on screen, there is a request behind it.
 */

const STATE_STYLE: Record<StepState, { dot: string; text: string; label: string }> = {
  empty: { dot: "border-[#3c5b60] bg-[#0b1d24]", text: "text-[#7c9b9b]", label: "NOT RUN" },
  ready: { dot: "border-[#4fc9a6] bg-[#0e2a26]", text: "text-[#a9dcc9]", label: "DONE" },
  running: { dot: "border-[#e0b25c] bg-[#2a2210]", text: "text-[#e8c887]", label: "RUNNING" },
  attention: { dot: "border-[#e8b757] bg-[#2a2210]", text: "text-[#e8c887]", label: "NEEDS REVIEW" },
  error: { dot: "border-[#e8745f] bg-[#2a1410]", text: "text-[#f2a294]", label: "FAILED" },
  blocked: { dot: "border-[#33474c] bg-transparent", text: "text-[#6b8385]", label: "BLOCKED" },
};

function StateIcon({ state }: { state: StepState }) {
  if (state === "running") return <Loader2 size={11} className="animate-spin" />;
  if (state === "ready") return <CheckCircle2 size={11} />;
  if (state === "error") return <XCircle size={11} />;
  if (state === "attention") return <AlertTriangle size={11} />;
  return <CircleDashed size={11} />;
}

export function WorkflowRail({ steps, busyStep, onRun }: { steps: StepView[]; busyStep?: string | null; onRun?: (step: StepView) => void }) {
  return (
    <div>
      <div className="flex items-baseline justify-between">
        <span className="text-[10px] font-bold tracking-[.15em] text-[#87adac]">PROCESSING WORKFLOW</span>
        <span className="text-[9px] text-[#5f8082]">9 stages &middot; every count from the API</span>
      </div>
      <ol className="mt-2 flex items-stretch gap-0">
        {steps.map((step, index) => {
          const style = STATE_STYLE[step.state];
          const runnable = onRun && (step.state === "empty" || step.state === "blocked") && ["extraction", "floors", "volumes"].includes(step.id);
          return (
            <li key={step.id} className="flex min-w-0 flex-1 items-center">
              <div
                className={`min-w-0 flex-1 border px-1.5 py-1.5 ${style.dot} ${style.text}`}
                title={`${step.blurb}\n${step.endpoint}\n${step.detail}`}
              >
                <div className="flex items-center gap-1">
                  <StateIcon state={step.state} />
                  <span className="text-[8px] tabular-nums opacity-70">{step.index}</span>
                  {runnable ? (
                    <button
                      onClick={() => onRun?.(step)}
                      disabled={busyStep === step.id}
                      className="ml-auto rounded p-0.5 hover:bg-[#1c4a4c]"
                      title={`Run ${step.label}`}
                    >
                      {busyStep === step.id ? <Loader2 size={10} className="animate-spin" /> : <Play size={10} />}
                    </button>
                  ) : null}
                </div>
                <div className="mt-0.5 truncate text-[9px] font-semibold leading-tight">{step.label}</div>
                <div className="text-[11px] font-bold tabular-nums text-[#dff3ee]">{step.count ?? "—"}</div>
                <div className="truncate text-[8px] opacity-75">{style.label}</div>
              </div>
              {index < steps.length - 1 ? <ChevronRight size={9} className="shrink-0 text-[#2f4a4f]" /> : null}
            </li>
          );
        })}
      </ol>
      <p className="mt-1.5 text-[9px] leading-4 text-[#5f8082]">
        {steps.find((s) => s.id === "extraction")?.detail} &middot; {steps.find((s) => s.id === "floors")?.detail}
      </p>
    </div>
  );
}

export function JobPanel({ jobs, busy, error }: { jobs: AnyRecord[]; busy?: string | null; error?: string | null }) {
  const ordered = [...jobs].sort((a, b) => String(b?.started_at ?? "").localeCompare(String(a?.started_at ?? ""))).slice(0, 8);
  return (
    <div className="min-h-0">
      <div className="mb-1.5 flex items-center justify-between">
        <span className="text-[10px] font-bold tracking-[.15em] text-[#87adac]">PROCESSING JOBS</span>
        <span className="text-[9px] text-[#5f8082]">GET /processing-jobs</span>
      </div>
      {error ? <p className="border border-[#5c3a34] bg-[#1d1210] px-2 py-1 text-[9px] text-[#f2a294]">{error}</p> : null}
      {!error && ordered.length === 0 ? (
        <p className="border border-dashed border-[#2f4a4f] px-2 py-3 text-center text-[9px] text-[#6b8385]">
          No processing job recorded. Upload a point cloud, then run building extraction.
        </p>
      ) : null}
      <div className="max-h-[132px] space-y-1 overflow-y-auto thin-scroll">
        {ordered.map((job) => {
          const failed = job?.status === "FAILED";
          const running = job?.status === "RUNNING";
          return (
            <div key={job.id} className="border border-[#253f45] bg-[#091c22] px-2 py-1">
              <div className="flex items-center gap-1.5">
                {running ? <Loader2 size={10} className="animate-spin text-[#e8c887]" /> : failed ? <XCircle size={10} className="text-[#f2a294]" /> : <CheckCircle2 size={10} className="text-[#6fd0b0]" />}
                <span className="truncate text-[9px] font-semibold text-[#c6dedb]">{String(job?.job_type ?? "").replaceAll("_", " ")}</span>
                <span className={`ml-auto shrink-0 text-[8px] ${failed ? "text-[#f2a294]" : running ? "text-[#e8c887]" : "text-[#6fd0b0]"}`}>{String(job?.status ?? "")}</span>
              </div>
              <div className="mt-0.5 flex gap-2 text-[8px] text-[#6b8385]">
                <span className="truncate">{job?.detail ?? job?.error ?? "—"}</span>
                {job?.point_count ? <span className="ml-auto shrink-0 tabular-nums">{Number(job.point_count).toLocaleString()} pts</span> : null}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export function SourceTable({ sources, clouds, error }: { sources: AnyRecord[]; clouds: AnyRecord[]; error?: string | null }) {
  const cloudById = new Map(clouds.map((c) => [c.source_id ?? c.id, c]));
  return (
    <div className="min-h-0">
      <div className="mb-1.5 flex items-center justify-between">
        <span className="text-[10px] font-bold tracking-[.15em] text-[#87adac]">DATA SOURCES</span>
        <span className="text-[9px] text-[#5f8082]">GET /data-sources</span>
      </div>
      {error ? <p className="border border-[#5c3a34] bg-[#1d1210] px-2 py-1 text-[9px] text-[#f2a294]">{error}</p> : null}
      {!error && sources.length === 0 ? (
        <p className="border border-dashed border-[#2f4a4f] px-2 py-3 text-center text-[9px] text-[#6b8385]">No source ingested.</p>
      ) : null}
      {sources.length ? (
        <div className="max-h-[132px] overflow-y-auto thin-scroll">
          <table className="w-full border-collapse text-[9px]">
            <thead>
              <tr className="text-left text-[8px] uppercase tracking-wider text-[#5f8082]">
                {SOURCE_FIELDS.map((field) => (
                  <th key={field.key} className="border-b border-[#22393f] px-1 py-1 font-semibold">{field.label}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {sources.map((source) => {
                const row = sourceRow(source, cloudById.get(source.id) ?? null);
                return (
                  <tr key={row.id} className="text-[#bcd6d3]">
                    {SOURCE_FIELDS.map((field) => (
                      <td key={field.key} className="border-b border-[#16292e] px-1 py-1 tabular-nums">
                        {field.key === "id" ? <span className="font-semibold text-[#8fe0cd]">{row.id}</span> : row[field.key]}
                      </td>
                    ))}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : null}
    </div>
  );
}

export function ResultProvenance({ record, status }: { record: AnyRecord; status?: string | null }) {
  const kind = resultKind(record, status);
  const { method, quality, flagged, model } = describeProvenance(record);
  return (
    <div className="mt-2 border border-[#2a464d] bg-[#0a1e25] p-2">
      <div className="flex items-center gap-1.5">
        <span className={`border px-1.5 py-0.5 text-[8px] font-bold tracking-wider ${kind === "human-approved" ? "border-[#4fc9a6] text-[#8fe0cd]" : kind === "ml" ? "border-[#c58ae0] text-[#e3bdf2]" : "border-[#4a6a6e] text-[#a6c4c2]"}`}>
          {RESULT_KIND_LABEL[kind]}
        </span>
        {method ? <span className="truncate text-[8px] text-[#6b8385]">{method}</span> : null}
        {model ? <span className="text-[8px] text-[#e3bdf2]">{model}</span> : null}
        {flagged ? <span className="ml-auto text-[8px] text-[#e8c887]">FLAGGED FOR REVIEW</span> : null}
      </div>
      {quality !== null ? (
        <div className="mt-1 text-[8px] text-[#6b8385]" title={GEOMETRIC_QUALITY_NOTE}>
          Geometric quality {quality.toFixed(3)} &mdash; regularity measure, not an accuracy
        </div>
      ) : null}
    </div>
  );
}

export function IssueActions({
  issue,
  reviewCase,
  busy,
  onInspect,
  onFocus,
  onOpenReview,
  onDecide,
  reason,
  setReason,
}: {
  issue: AnyRecord;
  reviewCase?: AnyRecord | null;
  busy?: string | null;
  onInspect: (issue: AnyRecord) => void;
  onFocus: (issue: AnyRecord) => void;
  onOpenReview: (issue: AnyRecord) => void;
  onDecide: (issue: AnyRecord, action: string) => void;
  reason: string;
  setReason: (value: string) => void;
}) {
  const [showReasons, setShowReasons] = useState(false);
  const decided = reviewCase && reviewCase.state && !["PENDING", "IN_REVIEW"].includes(reviewCase.state);
  return (
    <div className="mt-2 border border-[#2a464d] bg-[#0a1e25] p-2">
      <div className="flex items-center justify-between">
        <span className="truncate text-[9px] font-semibold text-[#c6dedb]">{issue.id}</span>
        <span className={`shrink-0 text-[8px] ${issue.severity === "CRITICAL" ? "text-[#fc806d]" : issue.severity === "WARNING" ? "text-[#e8b85e]" : "text-[#8fc6dd]"}`}>{issue.severity}</span>
      </div>
      <div className="mt-1 flex flex-wrap gap-1">
        <button onClick={() => onInspect(issue)} className="border border-[#3d626a] px-1.5 py-0.5 text-[9px] text-[#bcd6d3] hover:border-[#62cdbd]">Inspect</button>
        <button onClick={() => onFocus(issue)} className="border border-[#3d626a] px-1.5 py-0.5 text-[9px] text-[#bcd6d3] hover:border-[#62cdbd]">Focus on map</button>
        {decided ? (
          <span className="border border-[#3d7a68] bg-[#0e2a26] px-1.5 py-0.5 text-[9px] text-[#8fe0cd]">{reviewCase.state}</span>
        ) : (
          <button onClick={() => onOpenReview(issue)} className="border border-[#d3a94f] bg-[#241d0e] px-1.5 py-0.5 text-[9px] text-[#e8c887] hover:border-[#e8b757]">
            {reviewCase ? "Review" : "Open review"}
          </button>
        )}
      </div>
      {!decided ? (
        <>
          <button onClick={() => setShowReasons((v) => !v)} className="mt-1 text-[8px] text-[#5f8082] underline">
            {showReasons ? "hide decision reason" : "record a decision"}
          </button>
          {showReasons ? (
            <div className="mt-1">
              <input
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                placeholder="Reason (required by the API)"
                className="h-6 w-full border border-[#35535a] bg-[#0b2028] px-2 text-[9px] text-[#d3e2e0] outline-none placeholder:text-[#5f8082] focus:border-[#62cdbd]"
              />
              <div className="mt-1 flex flex-wrap gap-1">
                {ISSUE_ACTIONS.map((action) => (
                  <button
                    key={action.id}
                    onClick={() => onDecide(issue, action.endpoint)}
                    disabled={!reason.trim() || busy === action.id}
                    title={!reason.trim() ? "A reason is required for every review decision" : undefined}
                    className={`border px-1.5 py-0.5 text-[9px] disabled:cursor-not-allowed disabled:opacity-40 ${
                      action.tone === "positive"
                        ? "border-[#4fc9a6] text-[#8fe0cd] hover:bg-[#123430]"
                        : action.tone === "negative"
                        ? "border-[#a4574a] text-[#f2a294] hover:bg-[#2a1410]"
                        : action.tone === "warn"
                          ? "border-[#d3a94f] text-[#e8c887] hover:bg-[#241d0e]"
                          : "border-[#4a6a6e] text-[#a6c4c2] hover:bg-[#0f262c]"
                    }`}
                  >
                    {busy === action.id ? "…" : action.label}
                  </button>
                ))}
              </div>
            </div>
          ) : null}
        </>
      ) : null}
    </div>
  );
}

export function MlDisclosure() {
  return (
    <p className="mt-1.5 border border-[#2a464d] bg-[#0a1e25] px-2 py-1 text-[8px] leading-3.5 text-[#7c9b9b]">
      {NO_ML_STATEMENT}
    </p>
  );
}
