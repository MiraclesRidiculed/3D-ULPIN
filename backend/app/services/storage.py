"""Read/write helpers shared by every storage backend.

Keeping these in one place is what allows the in-memory and PostGIS
repositories to satisfy the same contract, and lets services mutate records
without knowing which backend is underneath.

The ``apply_changes`` family is deliberately narrow: a change set may touch
scalar columns, the geometry field, or the JSON/date fields. Anything else is
ignored rather than raising, so a stale caller cannot corrupt a row.
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Any

#: Record field holding a GeoJSON footprint, per collection. Mirrors
#: ``app.db.models``; kept here so services need not import the ORM.
GEOMETRY_FIELDS: dict[str, str | None] = {
    "parcels": "geometry",
    "buildings": "footprint",
    "floors": "geometry_3d",
    "properties": "geometry_3d",
    "infrastructure": "geometry_3d",
    "issues": "geometry",
    "sources": None,
    "geometry_versions": "geometry",
    "processing_jobs": None,
    "extracted_buildings": "footprint",
    "extracted_floors": "footprint",
    "generated_property_volumes": "geometry_3d",
    "review_cases": None,
    "review_decisions": None,
    "audit_events": None,
    "cadastral_changes": None,
    "provenance_links": None,
}

#: Scalar columns that are safe to update directly.
UPDATABLE_FIELDS: dict[str, frozenset[str]] = {
    "parcels": frozenset(
        {
            "parcel_id",
            "prototype_ulpin",
            "area",
            "land_use",
            "survey_reference",
            "geometry_hash",
            "geometry_version",
            "version",
            "updated_at",
        }
    ),
    "buildings": frozenset(
        {
            "building_id",
            "parcel_id",
            "geometry_3d",
            "height",
            "floor_count",
            "confidence",
            "geometry_hash",
            "geometry_version",
            "version",
            "updated_at",
        }
    ),
    "floors": frozenset(
        {
            "building_id",
            "floor_number",
            "z_min",
            "z_max",
            "confidence",
            "version",
            "updated_at",
        }
    ),
    "properties": frozenset(
        {
            "prototype_ulpin",
            "parent_parcel_id",
            "building_id",
            "property_type",
            "floor_number",
            "unit_label",
            "z_min",
            "z_max",
            "volume_m3",
            "area_m2",
            "geometry_hash",
            "status",
            "confidence",
            "geometry_version",
            "version",
            "updated_at",
        }
    ),
    "infrastructure": frozenset(
        {
            "type",
            "z_min",
            "z_max",
            "owner",
            "reference",
            "geometry_hash",
            "geometry_version",
            "version",
            "updated_at",
        }
    ),
    "issues": frozenset(
        {
            "object_a",
            "object_b",
            "issue_type",
            "severity",
            "overlap_volume",
            "gap_m",
            "description",
            "status",
            "location",
            "rule_id",
            "category",
        }
    ),
    "sources": frozenset(
        {
            "source_type",
            "filename",
            "crs",
            "acquisition_date",
            "created_at",
        }
    ),
    "processing_jobs": frozenset(
        {
            "source_id",
            "job_type",
            "status",
            "point_count",
            "crs",
            "bounds",
            "detail",
            "error",
            "completed_at",
            "created_by",
        }
    ),
    "geometry_versions": frozenset(
        {
            "object_id",
            "object_type",
            "version",
            "z_min",
            "z_max",
            "geometry_hash",
            "source_id",
            "processing_job_id",
            "created_by",
            "change_reason",
            "created_at",
        }
    ),
    "extracted_buildings": frozenset(
        {
            "source_id",
            "processing_job_id",
            "height_m",
            "crs",
            "method",
            "extractor",
            "extractor_version",
            "geometry_hash",
            "geometric_quality",
        }
    ),
    "extracted_floors": frozenset(
        {
            "source_id",
            "processing_job_id",
            "building_id",
            "floor_number",
            "z_min",
            "z_max",
            "z_min_above_ground",
            "z_max_above_ground",
            "geometry_3d",
            "area_m2",
            "volume_m3",
            "crs",
            "method",
            "geometry_hash",
            "geometric_quality",
            "requires_human_review",
        }
    ),
    "generated_property_volumes": frozenset(
        {
            "source_id",
            "prototype_ulpin",
            "parent_parcel_id",
            "building_id",
            "floor_number",
            "floor_label",
            "unit_label",
            "unit_label_source",
            "volume_scope",
            "property_type",
            "vertical_position",
            "z_min",
            "z_max",
            "geometry_3d",
            "area_m2",
            "volume_m3",
            "geometry_hash",
            "geometry_version",
            "status",
            "units_inferred",
            "requires_human_review",
        }
    ),
    # A review case is mutable current state: it is assigned, decided and
    # reopened. Its *history* lives in ``review_decisions``, which is not
    # updatable, so rewriting the case never rewrites what was decided.
    "review_cases": frozenset(
        {
            "issue_type",
            "severity",
            "object_a",
            "object_b",
            "object_type",
            "state",
            "assigned_to",
            "priority",
            "reason",
            "created_by",
            "updated_at",
            "decided_at",
            "decided_by",
            "decision",
            "decision_reason",
            "review_count",
        }
    ),
    # Append-only history. An empty set is load-bearing, not an oversight: the
    # repository silently drops fields that are not listed, so a write attempt
    # against a review decision or an audit event is rejected rather than
    # applied. A trail that can be edited is not a trail.
    "review_decisions": frozenset(),
    "audit_events": frozenset(),
    # A change record is a *finding about a comparison that already happened*.
    # Both geometries are retained on it, so re-running the comparison must not
    # rewrite what was observed: the empty set makes it immutable, which is what
    # lets a reviewer rely on the record months later.
    "cadastral_changes": frozenset(),
    # Provenance is a statement about what already happened, so it is immutable
    # in the same way the audit log is. A rewritten link would make the lineage
    # a reconstruction rather than a record.
    "provenance_links": frozenset(),
}

#: Collections whose records carry a ``metadata`` dict, as
#: ``collection -> (column attribute, record key)``. The attribute differs from
#: the record key because ``metadata`` is reserved by SQLAlchemy's declarative
#: base.
JSON_FIELD: dict[str, tuple[str, str]] = {
    "sources": ("source_metadata", "metadata"),
    "processing_jobs": ("job_metadata", "metadata"),
    "extracted_buildings": ("extraction", "extraction"),
    "extracted_floors": ("segmentation", "segmentation"),
    #: ``evidence`` holds the numbers behind a finding; the record key and the
    #: column attribute are the same, unlike ``metadata`` which is reserved.
    "issues": ("evidence", "evidence"),
    #: Free-form machine-readable payload carried alongside a human-readable
    #: sentence on an audit event.
    "audit_events": ("context", "context"),
    "provenance_links": ("parameters", "parameters"),
}

#: Collections whose records carry an ISO ``acquisition_date``, as
#: ``collection -> (column attribute, record key)``.
DATE_FIELD: dict[str, tuple[str, str]] = {
    "sources": ("acquisition_date", "acquisition_date")
}

#: Column names holding a timestamp. Postgres returns these as ``datetime`` while
#: the in-memory store and the Pydantic schemas use ISO strings, so a
#: SQLAlchemy-backed repository must normalise them or the two backends would
#: return different record shapes for the same data.
#:
#: ``occurred_at`` (audit events), ``decided_at`` (review cases and decisions)
#: and ``detected_at`` (change records) are here for the same reason as
#: ``created_at``: an un-normalised ``datetime`` in a response would be a
#: backend-dependent shape difference, and the parity between the two stores is
#: deliberate and test-enforced. Omitting one silently makes a field a
#: ``datetime`` under PostGIS and an ISO string in memory.
TIMESTAMP_FIELDS: frozenset[str] = frozenset(
    {"created_at", "updated_at", "occurred_at", "decided_at", "detected_at"}
)

#: Convenience sets for membership tests.
JSON_COLLECTIONS: frozenset[str] = frozenset(JSON_FIELD)
DATE_COLLECTIONS: frozenset[str] = frozenset(DATE_FIELD)


def to_iso_timestamp(value: Any) -> Any:
    """Coerce a ``datetime`` to an ISO string; anything else passes through."""
    if isinstance(value, dt.datetime):
        return value.isoformat()
    return value


def normalise_changes(kind: str, changes: Mapping[str, Any]) -> dict[str, Any]:
    """Filter a change set down to fields this collection may update.

    Geometry, JSON and date fields are passed through for the backend to encode;
    unknown or read-only fields are dropped.
    """
    allowed = UPDATABLE_FIELDS.get(kind, frozenset())
    geom_field = GEOMETRY_FIELDS.get(kind)
    out: dict[str, Any] = {
        k: v for k, v in changes.items() if k in allowed and v is not None
    }
    if geom_field and changes.get(geom_field):
        out[geom_field] = changes[geom_field]
    if kind in JSON_COLLECTIONS:
        # The JSON key is per collection (``metadata``, ``segmentation``,
        # ``extraction``); hard-coding one name silently dropped the payload for
        # every collection that used a different one.
        _, json_key = JSON_FIELD[kind]
        if json_key in changes:
            out[json_key] = changes[json_key]
    if kind in DATE_COLLECTIONS and changes.get("acquisition_date"):
        out["acquisition_date"] = changes["acquisition_date"]
    return out


def to_date(value: Any) -> dt.date | None:
    """Coerce an ISO string or ``date`` to ``date``; ``None`` passes through."""
    if value is None or isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value)[:10])


def to_iso(value: Any) -> Any:
    """Coerce a ``date`` to an ISO string so API output is unchanged."""
    if isinstance(value, dt.date):
        return value.isoformat()
    return value
