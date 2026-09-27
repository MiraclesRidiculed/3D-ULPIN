"use client";
import { useState } from "react";
import { AlertTriangle, Check, CircleDot, GitBranch, Layers, Maximize2, Ruler, X } from "lucide-react";
import {
  COMPARISON_CAPTION,
  ChangeMetrics,
  LineageStage,
  NeutralStatus,
  ObjectLineage,
  STATUS_TONE,
  changeOverlays,
  showAfterGeometry,
  showBeforeGeometry,
  showChangeMetrics,
  showValidationGeometry,
  highlightChangedGeometry,
} from "../lib/compare.ts";
import type { AnyRecord } from "../lib/compare.ts";

/**
 * The verification panels.
 *
 * Presentation only -- every value comes from `lib/compare.ts`, so the panels
 * cannot show a number the derivation did not produce. Wording is neutral
 * throughout: a change is a difference between two measurements, and the labels
 * say so.
 */

function StatusChip({ status }: { status: NeutralStatus }) {
  return (
    <span
      className="border px-1.5 py-0.5 text-[8px] font-bold tracking-wider"
      style={{ borderColor: STATUS_TONE[status], color: STATUS_TONE[status] }}
    >
      {status}
    </span>
  );
}

function MetricGroupPanel({ group }: { group: ChangeMetrics["previous"] }) {
  return (
    <div className="min-w-0 flex-1 border border-[#253f45] bg-[#091c22] p-1.5">
      <div className="text-[8px] font-bold tracking-[.14em] text-[#6b8385]">{group.title.toUpperCase()}</div>
      <dl className="mt-1 space-y-0.5">
        {group.rows.map((row) => (
          <div key={row.label} className="flex items-baseline justify-between gap-1" title={row.absentReason}>
            <dt className="truncate text-[8px] text-[#6b8385]">{row.label}</dt>
            <dd
              className={`shrink-0 text-[9px] tabular-nums ${row.present ? "text-[#c6dedb]" : "text-[#4d6a6d]"}`}
              title={row.present ? undefined : (row.absentReason ?? "not recorded")}
            >
              {row.value}
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

/** The Previous / Current / Difference comparison, plus the three map toggles. */
export function ChangeInspector({
  change,
  reviewCase,
  layers,
  onToggleLayer,
  onClose,
}: {
  change: AnyRecord | null;
  reviewCase?: AnyRecord | null;
  layers: Record<string, boolean>;
  onToggleLayer: (key: string) => void;
  onClose: () => void;
}) {
  const metrics = showChangeMetrics(change, reviewCase);
  const views = change
    ? {
        before: showBeforeGeometry(change),
        after: showAfterGeometry(change),
        changed: highlightChangedGeometry(change),
      }
    : null;

  if (!change) {
    return (
      <div className="border border-dashed border-[#2f4a4f] p-3 text-center text-[9px] text-[#6b8385]">
        Select a change to compare previous and current geometry.
      </div>
    );
  }

  return (
    <div className="border border-[#2f4a4f] bg-[#08191f] p-2">
      <div className="flex items-center gap-2">
        <span className="truncate text-[10px] font-bold text-[#dff3ee]">{metrics.objectId}</span>
        <span className="shrink-0 text-[8px] text-[#6b8385]">{metrics.changeType.replaceAll("_", " ")}</span>
        <StatusChip status={metrics.status} />
        <button onClick={onClose} className="ml-auto shrink-0 text-[#5f8082] hover:text-[#d8e6e6]" aria-label="Close comparison">
          <X size={12} />
        </button>
      </div>

      {/* The three map layers, each reporting honestly when it has nothing. */}
      <div className="mt-1.5 flex flex-wrap gap-1">
        {views ? (
          (["before", "after", "changed"] as const).map((key) => {
            const view = views[key];
            return (
              <button
                key={key}
                onClick={() => onToggleLayer(key)}
                disabled={!view.drawable}
                title={view.drawable ? view.reason ?? `Show ${view.label}` : (view.reason ?? "nothing to draw")}
                className={`border px-1.5 py-0.5 text-[9px] disabled:cursor-not-allowed disabled:opacity-45 ${
                  layers[key] ? "border-[#4fc9a6] text-[#8fe0cd]" : "border-[#3d626a] text-[#a6c4c2]"
                }`}
              >
                <span className="mr-1 inline-block h-2 w-2 align-middle" style={{ background: view.colour }} />
                {view.label}
              </button>
            );
          })
        ) : null}
        {views?.changed.reason ? <span className="self-center text-[8px] text-[#5f8082]">{views.changed.reason}</span> : null}
      </div>

      <div className="mt-2 flex gap-2">
        <MetricGroupPanel group={metrics.previous} />
        <MetricGroupPanel group={metrics.current} />
        <MetricGroupPanel group={metrics.difference} />
      </div>

      {metrics.provenance.method ? (
        <div className="mt-1.5 flex items-center gap-1.5 text-[8px] text-[#6b8385]">
          <Ruler size={9} />
          <span>{metrics.provenance.method}</span>
          {metrics.provenance.quality !== null ? (
            <span title="Geometric quality is a regularity measure of the recovered shape, not an accuracy.">
              · regularity {metrics.provenance.quality.toFixed(3)}
            </span>
          ) : null}
        </div>
      ) : null}

      <p className="mt-1.5 text-[8px] leading-3.5 text-[#7c9b9b]">{metrics.statement}</p>
      <p className="mt-1 text-[8px] leading-3.5 text-[#5f8082]">{COMPARISON_CAPTION}</p>
    </div>
  );
}

/** The lineage chain, in the backend's own stage order. */
export function LineagePanel({ lineage, loading }: { lineage: ObjectLineage | null; loading?: boolean }) {
  if (loading) {
    return (
      <div className="flex items-center gap-2 border border-[#2f4a4f] bg-[#08191f] p-2 text-[9px] text-[#6b8385]">
        <CircleDot size={10} className="animate-pulse" /> Reading lineage…
      </div>
    );
  }
  if (!lineage) {
    return <div className="border border-dashed border-[#2f4a4f] p-2 text-center text-[9px] text-[#6b8385]">Select an object to trace its origin.</div>;
  }
  return (
    <div className="border border-[#2f4a4f] bg-[#08191f] p-2">
      <div className="flex items-center gap-1.5">
        <GitBranch size={11} className="text-[#69cbbd]" />
        <span className="text-[10px] font-bold text-[#dff3ee]">{lineage.objectId}</span>
        <span className="ml-auto text-[8px] text-[#6b8385]">{lineage.complete ? "complete chain" : "origin not recorded"}</span>
      </div>
      {lineage.stages.length ? (
        <ol className="mt-1.5 space-y-0.5">
          {lineage.stages.map((stage: LineageStage, index: number) => (
            <li key={stage.stage} className="flex items-center gap-1.5 text-[8px]">
              <span className="w-3 shrink-0 text-[#4d6a6d]">{index + 1}</span>
              <span className="shrink-0 text-[#6b8385]">{stage.stage.replaceAll("_", " ")}</span>
              <span className="truncate text-[#a6c4c2]">{stage.objectId}</span>
              {stage.model ? <span className="ml-auto shrink-0 text-[#c58ae0]">{stage.model}</span> : null}
            </li>
          ))}
        </ol>
      ) : (
        <p className="mt-1 text-[8px] text-[#6b8385]">No recorded provenance for this object.</p>
      )}
      {lineage.note ? <p className="mt-1 text-[8px] leading-3.5 text-[#5f8082]">{lineage.note}</p> : null}
      <p className="mt-1 text-[8px] leading-3.5 text-[#5f8082]">
        Every stage here is algorithmic. No stage in this pipeline uses a trained model.
      </p>
    </div>
  );
}

/** The validation-finding list, with the map geometry each one carries. */
export function ChangeList({
  changes,
  selectedId,
  onSelect,
}: {
  changes: AnyRecord[];
  selectedId: string | null;
  onSelect: (change: AnyRecord) => void;
}) {
  const [showAll, setShowAll] = useState(false);
  const shown = showAll ? changes : changes.slice(0, 6);
  return (
    <div className="min-h-0">
      <div className="mb-1.5 flex items-center justify-between">
        <span className="text-[10px] font-bold tracking-[.15em] text-[#87adac]">DETECTED CHANGES</span>
        <span className="text-[9px] text-[#5f8082]">GET /changes</span>
      </div>
      {changes.length === 0 ? (
        <p className="border border-dashed border-[#2f4a4f] px-2 py-3 text-center text-[9px] text-[#6b8385]">
          No change detected against the approved cadastre.
        </p>
      ) : (
        <>
          <div className="max-h-[104px] space-y-0.5 overflow-y-auto thin-scroll">
            {shown.map((change) => {
              const view = highlightChangedGeometry(change);
              const active = selectedId === change.id;
              return (
                <button
                  key={change.id}
                  onClick={() => onSelect(change)}
                  className={`flex w-full items-center gap-1.5 border-l-2 px-1.5 py-1 text-left text-[9px] hover:bg-[#103038] ${
                    active ? "border-[#62cdbd] bg-[#103038] text-[#dff3ee]" : "border-transparent text-[#a8c0bf]"
                  }`}
                >
                  <AlertTriangle size={10} className={view.drawable ? "text-[#e0b25c]" : "text-[#4d6a6d]"} />
                  <span className="truncate">{change.change_type?.replaceAll("_", " ")}</span>
                  <span className="truncate text-[#6b8385]">{change.object_id}</span>
                  <span
                    className="ml-auto shrink-0"
                    title={view.drawable ? "this change has geometry to draw" : (view.reason ?? "no geometry")}
                  >
                    {view.drawable ? <Check size={9} className="text-[#6fd0b0]" /> : <X size={9} className="text-[#4d6a6d]" />}
                  </span>
                </button>
              );
            })}
          </div>
          {changes.length > 6 ? (
            <button onClick={() => setShowAll((v) => !v)} className="mt-1 text-[8px] text-[#5f8082] underline">
              {showAll ? "show fewer" : `show all ${changes.length}`}
            </button>
          ) : null}
        </>
      )}
    </div>
  );
}

/** Whether a change can actually be drawn, used by the parent to build the overlay. */
export function useOverlayFor(change: AnyRecord | null, layers: Record<string, boolean>) {
  if (!change) return [];
  return changeOverlays(change).filter((view) => {
    const key = view.label === "Previous" ? "before" : view.label === "Current" ? "after" : "changed";
    return layers[key];
  });
}
