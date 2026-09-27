"""Geometry versioning.

Owns the *current* geometry of every cadastral object and the immutable history
of how it got there. A geometry edit produces a new **version** with a new
**hash**; it never produces a new **identifier**.

The separation matters for a cadastre: the parcel is the same legal object
whether or not its boundary was re-surveyed, and an audit trail has to be able
to prove that. See :mod:`app.services.ulpin`.

Not yet implemented: persistence (the history is in-memory), ownership or
authentication (`created_by` is recorded but not enforced), and audit logging.
"""
from __future__ import annotations

from typing import Any, Mapping

from app.models.enums import AuditAction
from app.models.schemas import Geometry, GeometryVersion, GeometryVersionComparison
from app.repositories.base import CadastreRepository
from app.services.audit import create_audit_event
from app.services.geometry import (
    calculate_area,
    calculate_geometry_hash,
    calculate_volume,
    polygon_to_geojson,
    record_to_polygon,
    validate_z_range,
)
from app.utils import now

#: Reason recorded for the first version of an object.
INITIAL_CHANGE_REASON = "initial"

#: Field holding a footprint on each record type, per collection.
GEOMETRY_FIELD: dict[str, str] = {
    "parcels": "geometry",
    "buildings": "footprint",
    "properties": "geometry_3d",
    "infrastructure": "geometry_3d",
}

#: Collection name -> object type label used in version records.
OBJECT_TYPE: dict[str, str] = {
    "parcels": "PARCEL",
    "buildings": "BUILDING",
    "properties": "PROPERTY_VOLUME",
    "infrastructure": "INFRASTRUCTURE",
}

#: Collections that carry a versioned footprint.
VERSIONED_COLLECTIONS: tuple[str, ...] = (
    "parcels",
    "buildings",
    "properties",
    "infrastructure",
)


class UnknownObject(LookupError):
    """Raised when an object id does not resolve to a cadastral record."""


# --------------------------------------------------------------------------
# history reads
# --------------------------------------------------------------------------


def get_geometry_history(
    repo: CadastreRepository, object_id: str
) -> list[GeometryVersion]:
    """Full version history for an object, oldest first.

    Returns an empty list for an object that exists but has no versions yet.
    """
    versions = [
        GeometryVersion(**v)
        for v in repo.records("geometry_versions")
        if v["object_id"] == object_id
    ]
    return sorted(versions, key=lambda v: v.version)


def get_current_geometry_version(
    repo: CadastreRepository, object_id: str
) -> GeometryVersion | None:
    """The highest-numbered version for an object, or ``None``."""
    history = get_geometry_history(repo, object_id)
    return history[-1] if history else None


def get_previous_geometry_version(
    repo: CadastreRepository, object_id: str
) -> GeometryVersion | None:
    """The version immediately before the current one, or ``None``."""
    history = get_geometry_history(repo, object_id)
    return history[-2] if len(history) >= 2 else None


# --------------------------------------------------------------------------
# comparison
# --------------------------------------------------------------------------


def compare_geometry_versions(
    before: GeometryVersion, after: GeometryVersion
) -> GeometryVersionComparison:
    """Diff two versions of the same object.

    ``ulpin_changed`` is always ``False`` — identity is not carried on a
    version record, so it cannot change here. It is reported explicitly as the
    machine-checkable form of that invariant.
    """
    if before.object_id != after.object_id:
        raise ValueError(
            f"cannot compare versions of different objects: "
            f"{before.object_id!r} vs {after.object_id!r}"
        )

    before_area = calculate_area(record_to_polygon(before.model_dump()))
    after_area = calculate_area(record_to_polygon(after.model_dump()))
    before_volume = calculate_volume(
        record_to_polygon(before.model_dump()), before.z_min, before.z_max
    )
    after_volume = calculate_volume(
        record_to_polygon(after.model_dump()), after.z_min, after.z_max
    )

    return GeometryVersionComparison(
        object_id=after.object_id,
        from_version=before.version,
        to_version=after.version,
        ulpin_changed=False,
        geometry_changed=before.geometry_hash != after.geometry_hash,
        geometry_hash_changed=before.geometry_hash != after.geometry_hash,
        z_range_changed=(before.z_min, before.z_max) != (after.z_min, after.z_max),
        from_geometry_hash=before.geometry_hash,
        to_geometry_hash=after.geometry_hash,
        delta_z_min=round(after.z_min - before.z_min, 6),
        delta_z_max=round(after.z_max - before.z_max, 6),
        from_area_m2=round(before_area, 2),
        to_area_m2=round(after_area, 2),
        delta_area_m2=round(after_area - before_area, 2),
        from_volume_m3=round(before_volume, 2),
        to_volume_m3=round(after_volume, 2),
        delta_volume_m3=round(after_volume - before_volume, 2),
    )


# --------------------------------------------------------------------------
# writes
# --------------------------------------------------------------------------


def _resolve(
    repo: CadastreRepository, object_id: str
) -> tuple[str, dict[str, Any]]:
    """Locate ``(collection, record)`` for an object id."""
    for collection in VERSIONED_COLLECTIONS:
        for record in repo.records(collection):
            if record["id"] == object_id:
                return collection, record
    raise UnknownObject(object_id)


def snapshot_geometry(
    repo: CadastreRepository,
    object_id: str,
    *,
    version: int = 1,
    source_id: str | None = None,
    processing_job_id: str | None = None,
    created_by: str | None = None,
    change_reason: str | None = None,
) -> GeometryVersion:
    """Record the object's current geometry as a new version.

    Does not modify the record; use :func:`create_geometry_version` to both
    change the geometry and version it. Useful for capturing provenance against
    geometry that was set some other way.
    """
    collection, record = _resolve(repo, object_id)
    z_min, z_max = _z_range(record)
    polygon = record_to_polygon(record)
    snapshot = GeometryVersion(
        object_id=object_id,
        object_type=OBJECT_TYPE[collection],
        version=version,
        geometry=polygon_to_geojson(polygon),
        z_min=z_min,
        z_max=z_max,
        geometry_hash=calculate_geometry_hash(polygon, z_min, z_max),
        source_id=source_id,
        processing_job_id=processing_job_id,
        created_at=now(),
        created_by=created_by,
        change_reason=change_reason or INITIAL_CHANGE_REASON,
    )
    # Captured before the insert: after it, this row *is* the current version and
    # the comparison below would find nothing to compare against.
    previous = get_current_geometry_version(repo, object_id)
    repo.add(
        "geometry_versions",
        {**snapshot.model_dump(), "id": _version_id(object_id, version)},
    )
    _audit_geometry_version(
        repo,
        object_id=object_id,
        collection=collection,
        version=version,
        geometry_hash=snapshot.geometry_hash,
        previous=previous,
        moved=False,
        source_id=source_id,
        processing_job_id=processing_job_id,
        created_by=created_by,
        change_reason=change_reason or INITIAL_CHANGE_REASON,
    )
    return snapshot


def create_geometry_version(
    repo: CadastreRepository,
    object_id: str,
    *,
    geometry: Mapping[str, Any] | None = None,
    z_min: float | None = None,
    z_max: float | None = None,
    source_id: str | None = None,
    processing_job_id: str | None = None,
    created_by: str | None = None,
    change_reason: str | None = None,
) -> GeometryVersion:
    """Apply a geometry change and version it.

    Increments ``geometry_version``, recomputes ``geometry_hash``, refreshes the
    derived area/volume fields, and appends an immutable history entry.

    The record's ``prototype_ulpin`` is **not touched**: identity is stable
    across geometry revisions. Omit ``geometry``/``z_min``/``z_max`` to record a
    new version capturing a provenance change without moving the geometry.
    """
    collection, record = _resolve(repo, object_id)
    field = GEOMETRY_FIELD[collection]
    current_z_min, current_z_max = _z_range(record)
    #: Parcels are 2D land objects, so a zero Z extent is correct rather than
    #: degenerate and must not be rejected. Only volumetric records are checked.
    is_volumetric = "z_min" in record

    next_z_min = current_z_min if z_min is None else z_min
    next_z_max = current_z_max if z_max is None else z_max
    if is_volumetric:
        z_check = validate_z_range(next_z_min, next_z_max)
        if not z_check.is_valid:
            raise ValueError(f"invalid Z range for {object_id}: {z_check.reason}")

    changes: dict[str, Any] = {}
    if geometry is not None:
        changes[field] = Geometry(**dict(geometry)).model_dump()
        changes["updated_at"] = now()
    working = {**record, **changes}

    polygon = record_to_polygon(working)
    new_hash = calculate_geometry_hash(polygon, next_z_min, next_z_max)

    current = get_current_geometry_version(repo, object_id)
    next_version = (current.version + 1) if current else 1

    snapshot = GeometryVersion(
        object_id=object_id,
        object_type=OBJECT_TYPE[collection],
        version=next_version,
        geometry=polygon_to_geojson(polygon),
        z_min=next_z_min,
        z_max=next_z_max,
        geometry_hash=new_hash,
        source_id=source_id,
        processing_job_id=processing_job_id,
        created_at=now(),
        created_by=created_by,
        change_reason=change_reason or "geometry updated",
    )
    repo.add("geometry_versions", {**snapshot.model_dump(), "id": _version_id(object_id, next_version)})

    # Identity is intentionally not reassigned here.
    changes.update(
        {
            "geometry_hash": new_hash,
            "geometry_version": next_version,
            **_derived_changes(collection, polygon, next_z_min, next_z_max),
        }
    )
    repo.update(collection, object_id, changes)
    _audit_geometry_version(
        repo,
        object_id=object_id,
        collection=collection,
        version=next_version,
        geometry_hash=new_hash,
        previous=current,
        moved=geometry is not None
        or next_z_min != current_z_min
        or next_z_max != current_z_max,
        source_id=source_id,
        processing_job_id=processing_job_id,
        created_by=created_by,
        change_reason=change_reason or "geometry updated",
    )
    return snapshot


def _audit_geometry_version(
    repo: CadastreRepository,
    *,
    object_id: str,
    collection: str,
    version: int,
    geometry_hash: str,
    previous: GeometryVersion | None,
    moved: bool,
    source_id: str | None,
    processing_job_id: str | None,
    created_by: str | None,
    change_reason: str,
) -> None:
    """Record one geometry version in the audit log.

    The action distinguishes three cases that mean different things to anyone
    reconstructing the record later: a first observation, a genuine boundary
    change, and a revision that moved nothing. Collapsing the last two into one
    ``UPDATED`` would make the log claim a survey happened when it did not --
    which is the specific false claim an audit trail must not make.
    """
    changed = previous is not None and previous.geometry_hash != geometry_hash
    if previous is None:
        action = AuditAction.CREATED
        detail = f"{object_id} first geometry version recorded ({geometry_hash})"
    elif changed:
        action = AuditAction.GEOMETRY_CHANGED
        detail = (
            f"{object_id} geometry revised to version {version} "
            f"({previous.geometry_hash} -> {geometry_hash})"
        )
    else:
        action = AuditAction.UPDATED
        detail = f"{object_id} version {version} recorded with no geometry change"

    create_audit_event(
        repo,
        action=action.value,
        object_id=object_id,
        object_type=OBJECT_TYPE[collection],
        actor=created_by,
        detail=detail,
        previous_state=(
            f"version {previous.version} ({previous.geometry_hash})"
            if previous
            else None
        ),
        new_state=f"version {version} ({geometry_hash})",
        extra={
            "change_reason": change_reason,
            "geometry_moved": bool(changed or moved),
            "source_id": source_id,
            "processing_job_id": processing_job_id,
        },
    )


def _version_id(object_id: str, version: int) -> str:
    """Deterministic primary key for a geometry history row."""
    return f"GV-{object_id}-v{version}"


def _z_range(record: Mapping[str, Any]) -> tuple[float, float]:
    """Vertical extent of a record; parcels are flat and sit at zero."""
    if "z_min" in record:
        return float(record["z_min"]), float(record["z_max"])
    return 0.0, 0.0


def _derived_changes(
    collection: str, polygon: Any, z_min: float, z_max: float
) -> dict[str, Any]:
    """Recompute cached area/volume so they cannot drift from the geometry.

    Returned as a change set rather than mutated in place, so the same code path
    works whether records are dicts in memory or ORM rows in Postgres.
    """
    if collection == "parcels":
        return {"area": polygon.area}
    if collection == "properties":
        return {
            "area_m2": round(calculate_area(polygon), 2),
            "volume_m3": round(calculate_volume(polygon, z_min, z_max), 2),
        }
    if collection == "buildings":
        # A building's vertical extent is a storey count, not z_min/z_max.
        return {
            "geometry_3d": {
                "z_min": z_min,
                "z_max": z_max,
                "footprint": polygon_to_geojson(polygon),
            }
        }
    return {}


__all__ = [
    "GEOMETRY_FIELD",
    "INITIAL_CHANGE_REASON",
    "OBJECT_TYPE",
    "UnknownObject",
    "VERSIONED_COLLECTIONS",
    "compare_geometry_versions",
    "create_geometry_version",
    "get_current_geometry_version",
    "get_geometry_history",
    "get_previous_geometry_version",
    "snapshot_geometry",
]
