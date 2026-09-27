"""Geometry version history.

Read-only. Exposes the history that
:mod:`app.services.geometry_versioning` already maintains, so a reviewer can ask
what an object's geometry was, when it changed, and what moved -- without the
service, the ``geometry_versions`` table or the versioning rules changing at
all. No route here writes: a geometry change is applied through the pipeline, and
adding a write route would mean deciding who may move a cadastral boundary, which
is a policy question this layer must not answer.

The invariant these routes exist to make checkable:

    stable ULPIN
        -> geometry version 1
        -> geometry version 2
        -> geometry version N

A geometry change produces a new version and a new hash. It does not produce a
new identifier, and ``ulpin_changed`` in a comparison is therefore always False.

.. warning::
   These are **prototype** identifiers. ``VC-LP-…`` / ``VC-VP-…`` values are a
   deterministic format invented for this demonstration and are not official
   Government of India ULPINs.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import request_repository
from app.models.schemas import (
    CurrentGeometryVersion,
    GeometryVersion,
    GeometryVersionComparison,
)
from app.repositories.base import CadastreRepository
from app.services.geometry_versioning import (
    OBJECT_TYPE,
    VERSIONED_COLLECTIONS,
    compare_geometry_versions,
    get_current_geometry_version,
    get_geometry_history,
)

router = APIRouter(tags=["geometry-versions"])


def _resolve(repo: CadastreRepository, ident: str) -> dict[str, Any]:
    """Resolve an object identifier to its record.

    Accepts the record's primary key, or exactly -- never as a substring, which
    would be ambiguous -- its business id or its prototype ULPIN. Business ids are
    accepted because every other read route in this API accepts them
    (``/parcels/{ident}``, ``/properties/{ident}``), and a caller holding
    ``P-001`` should not have to know the row id is ``parcel-001``.

    Only collections that actually carry a versioned footprint are searched, so a
    data source or a processing job cannot be mistaken for a cadastral object.
    """
    found = repo.find_by_id(ident)
    if found is not None:
        collection, record = found
        if collection in VERSIONED_COLLECTIONS:
            return record
        raise HTTPException(
            404,
            f"{ident!r} is a {collection[:-1]} and carries no versioned geometry",
        )
    for collection in VERSIONED_COLLECTIONS:
        for record in repo.records(collection):
            if ident in (record.get("parcel_id"), record.get("prototype_ulpin")):
                return record
    raise HTTPException(
        404, f"No cadastral object matches {ident!r}"
    )


def _version_or_404(
    history: list[GeometryVersion], object_id: str, version: int
) -> GeometryVersion:
    """One version by number, or a 404 naming what does exist."""
    for candidate in history:
        if candidate.version == version:
            return candidate
    available = [str(v.version) for v in history] or ["none recorded"]
    raise HTTPException(
        404,
        f"{object_id!r} has no geometry version {version}; "
        f"recorded versions: {', '.join(available)}",
    )


@router.get("/geometry-versions/{object_id}", response_model=CurrentGeometryVersion)
def current_geometry_version(
    object_id: str,
    repository: CadastreRepository = Depends(request_repository),
) -> dict[str, Any]:
    """The object's current geometry version, with its stable identifier.

    ``prototype_ulpin`` is read from the owning record rather than from the
    version row, which has no identifier field by design. It is the same value on
    every version of an object -- that is the point.
    """
    record = _resolve(repository, object_id)
    current = get_current_geometry_version(repository, record["id"])
    if current is None:
        raise HTTPException(
            404,
            f"No geometry version has been recorded for {record['id']!r}",
        )
    return {
        "object_id": current.object_id,
        "object_type": current.object_type,
        "prototype_ulpin": record.get("prototype_ulpin"),
        "version": current.version,
        "geometry_hash": current.geometry_hash,
        "geometry": current.geometry.model_dump(),
        "z_min": current.z_min,
        "z_max": current.z_max,
        "source_id": current.source_id,
        "processing_job_id": current.processing_job_id,
        "created_at": current.created_at,
        "created_by": current.created_by,
        "change_reason": current.change_reason,
    }


@router.get(
    "/geometry-versions/{object_id}/history", response_model=list[GeometryVersion]
)
def geometry_version_history(
    object_id: str,
    repository: CadastreRepository = Depends(request_repository),
) -> list[dict[str, Any]]:
    """Every recorded version of an object, oldest first.

    Ordering is by version number, so the list is deterministic and the last entry
    is always the current one. Each entry is exactly what was stored: no field is
    back-filled and no identifier is attached, because none is recorded on a
    version.
    """
    record = _resolve(repository, object_id)
    return [v.model_dump() for v in get_geometry_history(repository, record["id"])]


@router.get(
    "/geometry-versions/{object_id}/versions/{version}", response_model=GeometryVersion
)
def specific_geometry_version(
    object_id: str,
    version: int,
    repository: CadastreRepository = Depends(request_repository),
) -> dict[str, Any]:
    """One historical version, as it was recorded at the time.

    This is the audit path: the geometry and hash stored then, not today's
    geometry with today's version number. A version that does not exist is a 404
    naming the versions that do, not an empty result.
    """
    record = _resolve(repository, object_id)
    history = get_geometry_history(repository, record["id"])
    if not history:
        raise HTTPException(
            404, f"No geometry version has been recorded for {record['id']!r}"
        )
    return _version_or_404(history, record["id"], version).model_dump()


@router.get(
    "/geometry-versions/{object_id}/compare",
    response_model=GeometryVersionComparison,
)
def compare_object_versions(
    object_id: str,
    from_version: int,
    to_version: int,
    repository: CadastreRepository = Depends(request_repository),
) -> dict[str, Any]:
    """Diff two versions of the same object.

    Reports what moved and by how much: the hash on each side, whether the
    geometry or the vertical band changed, and the area, volume and Z deltas
    computed by the geometry engine.

    It reports no legal conclusion. A changed boundary is a measured difference
    between two observations, not a finding about entitlement, validity or
    offence -- that judgement belongs to a human reviewer, and is made through the
    review workflow rather than here.

    ``ulpin_changed`` is always False, which is the machine-checkable form of the
    identity invariant. ``prototype_ulpin`` is the object's current identifier,
    identical for both sides.
    """
    record = _resolve(repository, object_id)
    history = get_geometry_history(repository, record["id"])
    if not history:
        raise HTTPException(
            404, f"No geometry version has been recorded for {record['id']!r}"
        )
    before = _version_or_404(history, record["id"], from_version)
    after = _version_or_404(history, record["id"], to_version)
    comparison = compare_geometry_versions(before, after)
    return {
        **comparison.model_dump(),
        "prototype_ulpin": record.get("prototype_ulpin"),
    }


@router.get("/geometry-versions")
def list_versioned_objects(
    repository: CadastreRepository = Depends(request_repository),
) -> dict[str, Any]:
    """Which object types carry versioned geometry, and how many are recorded.

    A discovery route, so a caller can find the object types before guessing at
    identifiers. It names the collections and object-type labels the versioning
    service uses, which are otherwise only visible in the history rows.
    """
    counts = {
        collection: len(repository.records(collection))
        for collection in VERSIONED_COLLECTIONS
    }
    return {
        "versioned_collections": list(VERSIONED_COLLECTIONS),
        "object_types": dict(OBJECT_TYPE),
        "object_counts": counts,
        "total_objects": sum(counts.values()),
        "note": (
            "Every listed object carries a versioned footprint. A stable "
            "prototype ULPIN is issued for parcels and property volumes; "
            "buildings and infrastructure carry none, and their absence from a "
            "version response means none was issued, not that one was lost."
        ),
    }


__all__ = ["router"]
