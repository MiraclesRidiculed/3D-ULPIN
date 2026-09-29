"use client";

import dynamic from "next/dynamic";
import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import {
  Activity,
  AlertTriangle,
  Boxes,
  Building2,
  CheckCircle2,
  ChevronRight,
  ClipboardCheck,
  Database,
  GitBranch,
  Layers3,
  Loader2,
  MapPinned,
  Network,
  RefreshCw,
  Ruler,
  Search,
  ShieldCheck,
  Target,
  TowerControl,
  Upload,
  X,
  type LucideIcon,
} from "lucide-react";
import {
  ENDPOINTS,
  PIPELINE,
  REVIEW_ACTIONS,
  api,
  decide,
  loadWorkspace,
  runStage,
  uploadFile,
} from "../lib/api";
import {
  GEOMETRIC_QUALITY_NOTE,
  deriveWorkflow,
  emptySnapshot,
} from "../lib/workflow";
import type { StepView, WorkflowSnapshot } from "../lib/workflow";
import {
  IssueActions,
  JobPanel,
  MlDisclosure,
  ResultProvenance,
  WorkflowRail,
} from "../components/Workflow";
import {
  ChangeInspector,
  ChangeList,
  LineagePanel,
  useOverlayFor,
} from "../components/ChangeInspector";
import { showObjectLineage } from "../lib/compare.ts";
import type { ObjectLineage } from "../lib/compare.ts";
import type { AnyRecord } from "../lib/compare.ts";

const CesiumMap = dynamic(() => import("../components/CesiumMap"), {
  ssr: false,
  loading: () => (
    <div className="grid h-full place-items-center text-xs text-[#91adac]">
      <span className="flex items-center gap-2"><Loader2 className="animate-spin" size={16} /> Initialising 3D workspace…</span>
    </div>
  ),
});

type RecordAny = Record<string, any>;
type Section =
  | "overview"
  | "sources"
  | "parcels"
  | "buildings"
  | "floors"
  | "properties"
  | "infrastructure"
  | "validation"
  | "analysis"
  | "imports"
  | "ulpin"
  | "provenance"
  | "review"
  | "changes";
type Workspace = WorkflowSnapshot & {
  floors: RecordAny[];
  changes: RecordAny[];
  infrastructure: RecordAny[];
  analytics: RecordAny | null;
};

const emptyWorkspace: Workspace = {
  ...emptySnapshot,
  floors: [],
  changes: [],
  infrastructure: [],
  analytics: null,
};

const SECTION_ITEMS: Array<{ id: Section; label: string; icon: LucideIcon; endpoint: string }> = [
  { id: "overview", label: "Overview", icon: Activity, endpoint: "GET /analytics/summary" },
  { id: "sources", label: "Data Sources", icon: Database, endpoint: "GET /data-sources" },
  { id: "parcels", label: "Parcels", icon: MapPinned, endpoint: "GET /parcels" },
  { id: "buildings", label: "Buildings", icon: Building2, endpoint: "GET /buildings" },
  { id: "floors", label: "Floors", icon: Layers3, endpoint: "GET /floors · /extracted-floors" },
  { id: "properties", label: "Properties", icon: Boxes, endpoint: "GET /properties" },
  { id: "infrastructure", label: "Infrastructure", icon: TowerControl, endpoint: "GET /infrastructure" },
  { id: "validation", label: "Validation", icon: AlertTriangle, endpoint: "GET /validation/issues" },
  { id: "analysis", label: "Analysis", icon: Network, endpoint: "GET /analytics/summary" },
  { id: "imports", label: "Imports", icon: Upload, endpoint: "POST /import/source" },
  { id: "ulpin", label: "ULPIN", icon: Search, endpoint: "GET /ulpin/{prototype_ulpin}" },
  { id: "provenance", label: "Provenance", icon: GitBranch, endpoint: "GET /provenance/{object_id}/lineage" },
  { id: "review", label: "Review", icon: ClipboardCheck, endpoint: "GET /reviews" },
  { id: "changes", label: "Change Detection", icon: Ruler, endpoint: "GET /changes" },
];

const rows = (value: unknown): RecordAny[] => Array.isArray(value) ? value : [];
const recordId = (item: RecordAny | null | undefined) =>
  String(item?.id ?? item?.source_id ?? item?.object_id ?? "");
const hasGeometry = (item: RecordAny | null | undefined) =>
  Boolean(item?.geometry_3d || item?.geometry || item?.footprint);
const displayValue = (value: unknown): string => {
  if (value === null || value === undefined || value === "") return "Not recorded";
  if (typeof value === "object") {
    try {
      return JSON.stringify(value);
    } catch {
      return "Recorded";
    }
  }
  return String(value);
};

function recordTitle(item: RecordAny, section?: Section): string {
  if (section === "sources" || item.filename) return String(item.filename ?? item.id ?? "Data source");
  if (section === "validation" || item.issue_type) {
    return `${item.issue_type ?? "Finding"} · ${item.id ?? ""}`.trim();
  }
  if (item.unit_label) return String(item.unit_label);
  if (item.floor_label) return String(item.floor_label);
  if (item.building_id && item.floor_number !== undefined) {
    return `${item.building_id} · Floor ${item.floor_number}`;
  }
  if (item.building_id) return String(item.building_id);
  return String(
    item.prototype_ulpin ??
      item.parcel_id ??
      item.building_id ??
      item.type ??
      item.id ??
      "Record",
  );
}

function recordSubtitle(item: RecordAny): string {
  const parts = [
    item.source_type ?? item.type ?? item.property_type ?? item.object_type,
    item.status ?? item.state ?? item.severity,
    item.crs ? `CRS ${item.crs}` : null,
    item.point_count !== undefined ? `${item.point_count} points` : null,
    item.parcel_id ?? item.parent_parcel_id,
    item.floor_number !== undefined ? `Floor ${item.floor_number}` : null,
  ].filter((value) => value !== null && value !== undefined && value !== "");
  return parts.map(String).join(" · ") || `ID ${recordId(item) || "not recorded"}`;
}

function RecordList({
  title,
  endpoint,
  items,
  section,
  selectedId,
  onSelect,
  emptyText,
}: {
  title: string;
  endpoint: string;
  items: RecordAny[];
  section: Section;
  selectedId: string;
  onSelect: (item: RecordAny) => void;
  emptyText: string;
}) {
  return (
    <section className="min-w-0">
      <div className="mb-2 flex items-baseline justify-between gap-2">
        <h3 className="text-[11px] font-semibold tracking-[.12em] text-[#b4cfcc]">{title}</h3>
        <span className="shrink-0 text-[9px] text-[#5f8082]">{endpoint} · {items.length}</span>
      </div>
      {items.length === 0 ? (
        <p className="border border-dashed border-[#2f4a4f] px-3 py-4 text-center text-[10px] leading-4 text-[#718d8e]">{emptyText}</p>
      ) : (
        <ul className="space-y-1">
          {items.map((item, index) => {
            const id = recordId(item) || `${section}-${index}`;
            const active = id === selectedId;
            return (
              <li key={`${section}-${id}`}>
                <button
                  type="button"
                  onClick={() => onSelect(item)}
                  className={`w-full border-l-2 px-2.5 py-2 text-left transition ${
                    active
                      ? "border-[#62cdbd] bg-[#103038] text-[#e0f3ef]"
                      : "border-transparent bg-[#091c22] text-[#afc6c4] hover:border-[#42666b] hover:bg-[#0d252d]"
                  }`}
                  aria-pressed={active}
                >
                  <span className="flex items-center gap-2">
                    <span className="min-w-0 flex-1 truncate text-[10px] font-medium">{recordTitle(item, section)}</span>
                    {hasGeometry(item) ? <MapPinned size={11} className="shrink-0 text-[#62cdbd]" aria-label="Has spatial geometry" /> : null}
                    <ChevronRight size={12} className="shrink-0 text-[#527176]" />
                  </span>
                  <span className="mt-1 block truncate text-[9px] text-[#6f9192]">{recordSubtitle(item)}</span>
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

function MetricCard({ label, value }: { label: string; value: unknown }) {
  return (
    <div className="border border-[#253f45] bg-[#091c22] px-3 py-2">
      <div className="text-[17px] font-semibold tabular-nums text-[#dff3ee]">{displayValue(value)}</div>
      <div className="mt-0.5 text-[8px] tracking-[.12em] text-[#6f9192]">{label}</div>
    </div>
  );
}

function Inspector({ item, canFocus, onFocus }: { item: RecordAny; canFocus: boolean; onFocus: () => void }) {
  const height = item.height ?? item.height_m ??
    (typeof item.z_min === "number" && typeof item.z_max === "number" ? item.z_max - item.z_min : null);
  const fields: Array<[string, unknown]> = [
    ["Prototype 3D ULPIN", item.prototype_ulpin],
    ["Property ID", item.id],
    ["Parcel", item.parent_parcel_id ?? item.parcel_id],
    ["Building", item.building_id],
    ["Floor", item.floor_label ?? item.floor_number],
    ["Type", item.property_type ?? item.type],
    ["z_min", item.z_min],
    ["z_max", item.z_max],
    ["Height", height],
    ["Area", item.area_m2 ?? item.area],
    ["Volume", item.volume_m3],
    ["Geometry version", item.geometry_version],
    ["Geometry hash", item.geometry_hash],
    ["Method", item.method ?? item.extractor],
    ["Extractor version", item.extractor_version],
    ["Source", item.source_id],
    ["Processing job", item.processing_job_id],
    ["Provenance", item.source_provenance ?? item.provenance],
    ["CRS", item.crs],
    ["Format", item.format],
    ["Point count", item.point_count],
    ["Source bounds", item.bounds ?? item.source_bounds],
    ["File size (bytes)", item.file_size_bytes ?? item.bytes],
    ["Source metadata", item.metadata],
  ];
  const propertyVolume = Boolean(
    item.property_type !== undefined ||
    item.unit_label !== undefined ||
    item.volume_m3 !== undefined,
  );
  const propertyFields = new Set([
    "Prototype 3D ULPIN",
    "Property ID",
    "Parcel",
    "Building",
    "Floor",
    "Type",
    "z_min",
    "z_max",
    "Height",
    "Area",
    "Volume",
    "Geometry version",
  ]);
  const visibleFields = fields.filter(([label, value]) =>
    propertyVolume
      ? propertyFields.has(label) || (value !== null && value !== undefined && value !== "")
      : value !== null && value !== undefined && value !== "",
  );
  const provenanceRecord = item.method || item.extractor || item.geometric_quality !== undefined || item.model_name;
  return (
    <section className="border border-[#31525a] bg-[#091d24]">
      <div className="flex items-start gap-2 border-b border-[#223b41] px-3 py-2.5">
        <div className="min-w-0 flex-1">
          <div className="truncate text-[11px] font-semibold text-[#e1f0ee]">{recordTitle(item)}</div>
          <div className="mt-0.5 break-all text-[8px] text-[#6f9192]">{recordId(item)}</div>
        </div>
        {canFocus ? (
          <button
            type="button"
            onClick={onFocus}
            className="flex shrink-0 items-center gap-1 border border-[#4b7777] px-2 py-1 text-[9px] font-semibold text-[#a8e4d7] hover:bg-[#12383a]"
            title="Move the camera to this record; selection itself never moves the camera"
          >
            <Target size={11} /> FOCUS
          </button>
        ) : null}
      </div>
      <dl className="grid grid-cols-[minmax(95px,.8fr)_minmax(0,1.2fr)] gap-x-2 px-3 py-2">
        {visibleFields.map(([label, value]) => (
          <div key={label} className="contents">
            <dt className="border-b border-[#172d32] py-1.5 text-[9px] text-[#6f9192]">{label}</dt>
            <dd className="min-w-0 break-words border-b border-[#172d32] py-1.5 text-[9px] text-[#c1d7d4]">{displayValue(value)}</dd>
          </div>
        ))}
      </dl>
      {provenanceRecord ? (
        <div className="px-2 pb-2">
          <ResultProvenance record={item} status={item.status} />
          <p className="mt-1 text-[8px] leading-3 text-[#637f80]" title={GEOMETRIC_QUALITY_NOTE}>
            Geometric quality, when recorded, is a regularity measure—not an accuracy.
          </p>
        </div>
      ) : null}
      {item.issue_type ? (
        <div className="border-t border-[#223b41] px-3 py-2">
          <div className="mb-1 text-[9px] font-semibold text-[#b4cfcc]">{item.issue_type} · {item.severity}</div>
          <p className="text-[9px] leading-4 text-[#9bb8b5]">{item.description ?? "No description recorded."}</p>
          {item.rule_id ? <div className="mt-1 text-[8px] text-[#6f9192]">Rule {item.rule_id} · {item.category ?? "category not recorded"}</div> : null}
        </div>
      ) : null}
    </section>
  );
}

export default function CommandCenter() {
  const [workspace, setWorkspace] = useState<Workspace>(emptyWorkspace);
  const [active, setActive] = useState<Section>("overview");
  const [selected, setSelected] = useState<RecordAny | null>(null);
  const [selectedSection, setSelectedSection] = useState<Section | null>(null);
  const [mapSelection, setMapSelection] = useState<RecordAny | null>(null);
  const [focus, setFocus] = useState<RecordAny | null>(null);
  const [floor, setFloor] = useState<number | "all">("all");
  const [underground, setUnderground] = useState(true);
  const [shownSourceId, setShownSourceId] = useState<string | null>(null);
  const [failures, setFailures] = useState<Array<{ key: string; message: string }>>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");
  const [query, setQuery] = useState("");
  const [reason, setReason] = useState("");
  const [selectedIssue, setSelectedIssue] = useState<RecordAny | null>(null);
  const [selectedChange, setSelectedChange] = useState<RecordAny | null>(null);
  const [layers, setLayers] = useState<Record<string, boolean>>({ before: true, after: true, changed: true });
  const [lineage, setLineage] = useState<ObjectLineage | null>(null);
  const [lineageStages, setLineageStages] = useState<string[]>([]);
  const [lineageQuery, setLineageQuery] = useState("");
  const [lineageError, setLineageError] = useState("");
  const [lineageBusy, setLineageBusy] = useState(false);
  const [importResult, setImportResult] = useState<{ filename: string; response: RecordAny; source: RecordAny | null } | null>(null);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const { data, failures: requestFailures } = await loadWorkspace();
      const next: Workspace = {
        sources: rows(data.sources),
        pointClouds: rows(data.pointClouds),
        parcels: rows(data.parcels),
        buildings: rows(data.buildings),
        floors: rows(data.floors),
        properties: rows(data.properties),
        infrastructure: rows(data.infrastructure),
        extractedBuildings: rows(data.extractedBuildings),
        extractedFloors: rows(data.extractedFloors),
        generatedVolumes: rows(data.generatedVolumes),
        jobs: rows(data.jobs),
        issues: rows(data.issues),
        reviewCases: rows(data.reviewCases),
        changes: rows(data.changes),
        validationSummary: data.validationSummary && !Array.isArray(data.validationSummary)
          ? data.validationSummary
          : null,
        analytics: data.analytics && !Array.isArray(data.analytics) ? data.analytics : null,
      };
      setWorkspace(next);
      setFailures(requestFailures);
      return next;
    } catch (error) {
      const message = error instanceof Error ? error.message : "Unable to load the workspace.";
      setNotice(message);
      return null;
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
    api<{ stages: string[] }>(ENDPOINTS.provenanceStages)
      .then((result) => setLineageStages(Array.isArray(result.stages) ? result.stages : []))
      .catch((error) => setLineageError(error instanceof Error ? error.message : "Could not read provenance stages."));
  }, [reload]);

  const steps = useMemo(() => deriveWorkflow(workspace), [workspace]);
  const failureByKey = useMemo(() => new Map(failures.map((failure) => [failure.key, failure.message])), [failures]);
  const issueCounts = useMemo(() => ({
    critical: workspace.issues.filter((issue) => issue?.severity === "CRITICAL").length,
    warning: workspace.issues.filter((issue) => issue?.severity === "WARNING").length,
    info: workspace.issues.filter((issue) => issue?.severity === "INFO").length,
  }), [workspace.issues]);
  const floorOptions = useMemo(() => {
    const levels = new Set<number>();
    for (const record of [...workspace.properties, ...workspace.floors, ...workspace.extractedFloors]) {
      if (typeof record?.floor_number === "number") levels.add(record.floor_number);
    }
    return Array.from(levels).sort((a, b) => a - b);
  }, [workspace.properties, workspace.floors, workspace.extractedFloors]);

  const visibleDerived = useMemo(() => {
    if (!shownSourceId) return { buildings: [] as RecordAny[], properties: [] as RecordAny[] };
    const buildings: RecordAny[] = workspace.extractedBuildings
      .filter((record) => record.source_id === shownSourceId && record.footprint)
      .map((record): RecordAny => ({ ...record, height: record.height_m, _derived_kind: "building" }));
    const properties: RecordAny[] = [
      ...workspace.extractedFloors.filter((record) => record.source_id === shownSourceId && record.geometry_3d),
      ...workspace.generatedVolumes.filter((record) => record.source_id === shownSourceId && record.geometry_3d),
    ];
    return { buildings, properties };
  }, [shownSourceId, workspace.extractedBuildings, workspace.extractedFloors, workspace.generatedVolumes]);
  const mapBuildings = useMemo(() => [...workspace.buildings, ...visibleDerived.buildings], [workspace.buildings, visibleDerived.buildings]);
  const mapProperties = useMemo(() => [...workspace.properties, ...visibleDerived.properties], [workspace.properties, visibleDerived.properties]);

  const changeOverlay = useOverlayFor(selectedChange, layers).map((view) => ({
    geometry: view.geometry,
    zMin: view.zMin,
    zMax: view.zMax,
    color: view.colour,
    label: view.label,
  }));
  const findingOverlay = selectedSection === "validation" && selected?.geometry
    ? [{ geometry: selected.geometry, zMin: 0, zMax: 0, color: "#e0b25c", label: "Validation finding" }]
    : [];
  const overlays = [...changeOverlay, ...findingOverlay];

  const findSpatialRecord = useCallback((item: RecordAny, section?: Section): RecordAny | null => {
    if (hasGeometry(item)) return item;
    const candidates = [
      ...mapProperties,
      ...mapBuildings,
      ...workspace.parcels,
      ...workspace.infrastructure,
    ];
    if (section === "floors" && item.building_id) {
      const building = mapBuildings.find((candidate) =>
        candidate.id === item.building_id || candidate.building_id === item.building_id,
      );
      if (building) return building;
    }
    const keys = [
      item.object_a,
      item.object_b,
      item.object_id,
      item.building_id,
      item.parcel_id,
      item.parent_parcel_id,
      item.prototype_ulpin,
      item.id,
    ].filter((value) => value !== null && value !== undefined).map(String);
    return candidates.find((candidate) => [
      candidate.id,
      candidate.parcel_id,
      candidate.building_id,
      candidate.parent_parcel_id,
      candidate.prototype_ulpin,
      candidate.unit_label,
    ].some((value) => value !== null && value !== undefined && keys.includes(String(value)))) ?? null;
  }, [mapBuildings, mapProperties, workspace.infrastructure, workspace.parcels]);

  const showLineage = useCallback(async (objectId: string) => {
    if (!objectId) {
      setLineage(null);
      setLineageError("");
      return;
    }
    setLineageQuery(objectId);
    setLineageBusy(true);
    setLineageError("");
    try {
      const response = await api<RecordAny>(`/provenance/${encodeURIComponent(objectId)}/lineage`);
      setLineage(showObjectLineage(response, lineageStages));
    } catch (error) {
      setLineage(null);
      setLineageError(error instanceof Error ? error.message : "Could not load this object's provenance.");
    } finally {
      setLineageBusy(false);
    }
  }, [lineageStages]);

  const selectRecord = useCallback((item: RecordAny, section: Section) => {
    setSelected(item);
    setSelectedSection(section);
    setMapSelection(findSpatialRecord(item, section));
    if (
      item.source_id &&
      ["buildings", "floors", "properties"].includes(section) &&
      [...workspace.extractedBuildings, ...workspace.extractedFloors, ...workspace.generatedVolumes]
        .some((record) => record.id === item.id)
    ) {
      setShownSourceId(String(item.source_id));
    }
    if (section === "validation") setSelectedIssue(item);
    const objectId = String(item.id ?? item.object_id ?? item.parcel_id ?? item.building_id ?? "");
    if (objectId) void showLineage(objectId);
  }, [findSpatialRecord, showLineage, workspace.extractedBuildings, workspace.extractedFloors, workspace.generatedVolumes]);

  const sectionForMapRecord = useCallback((record: RecordAny): Section => {
    const id = recordId(record);
    if (workspace.parcels.some((item) => recordId(item) === id)) return "parcels";
    if (workspace.infrastructure.some((item) => recordId(item) === id)) return "infrastructure";
    if (workspace.buildings.some((item) => recordId(item) === id) || visibleDerived.buildings.some((item) => recordId(item) === id)) return "buildings";
    if (workspace.extractedFloors.some((item) => recordId(item) === id) || workspace.floors.some((item) => recordId(item) === id)) return "floors";
    return "properties";
  }, [visibleDerived.buildings, workspace.buildings, workspace.extractedFloors, workspace.floors, workspace.infrastructure, workspace.parcels]);

  const pickOnMap = useCallback((record: RecordAny) => {
    const section = sectionForMapRecord(record);
    setActive(section);
    selectRecord(record, section);
  }, [sectionForMapRecord, selectRecord]);

  const doAction = useCallback(async (label: string, path: string, method: "GET" | "POST" = "POST") => {
    setBusy(label);
    setNotice("");
    try {
      const result = await api<RecordAny>(path, { method, ...(method === "POST" ? { body: "{}" } : {}) });
      await reload();
      setNotice(result?.message ?? result?.note ?? `${label} completed.`);
    } catch (error) {
      setNotice(`${label} failed: ${error instanceof Error ? error.message : "Request failed"}`);
    } finally {
      setBusy("");
    }
  }, [reload]);

  const onSearch = async (event: FormEvent) => {
    event.preventDefault();
    const ident = query.trim();
    if (!ident) return;
    setBusy("ULPIN lookup");
    setNotice("");
    try {
      const result = await api<RecordAny>(ENDPOINTS.ulpinLookup(ident));
      setActive("ulpin");
      selectRecord(result.record, "ulpin");
      setNotice(`Found ${result.kind}: ${recordTitle(result.record)}.`);
    } catch (error) {
      setNotice(`Lookup failed: ${error instanceof Error ? error.message : "Request failed"}`);
    } finally {
      setBusy("");
    }
  };

  const runWorkflowStep = async (step: StepView) => {
    const cloud = workspace.pointClouds[0];
    if (!cloud?.source_id) {
      setNotice("No point cloud is registered. Register a supported LAS, LAZ, or PLY source first.");
      return;
    }
    const path = step.id === "extraction"
      ? PIPELINE.extract(cloud.source_id)
      : step.id === "floors"
        ? PIPELINE.segment(cloud.source_id)
        : step.id === "volumes"
          ? PIPELINE.volumes(cloud.source_id)
          : null;
    if (!path) return;
    setBusy(step.label);
    setNotice("");
    try {
      const result = await runStage(path);
      await reload();
      setNotice(result?.note ?? `${step.label} completed.`);
    } catch (error) {
      setNotice(`${step.label} failed: ${error instanceof Error ? error.message : "Request failed"}`);
    } finally {
      setBusy("");
    }
  };

  const selectIssue = (issue: RecordAny) => selectRecord(issue, "validation");
  const reviewFor = (issueId: string) =>
    workspace.reviewCases.find((review) => review?.issue_id === issueId) ?? null;

  const openReview = async (issue: RecordAny) => {
    setBusy("Open review");
    setNotice("");
    try {
      await api(REVIEW_ACTIONS.open(issue.id), {
        method: "POST",
        body: JSON.stringify({ reason: reason.trim() || "Finding requires human verification." }),
      });
      await reload();
      setNotice(`Review opened for ${issue.id}.`);
    } catch (error) {
      setNotice(`Could not open review: ${error instanceof Error ? error.message : "Request failed"}`);
    } finally {
      setBusy("");
    }
  };

  const decideIssue = async (issue: RecordAny, action: string) => {
    const reviewCase = reviewFor(issue.id);
    const ident = reviewCase?.id ?? issue.id;
    const path = action === "request-resurvey"
      ? REVIEW_ACTIONS.resurvey(ident)
      : action === "mark-expected"
        ? REVIEW_ACTIONS.expected(ident)
        : action === "approve"
          ? REVIEW_ACTIONS.approve(ident)
          : REVIEW_ACTIONS.reject(ident);
    setBusy(action);
    setNotice("");
    try {
      await decide(path, reason.trim());
      setReason("");
      await reload();
      setNotice(`Decision recorded for ${issue.id}.`);
    } catch (error) {
      setNotice(`Decision failed: ${error instanceof Error ? error.message : "Request failed"}`);
    } finally {
      setBusy("");
    }
  };

  const uploadSource = async (file?: File) => {
    if (!file) return;
    setBusy("Import source");
    setNotice("");
    try {
      const response = await uploadFile(file);
      const next = await reload();
      const source =
        rows(next?.sources).find((item) => item.filename === file.name) ??
        rows(next?.sources).find((item) => item.id === response.source_id) ??
        null;
      setImportResult({ filename: file.name, response, source });
      setNotice(response.note ?? `${file.name} was accepted for registration.`);
    } catch (error) {
      setNotice(`Import failed: ${error instanceof Error ? error.message : "Request failed"}`);
    } finally {
      setBusy("");
    }
  };

  const showSourceOnMap = (source: RecordAny) => {
    const sourceId = String(source.id ?? source.source_id ?? "");
    const derived = [
      ...workspace.extractedBuildings.filter((item) => item.source_id === sourceId && item.footprint),
      ...workspace.extractedFloors.filter((item) => item.source_id === sourceId && item.geometry_3d),
      ...workspace.generatedVolumes.filter((item) => item.source_id === sourceId && item.geometry_3d),
    ];
    if (!derived.length) {
      setNotice("No derived spatial records are available for this source. Registration alone does not visualize imported geometry.");
      return;
    }
    setShownSourceId(sourceId);
    setNotice(`${derived.length} derived spatial record${derived.length === 1 ? "" : "s"} from ${source.filename ?? sourceId} added to the map. Use FOCUS to move the camera.`);
  };

  const traceLineage = async (event: FormEvent) => {
    event.preventDefault();
    await showLineage(lineageQuery.trim());
  };

  const startFocus = () => {
    if (!mapSelection) return;
    setFocus({
      ...mapSelection,
      geometry_3d: mapSelection.geometry_3d ?? mapSelection.geometry ?? mapSelection.footprint,
    });
  };

  const selectedChangeCase = selectedChange?.finding_id
    ? reviewFor(selectedChange.finding_id)
    : null;
  const activeSection = SECTION_ITEMS.find((item) => item.id === active) ?? SECTION_ITEMS[0];
  const displayedSelectedId = recordId(selected);
  const sourceDerivedForImport = importResult?.source
    ? [
        ...workspace.extractedBuildings.filter((record) => record.source_id === importResult.source?.id && record.footprint),
        ...workspace.extractedFloors.filter((record) => record.source_id === importResult.source?.id && record.geometry_3d),
        ...workspace.generatedVolumes.filter((record) => record.source_id === importResult.source?.id && record.geometry_3d),
      ]
    : [];

  const renderSection = () => {
    switch (active) {
      case "overview":
        return (
          <div className="space-y-4">
            {failureByKey.get("analytics") ? <ErrorNotice message={failureByKey.get("analytics")!} /> : null}
            <div className="grid grid-cols-2 gap-2">
              <MetricCard label="PARCELS · /analytics/summary" value={workspace.analytics?.parcels} />
              <MetricCard label="BUILDINGS · /analytics/summary" value={workspace.analytics?.buildings} />
              <MetricCard label="PROPERTY VOLUMES · /analytics/summary" value={workspace.analytics?.property_volumes} />
              <MetricCard label="INFRASTRUCTURE · /analytics/summary" value={workspace.analytics?.underground_assets} />
              <MetricCard label="FINDINGS · /analytics/summary" value={workspace.analytics?.validation_issues} />
              <MetricCard label="CRITICAL · /analytics/summary" value={workspace.analytics?.critical_conflicts} />
            </div>
            <div className="border-t border-[#263f44] pt-3">
              <div className="mb-2 flex items-center justify-between">
                <h3 className="text-[10px] font-semibold tracking-[.12em] text-[#b4cfcc]">PROCESSING WORKFLOW</h3>
                <span className="text-[8px] text-[#5f8082]">Counts from API records</span>
              </div>
              <div className="overflow-x-auto pb-1">
                <div className="min-w-[640px]">
                  <WorkflowRail steps={steps} busyStep={busy || null} onRun={runWorkflowStep} />
                </div>
              </div>
              <MlDisclosure />
            </div>
            <div className="border-t border-[#263f44] pt-3">
              <RecordList
                title="Open findings"
                endpoint="GET /validation/issues"
                items={workspace.issues.slice(0, 6)}
                section="validation"
                selectedId={selectedSection === "validation" ? displayedSelectedId : ""}
                onSelect={selectIssue}
                emptyText="No validation findings are currently stored."
              />
            </div>
            <JobPanel jobs={workspace.jobs} error={failureByKey.get("jobs")} />
          </div>
        );
      case "sources":
        return (
          <div className="space-y-4">
            {failureByKey.get("sources") ? <ErrorNotice message={failureByKey.get("sources")!} /> : null}
            <RecordList
              title="Registered sources"
              endpoint="GET /data-sources"
              items={workspace.sources}
              section="sources"
              selectedId={selectedSection === "sources" ? displayedSelectedId : ""}
              onSelect={(record) => selectRecord(record, "sources")}
              emptyText="No data sources are registered."
            />
            <div className="border-t border-[#263f44] pt-3">
              <RecordList
                title="Inspected point clouds"
                endpoint="GET /point-clouds"
                items={workspace.pointClouds}
                section="sources"
                selectedId={selectedSection === "sources" ? displayedSelectedId : ""}
                onSelect={(record) => selectRecord(record, "sources")}
                emptyText="No point-cloud metadata is available."
              />
              {workspace.pointClouds.map((cloud) => {
                const source = workspace.sources.find((item) => item.id === cloud.source_id) ?? cloud;
                const derivedCount = [
                  ...workspace.extractedBuildings,
                  ...workspace.extractedFloors,
                  ...workspace.generatedVolumes,
                ].filter((item) => item.source_id === cloud.source_id && hasGeometry(item)).length;
                return (
                  <div key={cloud.source_id} className="mt-2 flex items-center justify-between gap-2 border border-[#29434a] bg-[#08191f] px-2 py-2">
                    <span className="min-w-0 truncate text-[9px] text-[#819f9e]">{cloud.status ?? "Status not recorded"} · {derivedCount} derived spatial records</span>
                    <button
                      type="button"
                      onClick={() => showSourceOnMap(source)}
                      disabled={!derivedCount}
                      className="shrink-0 border border-[#3d626a] px-2 py-1 text-[8px] text-[#a8e4d7] disabled:cursor-not-allowed disabled:opacity-40"
                    >
                      SHOW ON MAP
                    </button>
                  </div>
                );
              })}
            </div>
            <JobPanel jobs={workspace.jobs} error={failureByKey.get("jobs")} />
          </div>
        );
      case "parcels":
        return <CollectionPanel title="Parcels" endpoint="GET /parcels" items={workspace.parcels} section="parcels" selectedId={selectedSection === active ? displayedSelectedId : ""} onSelect={selectRecord} error={failureByKey.get("parcels")} emptyText="No parcels were returned by the API." />;
      case "buildings":
        return (
          <div className="space-y-4">
            {failureByKey.get("buildings") ? <ErrorNotice message={failureByKey.get("buildings")!} /> : null}
            <RecordList title="Cadastral buildings" endpoint="GET /buildings" items={workspace.buildings} section="buildings" selectedId={selectedSection === active ? displayedSelectedId : ""} onSelect={(record) => selectRecord(record, "buildings")} emptyText="No cadastral buildings were returned by the API." />
            <RecordList title="Point-cloud extracted buildings" endpoint="GET /extracted-buildings" items={workspace.extractedBuildings} section="buildings" selectedId={selectedSection === active ? displayedSelectedId : ""} onSelect={(record) => selectRecord(record, "buildings")} emptyText="No extracted buildings are available." />
          </div>
        );
      case "floors":
        return (
          <div className="space-y-4">
            {failureByKey.get("floors") ? <ErrorNotice message={failureByKey.get("floors")!} /> : null}
            <RecordList title="Registered floor bands" endpoint="GET /floors" items={workspace.floors} section="floors" selectedId={selectedSection === active ? displayedSelectedId : ""} onSelect={(record) => selectRecord(record, "floors")} emptyText="No registered floor bands are available." />
            <RecordList title="Point-cloud extracted storeys" endpoint="GET /extracted-floors" items={workspace.extractedFloors} section="floors" selectedId={selectedSection === active ? displayedSelectedId : ""} onSelect={(record) => selectRecord(record, "floors")} emptyText="No extracted storeys are available. Run floor segmentation for an inspected point cloud." />
            <p className="text-[8px] leading-4 text-[#5f8082]">A registered floor band may have elevation data without its own plan geometry; in that case selection highlights its parent building where available.</p>
          </div>
        );
      case "properties":
        return (
          <div className="space-y-4">
            {failureByKey.get("properties") ? <ErrorNotice message={failureByKey.get("properties")!} /> : null}
            <RecordList title="Cadastral property volumes" endpoint="GET /properties" items={workspace.properties} section="properties" selectedId={selectedSection === active ? displayedSelectedId : ""} onSelect={(record) => selectRecord(record, "properties")} emptyText="No cadastral property volumes were returned by the API." />
            <section className="border-t border-[#263f44] pt-3">
              <p className="mb-2 text-[8px] leading-4 text-[#6f9192]">Generated geometry is derived output only; it does not assert ownership, tenancy, or title.</p>
              <RecordList title="Generated spatial volumes" endpoint="GET /generated-property-volumes" items={workspace.generatedVolumes} section="properties" selectedId={selectedSection === active ? displayedSelectedId : ""} onSelect={(record) => selectRecord(record, "properties")} emptyText="No generated volumes are available." />
            </section>
          </div>
        );
      case "infrastructure":
        return <CollectionPanel title="Infrastructure" endpoint="GET /infrastructure" items={workspace.infrastructure} section="infrastructure" selectedId={selectedSection === active ? displayedSelectedId : ""} onSelect={selectRecord} error={failureByKey.get("infrastructure")} emptyText="No infrastructure records were returned by the API." />;
      case "validation":
        return (
          <div className="space-y-3">
            <div className="flex items-center justify-between gap-2">
              <span className="text-[9px] text-[#6f9192]">Stored findings from GET /validation/issues</span>
              <button type="button" onClick={() => void doAction("Run validation", ENDPOINTS.validate)} disabled={Boolean(busy)} className="border border-[#4b7777] px-2 py-1.5 text-[9px] font-semibold text-[#a8e4d7] disabled:opacity-50">
                {busy === "Run validation" ? "RUNNING…" : "RUN VALIDATION"}
              </button>
            </div>
            {failureByKey.get("issues") ? <ErrorNotice message={failureByKey.get("issues")!} /> : null}
            <div className="grid grid-cols-3 gap-1.5">
              <MetricCard label="CRITICAL" value={issueCounts.critical} />
              <MetricCard label="WARNING" value={issueCounts.warning} />
              <MetricCard label="INFO" value={issueCounts.info} />
            </div>
            <RecordList title="Findings" endpoint="GET /validation/issues" items={workspace.issues} section="validation" selectedId={selectedSection === active ? displayedSelectedId : ""} onSelect={selectIssue} emptyText="No findings are currently stored." />
            {selectedIssue ? (
              <IssueActions
                issue={selectedIssue}
                reviewCase={reviewFor(selectedIssue.id)}
                busy={busy || null}
                onInspect={(issue) => selectIssue(issue)}
                onFocus={(issue) => {
                  const target = findSpatialRecord(issue, "validation") ?? issue;
                  if (hasGeometry(target)) setFocus({ ...target, geometry_3d: target.geometry_3d ?? target.geometry ?? target.footprint });
                }}
                onOpenReview={openReview}
                onDecide={decideIssue}
                reason={reason}
                setReason={setReason}
              />
            ) : null}
          </div>
        );
      case "analysis":
        return (
          <div className="space-y-4">
            {failureByKey.get("analytics") ? <ErrorNotice message={failureByKey.get("analytics")!} /> : null}
            <div className="grid grid-cols-2 gap-2">
              <MetricCard label="PARCELS · /analytics/summary" value={workspace.analytics?.parcels} />
              <MetricCard label="BUILDINGS · /analytics/summary" value={workspace.analytics?.buildings} />
              <MetricCard label="PROPERTY VOLUMES · /analytics/summary" value={workspace.analytics?.property_volumes} />
              <MetricCard label="APARTMENTS · /analytics/summary" value={workspace.analytics?.apartments} />
              <MetricCard label="UNDERGROUND ASSETS · /analytics/summary" value={workspace.analytics?.underground_assets} />
              <MetricCard label="CRITICAL FINDINGS · /analytics/summary" value={workspace.analytics?.critical_conflicts} />
            </div>
            <div className="border-t border-[#263f44] pt-3">
              <h3 className="mb-2 text-[10px] font-semibold tracking-[.12em] text-[#b4cfcc]">VALIDATION SUMMARY</h3>
              {failureByKey.get("validationSummary") ? <ErrorNotice message={failureByKey.get("validationSummary")!} /> : (
                <SummaryGroups summary={workspace.validationSummary} />
              )}
            </div>
            <div className="border-t border-[#263f44] pt-3">
              <p className="text-[8px] leading-4 text-[#6f9192]">Analytics are read from GET /analytics/summary. Only API-backed record counts are shown; no accuracy or confidence figure is published.</p>
            </div>
          </div>
        );
      case "imports":
        return (
          <div className="space-y-4">
            <section>
              <h3 className="text-[11px] font-semibold tracking-[.12em] text-[#b4cfcc]">REGISTER A SOURCE</h3>
              <p className="mt-1 text-[9px] leading-4 text-[#718d8e]">Upload GeoJSON, JSON, CSV, LAS/LAZ, or PLY. A successful registration is not the same as importing or displaying cadastral geometry.</p>
              <label className="mt-3 flex cursor-pointer items-center justify-center gap-2 border border-dashed border-[#4b7777] bg-[#0b242a] px-3 py-3 text-[10px] text-[#b7d6d2] hover:bg-[#103038]">
                {busy === "Import source" ? <Loader2 size={13} className="animate-spin" /> : <Upload size={13} />}
                {busy === "Import source" ? "REGISTERING…" : "CHOOSE FILE"}
                <input
                  className="sr-only"
                  type="file"
                  accept=".geojson,.json,.csv,.las,.laz,.ply"
                  disabled={Boolean(busy)}
                  onChange={(event) => {
                    void uploadSource(event.target.files?.[0]);
                    event.currentTarget.value = "";
                  }}
                />
              </label>
            </section>
            {importResult ? (
              <section className="border border-[#31525a] bg-[#091d24] p-3">
                <div className="flex items-center gap-2 text-[10px] font-semibold text-[#a8e4d7]">
                  <CheckCircle2 size={13} /> Upload accepted / source registered
                </div>
                <div className="mt-1 break-all text-[9px] text-[#c1d7d4]">{importResult.filename}</div>
                <div className="mt-2 text-[9px] leading-4 text-[#819f9e]">{importResult.response.note ?? "Registration succeeded."}</div>
                <p className="mt-2 border-t border-[#223b41] pt-2 text-[8px] leading-4 text-[#6f9192]">
                  {importResult.response.point_count !== undefined
                    ? `Point-cloud header inspected: ${displayValue(importResult.response.point_count)} points. This does not mean derived geometry is visible.`
                    : "The import route registers the source; it does not parse generic uploaded features into the active cadastral scene."}
                </p>
                {sourceDerivedForImport.length ? (
                  <button type="button" onClick={() => importResult.source && showSourceOnMap(importResult.source)} className="mt-2 border border-[#4b7777] px-2 py-1.5 text-[9px] font-semibold text-[#a8e4d7]">SHOW ON MAP · {sourceDerivedForImport.length}</button>
                ) : (
                  <p className="mt-2 text-[8px] text-[#819f9e]">No derived spatial output is available yet. Run an implemented processing stage first; registration alone is not visualization.</p>
                )}
              </section>
            ) : null}
            <RecordList title="Registered sources" endpoint="GET /data-sources" items={workspace.sources} section="sources" selectedId={selectedSection === "sources" ? displayedSelectedId : ""} onSelect={(record) => selectRecord(record, "sources")} emptyText="No data sources are registered." />
            {failureByKey.get("sources") ? <ErrorNotice message={failureByKey.get("sources")!} /> : null}
          </div>
        );
      case "ulpin":
        return (
          <div className="space-y-3">
            <p className="border border-[#604d2a] bg-[#211d12] px-3 py-2 text-[9px] leading-4 text-[#d8c18b]">
              Prototype identifiers are deterministic demo values, not official Government of India ULPINs and not connected to a land-record service.
            </p>
            <form onSubmit={onSearch} className="flex gap-1.5">
              <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Prototype ULPIN or record ID" className="h-8 min-w-0 flex-1 border border-[#35535a] bg-[#08191f] px-2 text-[10px] text-[#d9e6e7] outline-none focus:border-[#62cdbd]" />
              <button className="border border-[#4b7777] px-2 text-[9px] text-[#a8e4d7]" disabled={Boolean(busy)}><Search size={12} /></button>
            </form>
            <button type="button" onClick={() => void doAction("Generate prototype identifiers", ENDPOINTS.ulpinGenerate)} disabled={Boolean(busy)} className="w-full border border-[#3d626a] px-2 py-2 text-[9px] text-[#a8c8c5] disabled:opacity-50">
              {busy === "Generate prototype identifiers" ? "GENERATING…" : "GENERATE MISSING PROTOTYPE IDENTIFIERS"}
            </button>
            <p className="text-[8px] leading-4 text-[#5f8082]">Lookup uses GET /ulpin/{`{prototype_ulpin}`}; issuance uses POST /ulpin/generate. The selected record synchronizes with Cesium when it has matching geometry.</p>
          </div>
        );
      case "provenance":
        return (
          <div className="space-y-3">
            <form onSubmit={traceLineage} className="flex gap-1.5">
              <input value={lineageQuery} onChange={(event) => setLineageQuery(event.target.value)} placeholder="Object ID" className="h-8 min-w-0 flex-1 border border-[#35535a] bg-[#08191f] px-2 text-[10px] text-[#d9e6e7] outline-none focus:border-[#62cdbd]" />
              <button className="border border-[#3d626a] px-2 text-[9px] text-[#a8c8c5]" disabled={lineageBusy}>{lineageBusy ? <Loader2 size={12} className="animate-spin" /> : "TRACE"}</button>
            </form>
            {lineageError ? <ErrorNotice message={lineageError} /> : null}
            <LineagePanel lineage={lineage} loading={lineageBusy} />
            {lineageStages.length ? <p className="text-[8px] leading-4 text-[#5f8082]">Stage order from GET /provenance/stages: {lineageStages.join(" → ")}</p> : null}
            <p className="text-[8px] leading-4 text-[#5f8082]">A missing origin is reported as incomplete; no source or processing step is inferred.</p>
          </div>
        );
      case "review": {
        const issue = selectedIssue ?? (selected ? workspace.issues.find((item) => item.id === selected.id) ?? null : null);
        return (
          <div className="space-y-3">
            {failureByKey.get("reviewCases") ? <ErrorNotice message={failureByKey.get("reviewCases")!} /> : null}
            <RecordList
              title="Pending review cases"
              endpoint="GET /reviews"
              items={workspace.reviewCases}
              section="review"
              selectedId={selectedSection === active ? displayedSelectedId : ""}
              onSelect={(review) => {
                const finding = workspace.issues.find((item) => item.id === review.issue_id);
                setSelectedIssue(finding ?? null);
                selectRecord(finding ?? review, finding ? "validation" : "review");
                setActive("review");
              }}
              emptyText="No pending review cases were returned."
            />
            {issue ? (
              <IssueActions
                issue={issue}
                reviewCase={reviewFor(issue.id)}
                busy={busy || null}
                onInspect={(finding) => selectIssue(finding)}
                onFocus={(finding) => {
                  const target = findSpatialRecord(finding, "validation") ?? finding;
                  if (hasGeometry(target)) setFocus({ ...target, geometry_3d: target.geometry_3d ?? target.geometry ?? target.footprint });
                }}
                onOpenReview={openReview}
                onDecide={decideIssue}
                reason={reason}
                setReason={setReason}
              />
            ) : <p className="border border-dashed border-[#2f4a4f] px-3 py-3 text-center text-[9px] text-[#718d8e]">Select a case to inspect its finding and available decision actions.</p>}
            <p className="text-[8px] leading-4 text-[#5f8082]">Review actors are self-asserted by the current API; no authentication is implemented.</p>
          </div>
        );
      }
      case "changes":
        return (
          <div className="space-y-3">
            {failureByKey.get("changes") ? <ErrorNotice message={failureByKey.get("changes")!} /> : null}
            <ChangeList
              changes={workspace.changes}
              selectedId={selectedChange?.id ?? null}
              onSelect={(change) => {
                setSelectedChange(change);
                setSelected(change);
                setSelectedSection("changes");
                setMapSelection(findSpatialRecord(change, "changes"));
                if (change.object_id) void showLineage(String(change.object_id));
              }}
            />
            <ChangeInspector
              change={selectedChange}
              reviewCase={selectedChangeCase}
              layers={layers}
              onToggleLayer={(key) => setLayers((previous) => ({ ...previous, [key]: !previous[key] }))}
              onClose={() => {
                setSelectedChange(null);
                setSelectedSection(null);
              }}
            />
            <p className="text-[8px] leading-4 text-[#5f8082]">A change is a measured difference and requires human verification. Selecting it updates the map overlay without moving the camera.</p>
          </div>
        );
    }
  };

  const currentDerivedCount = shownSourceId
    ? visibleDerived.buildings.length + visibleDerived.properties.length
    : 0;

  return (
    <main className="grid h-dvh min-h-[560px] min-w-[980px] grid-cols-[170px_minmax(0,1fr)_340px] grid-rows-[50px_minmax(0,1fr)_34px] bg-[#061119] text-[#d8e6e6]">
      <header className="col-span-3 flex min-w-0 items-center gap-3 border-b border-[#28434b] bg-[#081820] px-3">
        <div className="flex shrink-0 items-center gap-2">
          <div className="grid h-7 w-7 place-items-center border border-[#5ac3b5] bg-[#0c3538] text-[#74e0d0]"><Layers3 size={15} /></div>
          <div>
            <div className="text-[11px] font-bold tracking-[.17em] text-white">V-CAD</div>
            <div className="text-[7px] tracking-[.12em] text-[#78a9a8]">3D CADASTRE · PROTOTYPE</div>
          </div>
        </div>
        <span className="hidden border-l border-[#294047] pl-3 text-[9px] text-[#799796] xl:inline">Map-first spatial workspace</span>
        <form onSubmit={onSearch} className="ml-auto flex min-w-0 max-w-[330px] flex-1 items-center border border-[#35535a] bg-[#0b2028]">
          <Search size={13} className="ml-2 shrink-0 text-[#6c9091]" />
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search prototype ULPIN or record ID" className="h-7 min-w-0 flex-1 bg-transparent px-2 text-[9px] outline-none placeholder:text-[#637e80]" />
          <button type="submit" className="h-7 border-l border-[#35535a] px-2 text-[8px] text-[#9cbbba]" disabled={Boolean(busy)}>FIND</button>
        </form>
        <button type="button" onClick={() => void reload()} className="flex shrink-0 items-center gap-1.5 border border-[#35535a] px-2 py-1.5 text-[9px] text-[#9cbbba] hover:border-[#62cdbd]">
          <RefreshCw size={11} className={loading ? "animate-spin" : ""} /> Refresh
        </button>
        <span className={`hidden shrink-0 items-center gap-1.5 text-[8px] sm:flex ${failures.length ? "text-[#e8c887]" : "text-[#77d7bd]"}`}>
          <span className={`h-1.5 w-1.5 rounded-full ${loading ? "bg-[#e8c887]" : failures.length ? "bg-[#e8c887]" : "bg-[#52d39e]"}`} />
          {loading ? "SYNCING" : failures.length ? `${failures.length} API READ${failures.length === 1 ? "" : "S"} FAILED` : "API DATA READY"}
        </span>
      </header>

      <nav aria-label="Workspace sections" className="row-start-2 min-h-0 overflow-y-auto border-r border-[#28434b] bg-[#071820] py-2 thin-scroll">
        <div className="px-3 pb-2 text-[8px] font-semibold tracking-[.15em] text-[#638687]">WORKSPACE</div>
        {SECTION_ITEMS.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            type="button"
            onClick={() => setActive(id)}
            aria-current={active === id ? "page" : undefined}
            className={`flex w-full items-center gap-2 border-l-2 px-3 py-2 text-left text-[10px] transition ${
              active === id
                ? "border-[#62cdbd] bg-[#113038] font-semibold text-[#dffdf5]"
                : "border-transparent text-[#94afb1] hover:bg-[#0c222b]"
            }`}
          >
            <Icon size={13} className="shrink-0" />
            <span className="min-w-0 flex-1 truncate">{label}</span>
            {id === "validation" && issueCounts.critical > 0 ? <span className="rounded bg-[#713a31] px-1 text-[8px] text-[#ffb4a6]">{issueCounts.critical}</span> : null}
          </button>
        ))}
        <div className="mx-3 mt-3 border-t border-[#28434b] pt-3">
          <div className="mb-1.5 text-[8px] tracking-[.12em] text-[#638687]">DEMO DATA</div>
          <button type="button" onClick={() => void doAction("Load demo city", ENDPOINTS.demoLoad, "GET")} disabled={Boolean(busy)} className="w-full border border-[#3d626a] bg-[#0c252d] px-2 py-2 text-[9px] text-[#bcd6d3] hover:border-[#60c8b8] disabled:opacity-50">
            {busy === "Load demo city" ? "LOADING…" : "LOAD DEMO CITY"}
          </button>
        </div>
      </nav>

      <section aria-label="Cesium spatial workspace" className="relative row-start-2 min-h-0 min-w-0 overflow-hidden bg-[#07151d]">
        <CesiumMap
          parcels={workspace.parcels}
          buildings={mapBuildings}
          properties={mapProperties}
          infrastructure={workspace.infrastructure}
          selected={mapSelection}
          focus={focus}
          floor={floor}
          underground={underground}
          onPick={pickOnMap}
          overlay={overlays}
        />
        <div className="absolute left-3 top-3 flex items-center gap-2 border border-[#28434b] bg-[#07161eeF] px-2 py-1.5 shadow-lg">
          <span className="text-[8px] font-semibold tracking-[.12em] text-[#92b7b5]">VIEW</span>
          <select value={floor === "all" ? "all" : String(floor)} onChange={(event) => setFloor(event.target.value === "all" ? "all" : Number(event.target.value))} className="max-w-[125px] bg-transparent text-[9px] text-[#c2d9d5] outline-none">
            <option value="all">All levels</option>
            {floorOptions.map((level) => <option key={level} value={level}>{level < 0 ? `Basement ${Math.abs(level)}` : `Floor ${level}`}</option>)}
          </select>
          <label className="flex items-center gap-1 border-l border-[#29434a] pl-2 text-[8px] text-[#9bb5b4]">
            <input checked={underground} onChange={(event) => setUnderground(event.target.checked)} type="checkbox" className="accent-[#59c7b6]" />
            Below grade
          </label>
        </div>
        {shownSourceId ? (
          <div className="absolute right-3 top-3 flex items-center gap-2 border border-[#31525a] bg-[#07161eeF] px-2 py-1.5 text-[8px] text-[#9fc6c0]">
            {currentDerivedCount} derived source records shown
            <button type="button" onClick={() => setShownSourceId(null)} aria-label="Hide source-derived map records" className="text-[#719092] hover:text-white"><X size={11} /></button>
          </div>
        ) : null}
        {notice ? (
          <div role="status" className="absolute bottom-3 left-1/2 max-w-[min(88%,520px)] -translate-x-1/2 border border-[#496870] bg-[#0c2229f2] px-3 py-2 text-[9px] leading-4 text-[#d1e2e1] shadow-xl">
            {notice}
          </div>
        ) : null}
      </section>

      <aside aria-label={`${activeSection.label} contextual panel`} className="row-start-2 min-h-0 overflow-y-auto border-l border-[#28434b] bg-[#071820] p-3 thin-scroll">
        <div className="mb-3 border-b border-[#29434a] pb-2">
          <div className="flex items-center gap-2">
            <activeSection.icon size={13} className="text-[#69cbbd]" />
            <h1 className="min-w-0 flex-1 truncate text-[11px] font-bold tracking-[.12em] text-[#d7eae6]">{activeSection.label.toUpperCase()}</h1>
          </div>
          <div className="mt-1 pl-5 text-[8px] text-[#5f8082]">{activeSection.endpoint}</div>
        </div>
        {selected ? (
          <div className="mb-3">
            <Inspector item={selected} canFocus={Boolean(mapSelection && hasGeometry(mapSelection))} onFocus={startFocus} />
            {selectedSection === "sources" && selected.id ? (
              <div className="mt-1.5 flex items-center justify-between gap-2 border border-[#29434a] bg-[#08191f] px-2 py-2">
                <span className="text-[8px] text-[#718d8e]">Derived outputs are shown only when present.</span>
                <button
                  type="button"
                  onClick={() => showSourceOnMap(selected)}
                  disabled={![
                    ...workspace.extractedBuildings,
                    ...workspace.extractedFloors,
                    ...workspace.generatedVolumes,
                  ].some((record) => record.source_id === selected.id && hasGeometry(record))}
                  className="shrink-0 border border-[#3d626a] px-2 py-1 text-[8px] text-[#a8e4d7] disabled:cursor-not-allowed disabled:opacity-40"
                >
                  SHOW ON MAP
                </button>
              </div>
            ) : null}
          </div>
        ) : null}
        {renderSection()}
      </aside>

      <footer className="col-span-3 flex min-w-0 items-center gap-3 border-t border-[#28434b] bg-[#081820] px-3 text-[8px] text-[#91adac]">
        <span className="flex shrink-0 items-center gap-1.5 text-[#77d7bd]"><span className="h-1.5 w-1.5 rounded-full bg-[#52d39e]" /> {loading ? "SYNCING" : failures.length ? "PARTIAL DATA" : "LIVE API"}</span>
        <span className="hidden text-[#527176] sm:inline">|</span>
        <span className="hidden shrink-0 sm:inline">{workspace.parcels.length} parcels · {workspace.buildings.length} buildings · {workspace.properties.length} properties</span>
        <span className="ml-auto flex min-w-0 items-center justify-end gap-3 overflow-hidden">
          <span className="flex shrink-0 items-center gap-1"><i className="h-2 w-2 bg-[#456a57]" /> Parcel</span>
          <span className="flex shrink-0 items-center gap-1"><i className="h-2 w-2 bg-[#63808c]" /> Building</span>
          <span className="flex shrink-0 items-center gap-1"><i className="h-2 w-2 bg-[#3cb5a5]" /> Property</span>
          <span className="hidden shrink-0 items-center gap-1 md:flex"><i className="h-2 w-2 bg-[#d89936]" /> Infrastructure</span>
          <span className="hidden shrink-0 items-center gap-1 md:flex"><i className="h-2 w-2 bg-[#e0b25c]" /> Finding / change</span>
        </span>
      </footer>
    </main>
  );
}

function CollectionPanel({
  title,
  endpoint,
  items,
  section,
  selectedId,
  onSelect,
  error,
  emptyText,
}: {
  title: string;
  endpoint: string;
  items: RecordAny[];
  section: Section;
  selectedId: string;
  onSelect: (item: RecordAny, section: Section) => void;
  error?: string;
  emptyText: string;
}) {
  return (
    <div className="space-y-3">
      {error ? <ErrorNotice message={error} /> : null}
      <RecordList title={title} endpoint={endpoint} items={items} section={section} selectedId={selectedId} onSelect={(record) => onSelect(record, section)} emptyText={emptyText} />
    </div>
  );
}

function SummaryGroups({ summary }: { summary: RecordAny | null }) {
  if (!summary) return <p className="text-[9px] text-[#718d8e]">No validation summary was returned.</p>;
  const groups = [
    ["By severity", summary.by_severity],
    ["By category", summary.by_category],
  ] as const;
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between border border-[#253f45] bg-[#091c22] px-2 py-2">
        <span className="text-[9px] text-[#819f9e]">Total stored findings</span>
        <span className="text-[12px] font-semibold tabular-nums text-[#dff3ee]">{displayValue(summary.total)}</span>
      </div>
      {groups.map(([label, values]) => (
        <div key={label} className="border border-[#253f45] bg-[#091c22] p-2">
          <div className="mb-1 text-[8px] font-semibold tracking-[.1em] text-[#6f9192]">{label.toUpperCase()}</div>
          {values && typeof values === "object" && Object.keys(values).length ? (
            Object.entries(values).map(([key, value]) => (
              <div key={key} className="flex justify-between border-t border-[#172d32] py-1 text-[9px]">
                <span className="text-[#9bb8b5]">{key}</span><span className="tabular-nums text-[#d1e2e1]">{displayValue(value)}</span>
              </div>
            ))
          ) : <div className="text-[9px] text-[#718d8e]">No values returned.</div>}
        </div>
      ))}
    </div>
  );
}

function ErrorNotice({ message }: { message: string }) {
  return <p role="alert" className="border border-[#5c3a34] bg-[#1d1210] px-2 py-1.5 text-[9px] leading-4 text-[#f2a294]">{message}</p>;
}
