"""Stable cadastral identity.

A cadastral identifier must survive re-survey. The prototype ULPIN therefore
derives **only from stable, non-geometric attributes** — record kind, parent
parcel and unit label. Nothing derived from the geometry participates, so
editing a footprint cannot mint a new identifier.

.. warning::
   The identifiers produced here are a **deterministic prototype format created
   for this demonstration**. They are *not* official Government of India ULPINs
   and imply no government integration.

Separation of concerns
----------------------
===================  =========================================================
``prototype_ulpin``  Stable identity. Assigned once, never recomputed from
                     geometry, unchanged by ``create_geometry_version()``.
``geometry_hash``    Digest of the *current* geometry. Owned by the geometry
                     versioning service, changes when geometry changes.
``geometry_version`` Monotonic revision counter for the geometry.
===================  =========================================================

Format::

    VC-LP-{parcel_id}                              # land parcel
    VC-VP-{parent_parcel_id}-{unit_label}          # volumetric property right
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from app.models.enums import AuditAction
from app.repositories.base import CadastreRepository
from app.services.audit import create_audit_event

#: Shown in API responses. Keep the prototype disclaimer attached.
ULPIN_LABEL = (
    "Prototype 3D ULPIN — deterministic prototype identifier, "
    "not an official ULPIN."
)

#: Identifier namespace prefix. Invented for the prototype; not official.
ULPIN_PREFIX = "VC"

LAND_PREFIX = "LP"
VOLUME_PREFIX = "VP"

#: Used when a record has no distinguishing unit label.
LAND_FALLBACK_UNIT = "LAND"

_NON_ALNUM = re.compile(r"[^A-Z0-9]+")


def _slug(value: str | None) -> str:
    """Normalise a component to uppercase alphanumerics.

    ``"P-001"`` -> ``"P001"``, ``"Apartment 201"`` -> ``"APARTMENT201"``.
    """
    if not value:
        return ""
    return _NON_ALNUM.sub("", value.upper())


def generate_stable_ulpin(*, prefix: str, parent: str, unit: str) -> str:
    """Build a geometry-independent cadastral identifier.

    Every component is a stable attribute of the record, so the result is
    reproducible across processes and invariant under geometry edits.
    """
    parts = [ULPIN_PREFIX, prefix, _slug(parent), _slug(unit) or LAND_FALLBACK_UNIT]
    return "-".join(p for p in parts if p)


def ulpin_for_record(record: Mapping[str, Any]) -> str:
    """Derive the stable identifier for a cadastral record.

    Property volumes are identified by parent parcel plus unit label; land
    parcels by their own parcel id. Keyed on ``parent_parcel_id``, so only
    records that are volumetric rights get a ``VP`` identifier.
    """
    if "parent_parcel_id" in record:
        return generate_stable_ulpin(
            prefix=VOLUME_PREFIX,
            parent=record.get("parent_parcel_id") or "ROOT",
            unit=record.get("unit_label") or record.get("id", ""),
        )
    parcel_id = _slug(record.get("parcel_id", ""))
    return f"{ULPIN_PREFIX}-{LAND_PREFIX}-{parcel_id}" if parcel_id else ""


def assign_ulpins(
    repo: CadastreRepository,
    *,
    overwrite: bool = False,
    parent_parcel_id: str | None = None,
) -> dict[str, Any]:
    """Ensure every parcel and property volume carries a stable identifier.

    By default an existing identifier is left untouched, which is what makes
    identity genuinely stable: even a change to the derivation rules cannot
    silently re-issue live identifiers. Pass ``overwrite=True`` to deliberately
    re-issue them all.

    Geometry hashes and versions are owned by
    :mod:`app.services.geometry_versioning` and are not touched here.

    ``parent_parcel_id`` narrows the operation to one parcel's records. It used
    to be accepted by the route and dropped, so a caller asking for one parcel
    got every parcel. Absent means the whole scene, which is what it always did.

    Returns ``generated`` (records now carrying an identifier, matching the
    original response contract) and ``assigned`` (newly issued by this call).
    """
    records = repo.records("parcels") + repo.records("properties")
    if parent_parcel_id:
        records = [
            r
            for r in records
            if r.get("parcel_id") == parent_parcel_id
            or r.get("parent_parcel_id") == parent_parcel_id
        ]
    assigned = 0
    issued: list[str] = []
    for record in records:
        if overwrite or not record.get("prototype_ulpin"):
            # A record present in both listings (a parcel is not, but be explicit)
            # is updated once; the identifier is a function of stable attributes.
            collection = "parcels" if "parcel_id" in record else "properties"
            identifier = ulpin_for_record(record)
            repo.update(
                collection,
                record["id"],
                {"prototype_ulpin": identifier},
            )
            issued.append(f"{record['id']}={identifier}")
            assigned += 1
    if issued:
        # Logged as a batch rather than one event per record: a demo seed issues
        # twenty identifiers at once, and twenty near-identical events would bury
        # everything else in the trail. The full list is in ``context``.
        create_audit_event(
            repo,
            action=AuditAction.ULPIN_GENERATED.value,
            object_id=None,
            object_type="SCENE",
            detail=f"{assigned} prototype 3D ULPIN(s) issued",
            extra={"identifiers": issued, "overwrite": overwrite, "label": ULPIN_LABEL},
        )
    return {
        "generated": sum(1 for r in records if r.get("prototype_ulpin")),
        "assigned": assigned,
        "label": ULPIN_LABEL,
    }
