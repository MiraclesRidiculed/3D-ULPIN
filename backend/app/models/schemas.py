"""Pydantic schemas for the V-CAD API.

These define the externally observable request/response contract and must not
be changed without a deliberate API version bump.

Note: records held in the repository are plain ``dict`` objects, exactly as in
the original implementation. These schemas validate the API boundary only.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.models.enums import CHANGE_REQUIRES_VERIFICATION, ChangeStatus, Severity


class Geometry(BaseModel):
    # A GeoJSON Polygon footprint. Height is carried separately as z_min/z_max
    # on the owning record rather than embedded in the ring.
    # NOTE: deliberately no docstring here - Pydantic would copy it into the
    # OpenAPI schema as `description`, changing the published spec.
    type: Literal["Polygon"] = "Polygon"
    coordinates: list[list[list[float]]]


class Parcel(BaseModel):
    id: str
    parcel_id: str
    prototype_ulpin: str | None = None
    geometry: Geometry
    area: float
    land_use: str
    survey_reference: str
    created_at: str
    updated_at: str
    version: int = 1
    #: Revision of this parcel's geometry. Independent of ``version`` (the
    #: record row version) and of ``prototype_ulpin`` (stable identity).
    geometry_version: int = 1
    #: Digest of the current geometry, and the thing ``geometry_version`` counts.
    #: Published because the revision was already public without it: a version
    #: number that cannot be matched to the geometry it describes tells a caller
    #: nothing about whether a shape actually moved. Absent on a record that has
    #: never been versioned.
    geometry_hash: str | None = None


class Building(BaseModel):
    id: str
    building_id: str
    parcel_id: str
    footprint: Geometry
    height: float
    floor_count: int
    geometry_3d: dict[str, Any]
    created_at: str
    updated_at: str
    version: int = 1
    geometry_version: int = 1
    geometry_hash: str | None = None


class PropertyVolume(BaseModel):
    id: str
    prototype_ulpin: str | None = None
    parent_parcel_id: str
    building_id: str | None = None
    property_type: str
    floor_number: int | None = None
    unit_label: str | None = None
    z_min: float
    z_max: float
    geometry_3d: Geometry
    volume_m3: float
    area_m2: float
    geometry_hash: str
    status: str
    created_at: str
    updated_at: str
    version: int = 1
    geometry_version: int = 1


class Infrastructure(BaseModel):
    id: str
    type: str
    geometry_3d: Geometry
    z_min: float
    z_max: float
    owner: str
    reference: str
    created_at: str
    updated_at: str
    version: int = 1
    geometry_version: int = 1


class ValidationIssue(BaseModel):
    id: str
    object_a: str
    object_b: str | None = None
    issue_type: str
    severity: Severity
    overlap_volume: float = 0
    gap_m: float | None = None
    description: str
    geometry: Geometry | None = None
    status: str = "OPEN"
    location: str
    #: Which of the thirty rules filed this, its group, and the numbers that
    #: justified it. Nullable so rows written before the rule engine still
    #: validate; every finding produced today carries both.
    rule_id: str | None = None
    category: str | None = None
    evidence: dict[str, Any] | None = None


class GenerateRequest(BaseModel):
    parent_parcel_id: str | None = None


class SearchResult(BaseModel):
    kind: str
    record: dict[str, Any]
    related: dict[str, list[str]] = Field(default_factory=dict)


# --------------------------------------------------------------------------
# Geometry identity and versioning
# --------------------------------------------------------------------------


class GeometryVersion(BaseModel):
    """An immutable snapshot of one object's geometry at a point in time.

    Identity (``prototype_ulpin`` on the owning record) is deliberately absent:
    a geometry revision must never mint a new identifier. Only the geometry and
    its derived hash change between versions.
    """

    object_id: str
    object_type: str
    version: int
    geometry: Geometry
    z_min: float
    z_max: float
    geometry_hash: str
    source_id: str | None = None
    processing_job_id: str | None = None
    created_at: str
    created_by: str | None = None
    change_reason: str | None = None


# --------------------------------------------------------------------------
# Point clouds and processing jobs
# --------------------------------------------------------------------------


class PointCloudBounds(BaseModel):
    """Axis-aligned extent of a point cloud, in its own source CRS.

    ``None`` bounds mean the file declared none (common for PLY).
    """

    min_x: float | None = None
    min_y: float | None = None
    min_z: float | None = None
    max_x: float | None = None
    max_y: float | None = None
    max_z: float | None = None

    @property
    def is_defined(self) -> bool:
        return all(
            v is not None
            for v in (self.min_x, self.min_y, self.min_z, self.max_x, self.max_y, self.max_z)
        )


class PointCloudMetadata(BaseModel):
    """Everything learned from a point cloud without loading its points.

    Read from the file header only, so cost is independent of point count.
    """

    filename: str
    format: str
    file_size_bytes: int
    sha256: str | None = None
    compressed: bool = False
    point_count: int
    bounds: PointCloudBounds
    #: CRS declared by the file. ``UNKNOWN`` when the file states none — never
    #: guessed, because assuming one silently mis-places the data.
    crs: str
    crs_source: str | None = None
    #: Bounds reprojected to WGS84 for display.
    display_bounds: PointCloudBounds | None = None
    point_format: str | None = None
    file_version: str | None = None
    generating_software: str | None = None
    system_identifier: str | None = None
    creation_date: str | None = None
    gps_time_range: tuple[float, float] | None = None
    extra_dimensions: list[str] = []
    properties: list[str] = []
    comments: list[str] = []
    byte_order: str | None = None
    text_format: bool = False
    #: Points per square metre of plan extent. ``None`` when the plan area is
    #: zero (a vertical-only scan) or unknown.
    density_points_per_m2: float | None = None
    acquisition: dict[str, Any] = {}


class PointCloudValidation(BaseModel):
    """Outcome of inspecting a point cloud for structural problems."""

    is_valid: bool
    problems: list[str] = []
    warnings: list[str] = []


class ExtractedBuilding(BaseModel):
    """One building produced by point-cloud extraction.

    ``method`` is always present so a consumer can tell an algorithmic result
    from a learned one, and ``geometric_quality`` is a regularity score that must
    never be presented as an accuracy figure.
    """

    id: str
    source_id: str
    processing_job_id: str
    footprint: dict[str, Any]
    height_m: float
    crs: str
    method: str
    extractor: str
    extractor_version: str
    geometry_hash: str
    geometric_quality: float
    quality_metrics: dict[str, Any] = {}
    provenance: dict[str, Any] = {}
    created_at: str


class BuildingExtractionResult(BaseModel):
    """Outcome of running building extraction over one ingested point cloud."""

    source_id: str
    processing_job_id: str
    method: str
    method_description: str
    extractor: str
    extractor_version: str
    point_cloud: dict[str, Any]
    buildings_found: int
    buildings: list[ExtractedBuilding]
    quality_summary: dict[str, Any] = {}
    warnings: list[str] = []
    parameters: dict[str, Any] = {}
    note: str


class ExtractedFloorRecord(BaseModel):
    """One storey detected in a point cloud.

    ``z_min`` / ``z_max`` are absolute elevations in ``crs``, the point cloud's
    own projected CRS; the ``*_above_ground`` values are heights above the
    building's base. ``requires_human_review`` is a flag, not a score: the
    accompanying ``review_reasons`` say what a human needs to check.
    """

    id: str
    source_id: str
    processing_job_id: str
    building_id: str
    floor_number: int
    z_min: float
    z_max: float
    z_min_above_ground: float
    z_max_above_ground: float
    footprint: dict[str, Any]
    geometry_3d: dict[str, Any]
    area_m2: float
    volume_m3: float
    crs: str
    method: str
    geometry_hash: str
    geometric_quality: float
    requires_human_review: bool
    quality_metrics: dict[str, Any] = {}
    review_reasons: list[str] = []
    provenance: dict[str, Any] = {}
    created_at: str


class FloorSegmentationResult(BaseModel):
    """Outcome of segmenting storeys across a source's extracted buildings."""

    source_id: str
    processing_job_id: str
    method: str
    method_description: str
    point_cloud: dict[str, Any]
    buildings_segmented: int
    storeys_found: int
    storeys: list[ExtractedFloorRecord]
    buildings: list[dict[str, Any]] = []
    requires_human_review: bool = False
    review_reasons: list[str] = []
    warnings: list[str] = []
    note: str


class GeneratedPropertyVolumeRecord(BaseModel):
    """One generated volumetric property right.

    ``volume_scope`` is the field that carries the honesty: ``UNIT`` only where
    floor-plan data actually divided the storey, ``FLOOR`` otherwise.
    ``units_inferred`` is always false and is present so a consumer can verify
    that rather than assume it.
    """

    id: str
    prototype_ulpin: str
    parent_parcel_id: str
    building_id: str
    floor_number: int
    floor_label: str
    unit_label: str | None = None
    unit_label_source: str
    volume_scope: str
    property_type: str
    vertical_position: str
    z_min: float
    z_max: float
    geometry_3d: dict[str, Any]
    area_m2: float
    volume_m3: float
    geometry_hash: str
    geometry_version: int
    status: str
    units_inferred: bool
    requires_human_review: bool
    source_provenance: dict[str, Any] = {}
    processing_provenance: dict[str, Any] = {}
    review_reasons: list[str] = []
    notes: list[str] = []
    source_id: str | None = None
    generated_at: str


class VolumeGenerationResult(BaseModel):
    """Outcome of generating property volumes for a source's buildings."""

    source_id: str
    processing_job_id: str
    method: str
    method_description: str
    volumes_generated: int
    units_generated: int
    buildings: list[dict[str, Any]] = []
    volumes: list[GeneratedPropertyVolumeRecord]
    floors_without_unit_data: list[str] = []
    requires_human_review: bool = False
    review_reasons: list[str] = []
    warnings: list[str] = []
    label: str
    note: str


class ProcessingJob(BaseModel):
    """A unit of processing work recorded against an ingested source.

    Exists so ingestion is auditable and so the frontend can show status. Only
    ``METADATA_EXTRACTION`` runs today; the extraction stages are recorded as
    ``NOT_IMPLEMENTED`` rather than silently skipped.
    """

    id: str
    source_id: str
    job_type: str
    status: str
    point_count: int | None = None
    crs: str | None = None
    bounds: dict[str, Any] | None = None
    detail: str | None = None
    error: str | None = None
    started_at: str
    completed_at: str | None = None
    created_by: str | None = None
    metadata: dict[str, Any] = {}


class GeometryVersionComparison(BaseModel):
    """Difference between two geometry versions of the same object."""

    object_id: str
    from_version: int
    to_version: int
    #: Always False in a correct system: identity survives geometry edits.
    ulpin_changed: bool
    geometry_changed: bool
    geometry_hash_changed: bool
    z_range_changed: bool
    from_geometry_hash: str
    to_geometry_hash: str
    delta_z_min: float
    delta_z_max: float
    from_area_m2: float
    to_area_m2: float
    delta_area_m2: float
    from_volume_m3: float
    to_volume_m3: float
    delta_volume_m3: float

# --------------------------------------------------------------------------
# Human verification and audit history
# --------------------------------------------------------------------------


class ReviewCase(BaseModel):
    """Current state of a human review of one validation finding.

    Mutable, unlike :class:`ReviewDecision`: this is the queue a reviewer works
    from. ``issue_id`` refers to a finding that a validation re-run may replace,
    so it is not a foreign key.
    """

    id: str
    issue_id: str
    issue_type: str | None = None
    severity: str | None = None
    object_a: str | None = None
    object_b: str | None = None
    object_type: str | None = None
    state: str
    assigned_to: str | None = None
    priority: str | None = None
    reason: str | None = None
    created_by: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    decided_at: str | None = None
    decided_by: str | None = None
    decision: str | None = None
    decision_reason: str | None = None
    review_count: int = 0


class ReviewDecision(BaseModel):
    """One immutable human decision, with the context needed to audit it.

    ``reviewer`` is **self-asserted**: authentication is not implemented, so this
    is a name the caller supplied, not a verified identity.
    """

    id: str
    review_case_id: str
    issue_id: str | None = None
    decision: str
    action: str
    reviewer: str | None = None
    reason: str
    object_id: str | None = None
    related_object_id: str | None = None
    object_type: str | None = None
    previous_state: str | None = None
    new_state: str
    decided_at: str | None = None


class AuditEvent(BaseModel):
    """One entry in the append-only audit log.

    ``actor`` is self-asserted and unverified; see :class:`ReviewDecision`.
    """

    id: str
    action: str
    object_id: str | None = None
    object_type: str | None = None
    actor: str
    detail: str | None = None
    issue_id: str | None = None
    review_case_id: str | None = None
    previous_state: str | None = None
    new_state: str | None = None
    occurred_at: str | None = None
    context: dict[str, Any] | None = None


class ReviewRequest(BaseModel):
    """Open a review case for a finding."""

    reason: str | None = None
    reviewer: str | None = None
    priority: str | None = None


class AssignRequest(BaseModel):
    """Assign a case to a reviewer."""

    reviewer: str
    reason: str | None = None


class DecisionRequest(BaseModel):
    """Record a review decision.

    ``reason`` is required. A decision with no stated justification is the most
    common way an audit trail becomes worthless, so the service refuses to record
    one.
    """

    reason: str
    reviewer: str | None = None


class CloseRequest(BaseModel):
    reason: str
    reviewer: str | None = None

# --------------------------------------------------------------------------
# Cadastral change detection
# --------------------------------------------------------------------------


class ChangeRecord(BaseModel):
    """One difference between the approved cadastre and a new survey.

    ``geometric_quality`` is an **agreement score** in [0, 1] derived from the
    geometry alone -- not a confidence in the survey and not a statement about
    who produced it. ``status`` never carries an "illegal" or "unauthorised"
    value; every record starts at ``REQUIRES_VERIFICATION``.
    """

    id: str | None = None
    change_type: str
    object_id: str
    object_type: str = "PROPERTY_VOLUME"
    previous_geometry: dict[str, Any] | None = None
    new_geometry: dict[str, Any] | None = None
    #: The region belonging to exactly one of the two outlines, so a change can
    #: be highlighted rather than only described. ``None`` when a side has no
    #: footprint.
    difference_geometry: dict[str, Any] | None = None
    previous_z_range: tuple[float, float] | None = None
    new_z_range: tuple[float, float] | None = None
    area_delta: float = 0.0
    height_delta: float = 0.0
    volume_delta: float = 0.0
    previous_area_m2: float | None = None
    new_area_m2: float | None = None
    previous_height_m: float | None = None
    new_height_m: float | None = None
    previous_volume_m3: float | None = None
    new_volume_m3: float | None = None
    geometric_quality: float = 0.0
    source_id: str
    detected_at: str
    status: str
    floor_number: int | None = None
    building_id: str | None = None
    description: str = CHANGE_REQUIRES_VERIFICATION
    tolerance: float = 0.01
    finding_id: str | None = None

    @classmethod
    def from_record(cls, change) -> "ChangeRecord":
        """Build from a stored row (a dict) or an in-memory change dataclass.

        The two shapes differ in one place: a stored row keeps the Z range in
        four scalar columns, while the dataclass holds it as a pair. Accepting
        both here means a caller can pass the result of a comparison straight
        into this schema, and the API never has to flatten a record just to
        serialise it.
        """
        return cls(
            id=_get(change, "id"),
            change_type=_get(change, "change_type"),
            object_id=_get(change, "object_id"),
            object_type=_get(change, "object_type") or "PROPERTY_VOLUME",
            previous_geometry=_get(change, "previous_geometry"),
            new_geometry=_get(change, "new_geometry"),
            difference_geometry=_get(change, "difference_geometry"),
            previous_z_range=_z_range_of(change, "previous"),
            new_z_range=_z_range_of(change, "new"),
            area_delta=_get(change, "area_delta") or 0.0,
            height_delta=_get(change, "height_delta") or 0.0,
            volume_delta=_get(change, "volume_delta") or 0.0,
            previous_area_m2=_get(change, "previous_area_m2"),
            new_area_m2=_get(change, "new_area_m2"),
            previous_height_m=_get(change, "previous_height_m"),
            new_height_m=_get(change, "new_height_m"),
            previous_volume_m3=_get(change, "previous_volume_m3"),
            new_volume_m3=_get(change, "new_volume_m3"),
            geometric_quality=_get(change, "geometric_quality") or 0.0,
            source_id=_get(change, "source_id") or "",
            detected_at=_get(change, "detected_at") or "",
            status=_get(change, "status")
            or ChangeStatus.REQUIRES_VERIFICATION.value,
            floor_number=_get(change, "floor_number"),
            building_id=_get(change, "building_id"),
            description=_get(change, "description") or CHANGE_REQUIRES_VERIFICATION,
            tolerance=_get(change, "tolerance") or 0.01,
            finding_id=_get(change, "finding_id"),
        )


def _z_range(z_min: float | None, z_max: float | None) -> tuple[float, float] | None:
    if z_min is None or z_max is None:
        return None
    return (float(z_min), float(z_max))


def _z_range_of(change, prefix: str) -> tuple[float, float] | None:
    """Read a Z range off either shape.

    A stored row exposes ``<prefix>_z_min``/``<prefix>_z_max``; the dataclass
    exposes ``<prefix>_z_range`` as a pair. Prefers the pair, since that is the
    richer form, and falls back to the scalars.
    """
    pair = _get(change, f"{prefix}_z_range")
    if pair is not None:
        return (float(pair[0]), float(pair[1]))
    return _z_range(
        _get(change, f"{prefix}_z_min"), _get(change, f"{prefix}_z_max")
    )


def _get(record, name: str, default=None):
    """Read a field off a stored row or a dataclass, whichever was handed in."""
    if isinstance(record, Mapping):
        return record.get(name, default)
    return getattr(record, name, default)


class ChangeSummary(BaseModel):
    """Aggregate outcome of a comparison, or of everything on record."""

    source_id: str | None = None
    compared_objects: int = 0
    unchanged_objects: int = 0
    total_changes: int = 0
    by_type: dict[str, int] = Field(default_factory=dict)
    objects_affected: int | None = None
    status: str
    message: str = CHANGE_REQUIRES_VERIFICATION
    tolerance: float = 0.01
    findings_created: int = 0
    review_cases_created: int = 0
    all_require_verification: bool | None = None
    changes: list[ChangeRecord] | None = None
    generated_at: str


class CompareRequest(BaseModel):
    """Compare the current cadastre (or a supplied baseline) with a survey.

    ``approved`` is optional: omitted, the repository's own records are the
    approved side, which is the common case of re-surveying a known parcel.
    """

    survey: list[dict[str, Any]]
    source_id: str
    approved: list[dict[str, Any]] | None = None
    tolerance: float = 0.01
    actor: str | None = None

# --------------------------------------------------------------------------
# Provenance
# --------------------------------------------------------------------------


class ProvenanceRecord(BaseModel):
    """One step of the chain from a data source to a reviewed finding.

    ``model_name``/``model_version`` are empty for everything this system
    currently runs: every stage is algorithmic or geometric and there is no
    trained model in the pipeline. A placeholder would read later as "a model
    produced this", which is false.
    """

    id: str
    stage: str
    object_id: str
    source_id: str | None = None
    processing_job_id: str | None = None
    parent_id: str | None = None
    parent_stage: str | None = None
    algorithm: str
    method_description: str | None = None
    model_name: str | None = None
    model_version: str | None = None
    parameters: dict[str, Any] | None = None
    created_by: str | None = None
    created_at: str | None = None


class LineageNode(BaseModel):
    """One object reached at one stage of a lineage."""

    stage: str | None = None
    object_id: str
    link_id: str | None = None
    algorithm: str | None = None
    model_name: str | None = None
    model_version: str | None = None
    source_id: str | None = None
    processing_job_id: str | None = None
    created_at: str | None = None


class ObjectLineage(BaseModel):
    """The chain from a data source to an object, plus its descendants.

    ``complete`` is ``False`` when the chain does not begin at a recorded data
    source. Gaps are reported, never filled in with a guessed parent.
    """

    object_id: str
    lineage: list[LineageNode] = Field(default_factory=list)
    ancestors: list[str] = Field(default_factory=list)
    descendants: list[str] = Field(default_factory=list)
    source_id: str | None = None
    processing_job_ids: list[str] = Field(default_factory=list)
    complete: bool = False
    note: str | None = None


#: Read-only alias kept for callers that import the lineage shape by its chain name.
Lineage = ObjectLineage


class DerivedStage(BaseModel):
    stage: str
    object_ids: list[str] = Field(default_factory=list)
    count: int = 0


class DerivedObjects(BaseModel):
    """Everything one data source produced, grouped by stage."""

    source_id: str
    stages: list[DerivedStage] = Field(default_factory=list)
    total_objects: int = 0


class ProcessingHistoryEntry(BaseModel):
    """One processing job, as recorded on the job row itself."""

    processing_job_id: str
    job_type: str | None = None
    status: str | None = None
    detail: str | None = None
    source_id: str | None = None
    started_at: str | None = None
    completed_at: str | None = None

