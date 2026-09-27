"""SQLAlchemy models for the PostGIS schema.

Reconciliation with ``database/init.sql``
-----------------------------------------
The original file was inspected before these models were written. It is
honoured where it was right and deliberately diverges where it conflicted with
the running application:

===================================  ==========================================
``init.sql``                          Decision
===================================  ==========================================
``UUID`` primary keys + TEXT business  **TEXT primary keys** holding the
keys                                    application's own id (``"PV-201"``).
                                       Preserves the API contract exactly, and a
                                       survey reference is a better cadastral
                                       key than a random UUID.
singular table names                   **plural**, matching the repository's
                                       collection vocabulary.
``geometry(POLYGON,4326)``             kept â€” authoritative storage is WGS84.
``POLYGONZ`` / ``POLYHEDRALSURFACEZ``  **2D ``POLYGON`` + ``z_min``/``z_max``
                                       columns. The application models a
                                       footprint plus scalar elevations and
                                       never builds a 3D geometry.
no ``unit_label`` on property_volume   **added** â€” the API exposes it and the
                                       UI displays it.
``infrastructure.owner_metadata``      **``owner`` + ``reference``** columns.
                                       The API model is the contract.
no ``gap_m`` / ``location`` on issues  **added** â€” both are in the API model.
no ``geometry_version`` anywhere       **added** to every geometry-bearing
                                       table, plus a new ``geometry_versions``
                                       history table.
no metric columns                      **added** as generated, GIST-indexed
                                       location-aware metric columns.
===================================  ==========================================
"""
from __future__ import annotations

import datetime as dt

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import geographic_geometry, metric_geometry

def created_at() -> Mapped[dt.datetime]:
    """Creation timestamp, defaulted by the database."""
    return mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )


def updated_at() -> Mapped[dt.datetime]:
    """Modification timestamp, maintained by the database."""
    return mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
    )


class Parcel(Base):
    """A land parcel: a 2D footprint with a stable cadastral identity."""

    __tablename__ = "parcels"

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    parcel_id: Mapped[str] = mapped_column(sa.Text, unique=True, nullable=False)
    prototype_ulpin: Mapped[str | None] = mapped_column(sa.Text, unique=True)
    geometry = mapped_column(*geographic_geometry(nullable=False))
    geometry_metric = mapped_column(*metric_geometry("geometry"))
    area: Mapped[float | None] = mapped_column(sa.Float)
    land_use: Mapped[str | None] = mapped_column(sa.Text)
    survey_reference: Mapped[str | None] = mapped_column(sa.Text)
    geometry_hash: Mapped[str | None] = mapped_column(sa.Text)
    geometry_version: Mapped[int] = mapped_column(sa.Integer, default=1, server_default="1")
    created_at = created_at()
    updated_at = updated_at()
    version: Mapped[int] = mapped_column(sa.Integer, default=1, server_default="1")


class Building(Base):
    """A building footprint on a parcel. Vertical extent is a storey count."""

    __tablename__ = "buildings"

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    building_id: Mapped[str] = mapped_column(sa.Text, unique=True, nullable=False)
    parcel_id: Mapped[str | None] = mapped_column(
        sa.Text, sa.ForeignKey("parcels.parcel_id")
    )
    footprint = mapped_column(*geographic_geometry())
    footprint_metric = mapped_column(*metric_geometry("footprint"))
    #: The API exposes a ``geometry_3d`` wrapper (``z_min``/``z_max``/``footprint``)
    #: on buildings, so it is persisted as JSONB to keep the response contract
    #: identical on both backends. The spatial truth stays in ``footprint``.
    geometry_3d: Mapped[dict | None] = mapped_column(JSONB)
    height: Mapped[float | None] = mapped_column(sa.Float)
    floor_count: Mapped[int | None] = mapped_column(sa.Integer)
    confidence: Mapped[float | None] = mapped_column(sa.Float)
    geometry_hash: Mapped[str | None] = mapped_column(sa.Text)
    geometry_version: Mapped[int] = mapped_column(sa.Integer, default=1, server_default="1")
    created_at = created_at()
    updated_at = updated_at()
    version: Mapped[int] = mapped_column(sa.Integer, default=1, server_default="1")


class Floor(Base):
    """One storey of a building, with its vertical band."""

    __tablename__ = "floors"
    __table_args__ = (
        sa.UniqueConstraint("building_id", "floor_number", name="uq_floors_building_level"),
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    building_id: Mapped[str] = mapped_column(
        sa.Text, sa.ForeignKey("buildings.building_id"), nullable=False
    )
    floor_number: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    z_min: Mapped[float] = mapped_column(sa.Float, nullable=False)
    z_max: Mapped[float] = mapped_column(sa.Float, nullable=False)
    geometry_3d = mapped_column(*geographic_geometry())
    geometry_3d_metric = mapped_column(*metric_geometry("geometry_3d"))
    confidence: Mapped[float | None] = mapped_column(sa.Float)
    created_at = created_at()
    updated_at = updated_at()
    version: Mapped[int] = mapped_column(sa.Integer, default=1, server_default="1")


class PropertyVolume(Base):
    """A volumetric property right: footprint plus ``z_min``/``z_max``."""

    __tablename__ = "property_volumes"

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    prototype_ulpin: Mapped[str | None] = mapped_column(sa.Text, unique=True)
    parent_parcel_id: Mapped[str | None] = mapped_column(
        sa.Text, sa.ForeignKey("parcels.parcel_id")
    )
    building_id: Mapped[str | None] = mapped_column(sa.Text)
    property_type: Mapped[str | None] = mapped_column(sa.Text)
    floor_number: Mapped[int | None] = mapped_column(sa.Integer)
    unit_label: Mapped[str | None] = mapped_column(sa.Text)
    z_min: Mapped[float | None] = mapped_column(sa.Float)
    z_max: Mapped[float | None] = mapped_column(sa.Float)
    geometry_3d = mapped_column(*geographic_geometry())
    geometry_3d_metric = mapped_column(*metric_geometry("geometry_3d"))
    volume_m3: Mapped[float | None] = mapped_column(sa.Float)
    area_m2: Mapped[float | None] = mapped_column(sa.Float)
    geometry_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[str | None] = mapped_column(sa.Text)
    confidence: Mapped[float | None] = mapped_column(sa.Float)
    geometry_version: Mapped[int] = mapped_column(sa.Integer, default=1, server_default="1")
    created_at = created_at()
    updated_at = updated_at()
    version: Mapped[int] = mapped_column(sa.Integer, default=1, server_default="1")


class Infrastructure(Base):
    """An underground or elevated asset, e.g. a utility corridor."""

    __tablename__ = "infrastructure"

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    type: Mapped[str | None] = mapped_column(sa.Text)
    geometry_3d = mapped_column(*geographic_geometry())
    geometry_3d_metric = mapped_column(*metric_geometry("geometry_3d"))
    z_min: Mapped[float | None] = mapped_column(sa.Float)
    z_max: Mapped[float | None] = mapped_column(sa.Float)
    owner: Mapped[str | None] = mapped_column(sa.Text)
    reference: Mapped[str | None] = mapped_column(sa.Text)
    geometry_hash: Mapped[str | None] = mapped_column(sa.Text)
    geometry_version: Mapped[int] = mapped_column(sa.Integer, default=1, server_default="1")
    created_at = created_at()
    updated_at = updated_at()
    version: Mapped[int] = mapped_column(sa.Integer, default=1, server_default="1")


class ValidationIssue(Base):
    """A persisted validation finding.

    ``rule_id``, ``category`` and ``evidence`` were added with the rule-based
    engine. ``evidence`` holds the numbers that produced the finding, so a
    reviewer can see *why* something was flagged rather than being handed a
    severity alone. The original eight fields are unchanged and remain the
    stored-record contract.
    """

    __tablename__ = "validation_issues"
    __table_args__ = (
        # Findings are triaged per rule or per category far more often than
        # scanned in full, so both are indexed.
        sa.Index("ix_validation_issues_rule_id", "rule_id"),
        sa.Index("ix_validation_issues_category", "category"),
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    object_a: Mapped[str] = mapped_column(sa.Text, nullable=False)
    object_b: Mapped[str | None] = mapped_column(sa.Text)
    issue_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    severity: Mapped[str] = mapped_column(sa.Text, nullable=False)
    overlap_volume: Mapped[float] = mapped_column(sa.Float, default=0, server_default="0")
    gap_m: Mapped[float | None] = mapped_column(sa.Float)
    description: Mapped[str] = mapped_column(sa.Text)
    geometry = mapped_column(*geographic_geometry())
    geometry_metric = mapped_column(*metric_geometry("geometry"))
    status: Mapped[str] = mapped_column(sa.Text, default="OPEN", server_default="OPEN")
    location: Mapped[str] = mapped_column(sa.Text)
    #: Which rule produced this finding, and the evidence behind it.
    rule_id: Mapped[str | None] = mapped_column(sa.Text)
    category: Mapped[str | None] = mapped_column(sa.Text)
    evidence: Mapped[dict | None] = mapped_column(JSONB)
    created_at = created_at()

class DataSource(Base):
    """Provenance for an ingested dataset."""

    __tablename__ = "data_sources"

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    source_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    filename: Mapped[str] = mapped_column(sa.Text, nullable=False)
    crs: Mapped[str | None] = mapped_column(sa.Text)
    acquisition_date: Mapped[dt.date | None] = mapped_column(sa.Date)
    #: Named ``source_metadata`` because ``metadata`` is reserved by SQLAlchemy.
    source_metadata: Mapped[dict | None] = mapped_column("metadata", JSONB)
    created_at = created_at()


class ExtractedBuilding(Base):
    """A building footprint extracted from point-cloud data.

    Separate from :class:`Building` on purpose. A cadastral building is a legal
    object tied to a parcel; this is a *measurement* derived from a scan, with no
    legal standing and no parcel. Conflating them would let an inferred footprint
    masquerade as surveyed record.

    ``confidence`` is deliberately **not** a column. The extractor is geometric,
    so its honest score is ``geometric_quality`` -- a regularity measure, not a
    probability -- and giving it a ``confidence`` field would invite exactly the
    misreading the extractor refuses to make.
    """

    __tablename__ = "extracted_buildings"
    __table_args__ = (
        sa.Index("ix_extracted_buildings_source", "source_id"),
        sa.Index("ix_extracted_buildings_job", "processing_job_id"),
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    source_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    processing_job_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    #: Footprint in the point cloud's own projected CRS, reprojected to WGS84 for
    #: storage, matching every other geometry column in the schema.
    footprint = mapped_column(*geographic_geometry())
    footprint_metric = mapped_column(*metric_geometry("footprint"))
    height_m: Mapped[float] = mapped_column(sa.Float, nullable=False)
    #: The CRS the scan's coordinates were actually in, preserved because a
    #: footprint's coordinates are meaningless without it.
    crs: Mapped[str] = mapped_column(sa.Text, nullable=False)
    method: Mapped[str] = mapped_column(sa.Text, nullable=False)
    extractor: Mapped[str] = mapped_column(sa.Text, nullable=False)
    extractor_version: Mapped[str] = mapped_column(sa.Text, nullable=False)
    geometry_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    geometric_quality: Mapped[float] = mapped_column(sa.Float, nullable=False)
    #: Quality components and full provenance as JSON.
    extraction: Mapped[dict | None] = mapped_column(JSONB)
    created_at = created_at()


class ExtractedFloor(Base):
    """A storey detected in a point cloud.

    Separate from the cadastral :class:`Floor`, for the same reason
    :class:`ExtractedBuilding` is separate from :class:`Building`: a storey
    inferred from a scan is a geometric observation, not a surveyed storey of a
    legal building. The cadastral table is keyed to ``building_id`` and feeds
    property volumes; this one is keyed to the job that produced it.

    ``z_min`` / ``z_max`` are absolute elevations in the **point cloud's own
    projected CRS**, which is recorded in ``crs``; the heights above the
    building's base are kept alongside because they are the engineering numbers.
    A footprint's coordinates are meaningless without the CRS, so it is stored.

    No ``confidence`` column, deliberately: the extractor is geometric, so its
    defensible score is ``geometric_quality``. Uncertainty is carried as explicit
    review flags in ``segmentation`` rather than as a number that invites
    over-trust.
    """

    __tablename__ = "extracted_floors"
    __table_args__ = (
        sa.UniqueConstraint(
            "processing_job_id",
            "building_id",
            "floor_number",
            name="uq_extracted_floors_job_building_level",
        ),
        sa.Index("ix_extracted_floors_source", "source_id"),
        sa.Index("ix_extracted_floors_building", "building_id"),
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    source_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    processing_job_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    #: The extracted building this storey belongs to.
    building_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    floor_number: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    z_min: Mapped[float] = mapped_column(sa.Float, nullable=False)
    z_max: Mapped[float] = mapped_column(sa.Float, nullable=False)
    z_min_above_ground: Mapped[float] = mapped_column(sa.Float, nullable=False)
    z_max_above_ground: Mapped[float] = mapped_column(sa.Float, nullable=False)
    #: Plan geometry of the storey, WGS84 as everywhere else in this schema.
    footprint = mapped_column(*geographic_geometry())
    footprint_metric = mapped_column(*metric_geometry("footprint"))
    geometry_3d = mapped_column(JSONB)
    area_m2: Mapped[float] = mapped_column(sa.Float, nullable=False)
    volume_m3: Mapped[float] = mapped_column(sa.Float, nullable=False)
    crs: Mapped[str] = mapped_column(sa.Text, nullable=False)
    method: Mapped[str] = mapped_column(sa.Text, nullable=False)
    geometry_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    geometric_quality: Mapped[float] = mapped_column(sa.Float, nullable=False)
    requires_human_review: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, server_default=sa.false()
    )
    #: Quality components, review flags and full provenance as JSON.
    segmentation: Mapped[dict | None] = mapped_column(JSONB)
    created_at = created_at()


class GeneratedPropertyVolume(Base):
    """A property volume generated from a parcel, a building and its storeys.

    Deliberately separate from the cadastral :class:`PropertyVolume`. This row is
    a *derived* record assembled from a point cloud plus a parent parcel; that
    table holds surveyed rights. Mixing them would let a generated volume be read
    as a cadastral one, and would disturb the demo scene's 17 deliberate
    properties.

    The honest-scope fields are the important part:

    * ``volume_scope`` is ``UNIT`` only when floor-plan unit data actually
      divided the storey, and ``FLOOR`` otherwise. It is never ``UNIT`` by
      default.
    * ``units_inferred`` is a column that is always false. Apartment boundaries
      are never invented; the flag exists so that a consumer can *check* that
      rather than take it on trust.

    No ``confidence`` column: these are derived geometric records, so
    ``geometry_quality`` and the explicit review reasons are the honest signals.
    """

    __tablename__ = "generated_property_volumes"
    __table_args__ = (
        sa.Index("ix_generated_volumes_source", "source_id"),
        sa.Index("ix_generated_volumes_building", "building_id"),
        sa.Index("ix_generated_volumes_parcel", "parent_parcel_id"),
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    #: The point cloud this descends from, when there was one.
    source_id: Mapped[str | None] = mapped_column(sa.Text)
    #: Stable prototype identifier. Unique, so two volumes can never share one.
    prototype_ulpin: Mapped[str] = mapped_column(sa.Text, unique=True, nullable=False)
    #: The parent parcel. May be ``UNASSIGNED`` rather than null, so the
    #: identifier stays stable even when no parcel matched.
    parent_parcel_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    building_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    floor_number: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    floor_label: Mapped[str] = mapped_column(sa.Text, nullable=False)
    unit_label: Mapped[str | None] = mapped_column(sa.Text)
    unit_label_source: Mapped[str] = mapped_column(sa.Text, nullable=False)
    volume_scope: Mapped[str] = mapped_column(sa.Text, nullable=False)
    property_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    vertical_position: Mapped[str] = mapped_column(sa.Text, nullable=False)
    z_min: Mapped[float] = mapped_column(sa.Float, nullable=False)
    z_max: Mapped[float] = mapped_column(sa.Float, nullable=False)
    geometry_3d = mapped_column(*geographic_geometry())
    geometry_3d_metric = mapped_column(*metric_geometry("geometry_3d"))
    area_m2: Mapped[float] = mapped_column(sa.Float, nullable=False)
    volume_m3: Mapped[float] = mapped_column(sa.Float, nullable=False)
    geometry_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    geometry_version: Mapped[int] = mapped_column(
        sa.Integer, default=1, server_default="1", nullable=False
    )
    status: Mapped[str] = mapped_column(sa.Text, nullable=False)
    units_inferred: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, server_default=sa.false()
    )
    requires_human_review: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, server_default=sa.false()
    )
    #: Which observation this came from, and how it was made.
    source_provenance: Mapped[dict | None] = mapped_column(JSONB)
    processing_provenance: Mapped[dict | None] = mapped_column(JSONB)
    #: Actionable problems, and factual notes about the output.
    review_reasons: Mapped[list | None] = mapped_column(JSONB)
    notes: Mapped[list | None] = mapped_column(JSONB)
    generated_at = created_at()


class ProcessingJob(Base):
    """A unit of processing work recorded against an ingested source.

    Makes ingestion auditable and lets the UI show status. Only
    ``METADATA_EXTRACTION`` runs today; the extraction stages are recorded as
    ``NOT_IMPLEMENTED`` rather than silently omitted.
    """

    __tablename__ = "processing_jobs"
    __table_args__ = (
        sa.Index("ix_processing_jobs_source", "source_id"),
        sa.Index("ix_processing_jobs_type_status", "job_type", "status"),
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    source_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    job_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[str] = mapped_column(sa.Text, nullable=False)
    point_count: Mapped[int | None] = mapped_column(sa.BigInteger)
    crs: Mapped[str | None] = mapped_column(sa.Text)
    bounds: Mapped[dict | None] = mapped_column(JSONB)
    detail: Mapped[str | None] = mapped_column(sa.Text)
    error: Mapped[str | None] = mapped_column(sa.Text)
    started_at: Mapped[str] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    completed_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_by: Mapped[str | None] = mapped_column(sa.Text)
    job_metadata: Mapped[dict | None] = mapped_column("metadata", JSONB)


class GeometryVersion(Base):
    """Immutable history row: one geometry revision of one object.

    Identity is deliberately absent. A geometry revision must never mint a new
    identifier, so there is nowhere here to store one.
    """

    __tablename__ = "geometry_versions"
    __table_args__ = (
        sa.UniqueConstraint("object_id", "version", name="uq_geometry_versions_object"),
        sa.Index("ix_geometry_versions_object", "object_id"),
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    object_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    object_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    geometry = mapped_column(*geographic_geometry(nullable=False))
    geometry_metric = mapped_column(*metric_geometry("geometry"))
    z_min: Mapped[float] = mapped_column(sa.Float, nullable=False)
    z_max: Mapped[float] = mapped_column(sa.Float, nullable=False)
    geometry_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    source_id: Mapped[str | None] = mapped_column(sa.Text)
    processing_job_id: Mapped[str | None] = mapped_column(sa.Text)
    created_at = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    created_by: Mapped[str | None] = mapped_column(sa.Text)
    change_reason: Mapped[str | None] = mapped_column(sa.Text)


class CadastralChange(Base):
    """One difference found between the approved cadastre and a new survey.

    Both geometries are retained on the row, not just the deltas. A reviewer
    months later needs to see the two outlines that were compared; keeping only
    the arithmetic would make the record impossible to check.

    ``previous_geometry``/``new_geometry`` are JSONB rather than geometry columns
    deliberately: they are *evidence of a comparison*, not a queryable location.
    Nothing spatially queries a change -- a change is looked up by the object it
    concerns, and by the survey that found it.

    ``status`` carries no "illegal" or "unauthorised" value. A geometric
    difference is a fact about two measurements; who was entitled to make it is
    a question for a human, and every row starts at
    ``REQUIRES_VERIFICATION``.
    """

    __tablename__ = "cadastral_changes"
    __table_args__ = (
        sa.Index("ix_cadastral_changes_object", "object_id"),
        sa.Index("ix_cadastral_changes_source", "source_id"),
        sa.Index("ix_cadastral_changes_type", "change_type"),
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    change_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    object_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    object_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    previous_geometry: Mapped[dict | None] = mapped_column(JSONB)
    new_geometry: Mapped[dict | None] = mapped_column(JSONB)
    #: The symmetric difference -- the plan area belonging to exactly one of the
    #: two outlines. Stored so a change can be *drawn* as well as described;
    #: recomputing it per read would show a different answer than the one
    #: recorded. ``None`` when a side has no footprint.
    difference_geometry: Mapped[dict | None] = mapped_column(JSONB)
    previous_z_min: Mapped[float | None] = mapped_column(sa.Float)
    previous_z_max: Mapped[float | None] = mapped_column(sa.Float)
    new_z_min: Mapped[float | None] = mapped_column(sa.Float)
    new_z_max: Mapped[float | None] = mapped_column(sa.Float)
    area_delta: Mapped[float] = mapped_column(
        sa.Float, nullable=False, default=0.0, server_default="0"
    )
    height_delta: Mapped[float] = mapped_column(
        sa.Float, nullable=False, default=0.0, server_default="0"
    )
    volume_delta: Mapped[float] = mapped_column(
        sa.Float, nullable=False, default=0.0, server_default="0"
    )
    previous_area_m2: Mapped[float | None] = mapped_column(sa.Float)
    new_area_m2: Mapped[float | None] = mapped_column(sa.Float)
    previous_height_m: Mapped[float | None] = mapped_column(sa.Float)
    new_height_m: Mapped[float | None] = mapped_column(sa.Float)
    previous_volume_m3: Mapped[float | None] = mapped_column(sa.Float)
    new_volume_m3: Mapped[float | None] = mapped_column(sa.Float)
    #: Agreement in [0, 1] derived from geometry alone -- NOT a confidence in
    #: the survey. See ``services/change_detection.py``.
    geometric_quality: Mapped[float] = mapped_column(
        sa.Float, nullable=False, default=0.0, server_default="0"
    )
    source_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    detected_at = created_at()
    status: Mapped[str] = mapped_column(sa.Text, nullable=False)
    floor_number: Mapped[int | None] = mapped_column(sa.Integer)
    building_id: Mapped[str | None] = mapped_column(sa.Text)
    description: Mapped[str] = mapped_column(sa.Text, nullable=False)
    tolerance: Mapped[float] = mapped_column(
        sa.Float, nullable=False, default=0.01, server_default="0.01"
    )
    #: The ``UNREGISTERED_FLOOR`` finding, where this change produced one.
    finding_id: Mapped[str | None] = mapped_column(sa.Text)


class ProvenanceLink(Base):
    """One step of the chain from a data source to a reviewed finding.

    Stored as a graph of parent-to-child links rather than columns on each object,
    for two reasons: a generated volume has one source but several things it also
    contributes to (a finding about it, a review of that finding), which a single
    ``source_id`` column cannot express; and the later stages are events rather
    than geometry, with no row to hang a foreign key off.

    ``model_name``/``model_version`` are empty for every link this system writes.
    Every stage is algorithmic or geometric -- a grid ground filter, DBSCAN, a
    concavity hull, an elevation histogram, a planar subdivision -- and there is no
    trained model anywhere in the pipeline. The columns exist so the day one is
    used, the information lands somewhere queryable instead of being lost; they
    are never populated with a placeholder, and
    ``services/provenance.create_provenance_record`` *rejects* a model name
    attached to a known algorithmic method.
    """

    __tablename__ = "provenance_links"
    __table_args__ = (
        sa.Index("ix_provenance_links_object", "object_id"),
        sa.Index("ix_provenance_links_parent", "parent_id"),
        sa.Index("ix_provenance_links_source", "source_id"),
        sa.Index("ix_provenance_links_stage", "stage"),
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    #: The stage this link *lands* on.
    stage: Mapped[str] = mapped_column(sa.Text, nullable=False)
    object_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    #: Denormalised so a query can find everything from a source without walking
    #: the chain, which is the common question.
    source_id: Mapped[str | None] = mapped_column(sa.Text)
    processing_job_id: Mapped[str | None] = mapped_column(sa.Text)
    #: The stage this link came *from*. A link with no parent is a root, which is
    #: legitimate: a hand-uploaded file genuinely has none.
    parent_id: Mapped[str | None] = mapped_column(sa.Text)
    parent_stage: Mapped[str | None] = mapped_column(sa.Text)
    algorithm: Mapped[str] = mapped_column(sa.Text, nullable=False)
    method_description: Mapped[str | None] = mapped_column(sa.Text)
    #: Empty for algorithmic methods. See the class docstring.
    model_name: Mapped[str | None] = mapped_column(sa.Text)
    model_version: Mapped[str | None] = mapped_column(sa.Text)
    parameters: Mapped[dict | None] = mapped_column(JSONB)
    created_by: Mapped[str | None] = mapped_column(sa.Text)
    created_at = created_at()


class ReviewCase(Base):
    """Mutable current state of a human review of one validation finding.

    Exactly one case per issue, enforced by a unique constraint rather than only
    by service code: two open cases on one finding would let two people decide it
    and the audit trail would record a contradiction.

    ``state`` mirrors onto ``validation_issues.status`` so existing consumers of
    the issues collection see the outcome without joining to the workflow.

    ``issue_id`` is a plain text column, not a foreign key. A validation run
    clears and rebuilds the issues collection, and a foreign key would either
    block that or cascade the review history away with it. The review history
    must outlive the run that produced the finding.
    """

    __tablename__ = "review_cases"
    __table_args__ = (
        sa.UniqueConstraint("issue_id", name="uq_review_cases_issue"),
        sa.Index("ix_review_cases_state", "state"),
        sa.Index("ix_review_cases_assignee", "assigned_to"),
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    issue_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    issue_type: Mapped[str | None] = mapped_column(sa.Text)
    severity: Mapped[str | None] = mapped_column(sa.Text)
    object_a: Mapped[str | None] = mapped_column(sa.Text)
    object_b: Mapped[str | None] = mapped_column(sa.Text)
    object_type: Mapped[str | None] = mapped_column(sa.Text)
    state: Mapped[str] = mapped_column(sa.Text, nullable=False)
    assigned_to: Mapped[str | None] = mapped_column(sa.Text)
    priority: Mapped[str | None] = mapped_column(sa.Text)
    reason: Mapped[str | None] = mapped_column(sa.Text)
    created_by: Mapped[str | None] = mapped_column(sa.Text)
    created_at = created_at()
    updated_at = updated_at()
    decided_at: Mapped[dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True)
    )
    decided_by: Mapped[str | None] = mapped_column(sa.Text)
    decision: Mapped[str | None] = mapped_column(sa.Text)
    decision_reason: Mapped[str | None] = mapped_column(sa.Text)
    review_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0, server_default="0"
    )


class ReviewDecision(Base):
    """Immutable record of one human decision on a review case.

    Append-only by construction: the repository gives this collection an empty
    set of updatable fields, so a write attempt is silently dropped rather than
    applied. A decision that can be edited is not evidence of anything.

    Carries the full before/after context an auditor needs -- who, when, what
    they decided, why, which finding, which object, and the states on either
    side -- because a decision row that only says ``APPROVED`` cannot be
    reconstructed into a reason months later.
    """

    __tablename__ = "review_decisions"
    __table_args__ = (
        sa.Index("ix_review_decisions_case", "review_case_id"),
        sa.Index("ix_review_decisions_issue", "issue_id"),
        sa.Index("ix_review_decisions_reviewer", "reviewer"),
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    review_case_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    issue_id: Mapped[str | None] = mapped_column(sa.Text)
    decision: Mapped[str] = mapped_column(sa.Text, nullable=False)
    #: The audit action this decision produced, so the two logs can be joined.
    action: Mapped[str] = mapped_column(sa.Text, nullable=False)
    #: Self-asserted. Authentication is not implemented; see app.services.audit.
    reviewer: Mapped[str | None] = mapped_column(sa.Text)
    reason: Mapped[str] = mapped_column(sa.Text, nullable=False)
    object_id: Mapped[str | None] = mapped_column(sa.Text)
    related_object_id: Mapped[str | None] = mapped_column(sa.Text)
    object_type: Mapped[str | None] = mapped_column(sa.Text)
    previous_state: Mapped[str | None] = mapped_column(sa.Text)
    new_state: Mapped[str] = mapped_column(sa.Text, nullable=False)
    decided_at = created_at()


class AuditEvent(Base):
    """One entry in the append-only cadastral audit log.

    Records what happened, to which object, on whose say-so, and -- where a
    transition occurred -- the states on either side of it. That last part is
    what makes the log replayable rather than merely readable.

    Never updated or deleted; see :class:`ReviewDecision` for why that is
    enforced in the repository layer rather than by convention.
    """

    __tablename__ = "audit_events"
    __table_args__ = (
        sa.Index("ix_audit_events_object", "object_id"),
        sa.Index("ix_audit_events_action", "action"),
        sa.Index("ix_audit_events_actor", "actor"),
        sa.Index("ix_audit_events_issue", "issue_id"),
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    action: Mapped[str] = mapped_column(sa.Text, nullable=False)
    object_id: Mapped[str | None] = mapped_column(sa.Text)
    object_type: Mapped[str | None] = mapped_column(sa.Text)
    #: Self-asserted and unverified; authentication is not implemented.
    actor: Mapped[str] = mapped_column(sa.Text, nullable=False)
    detail: Mapped[str | None] = mapped_column(sa.Text)
    issue_id: Mapped[str | None] = mapped_column(sa.Text)
    review_case_id: Mapped[str | None] = mapped_column(sa.Text)
    previous_state: Mapped[str | None] = mapped_column(sa.Text)
    new_state: Mapped[str | None] = mapped_column(sa.Text)
    occurred_at = created_at()
    #: Machine-readable payload kept separate from the human-readable ``detail``.
    context: Mapped[dict | None] = mapped_column(JSONB)


#: Repository collection name -> model. ``issues`` and ``sources`` are the
#: application's names for ``validation_issues`` and ``data_sources``.
COLLECTION_MODELS: dict[str, type[Base]] = {
    "parcels": Parcel,
    "buildings": Building,
    "floors": Floor,
    "properties": PropertyVolume,
    "infrastructure": Infrastructure,
    "issues": ValidationIssue,
    "sources": DataSource,
    "geometry_versions": GeometryVersion,
    "processing_jobs": ProcessingJob,
    "extracted_buildings": ExtractedBuilding,
    "extracted_floors": ExtractedFloor,
    "generated_property_volumes": GeneratedPropertyVolume,
    "review_cases": ReviewCase,
    "review_decisions": ReviewDecision,
    "audit_events": AuditEvent,
    "cadastral_changes": CadastralChange,
    "provenance_links": ProvenanceLink,
}

__all__ = [
    "COLLECTION_MODELS",
    "AuditEvent",
    "Base",
    "Building",
    "CadastralChange",
    "DataSource",
    "ExtractedBuilding",
    "ExtractedFloor",
    "Floor",
    "GeneratedPropertyVolume",
    "GeometryVersion",
    "Infrastructure",
    "Parcel",
    "ProcessingJob",
    "PropertyVolume",
    "ProvenanceLink",
    "ReviewCase",
    "ReviewDecision",
    "ValidationIssue",
]
