"""Cadastral change detection.

Compares the **approved cadastre** against a **new survey** and reports where
they disagree.

What this module does and does not conclude
--------------------------------------------
It reports a *measured difference*: two geometries that do not match, or a storey
present on one side and absent on the other. It does not conclude that anyone
altered anything, that a change was unauthorised, or that a boundary was
encroached. A comparison sees numbers; it cannot see who did what or whether they
were entitled to.

Every record therefore carries :data:`~app.models.enums.CHANGE_REQUIRES_VERIFICATION`
and a status of ``REQUIRES_VERIFICATION``, and each one is filed as a review case
so a human resolves it. The vocabulary in :class:`~app.models.enums.ChangeStatus`
has no "illegal" or "unauthorised" value at all, and
``test_change_detection.py::test_no_change_module_claims_wrongdoing`` enforces
that by scanning this file's own text for such words. That test is the mechanism
by which the constraint survives future edits.

Change types
------------
Five differences are reported, and they are **not** mutually exclusive: a storey
whose boundary moved *and* whose height changed produces two records for the same
object, because they are two different facts a reviewer must judge separately.

=========================  =============================================
``FOOTPRINT``              Plan area and/or outline differ.
``HEIGHT``                 Vertical extent differs.
``VOLUME``                 Enclosed volume differs.
``NEW_FLOOR``              A storey in the survey that the approved
                           cadastre does not have. Filed as a specific
                           ``UNREGISTERED_FLOOR`` finding.
``REMOVED_FLOOR``          A storey in the approved cadastre that the new
                           survey does not cover.
=========================  =============================================

``REMOVED_FLOOR`` deserves a caveat
-----------------------------------
A storey missing from a new survey is not a demolition. It may be outside the
survey's extent, obscured, or simply not visited. The finding says "absent from
the new survey" and nothing more, and
:data:`~app.models.enums.CHANGE_REQUIRES_VERIFICATION` applies to it like any
other change.

On ``geometric_quality``
-----------------------
The change record carries a quality metric, as a change record should. It is an
**agreement score** in ``[0, 1]`` derived from the geometry alone -- for a
footprint, the intersection-over-union of the two outlines; for a height or
volume, ``1 - |delta| / max(|a|, |b|)``.

It is emphatically **not** a confidence in the survey, a probability that the
change is genuine, or a statement about who made it. ``1.0`` means the two
geometries agree, ``0.0`` means they share nothing. No field here is named
``confidence``, and the schema has no such column -- consistent with the rest of
the system, where ``geometric_quality`` is a regularity measure and an accuracy
figure would be an invention.

Tolerances
----------
Defaults are 0.01 m / m² / m³, matching the rest of the system, and a caller can
pass a larger ``tolerance`` for a survey whose stated uncertainty is coarser than
that. Nothing here decides what counts as a real difference for a particular
survey; that is the surveyor's call, so it is a parameter rather than a constant.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from app.models.enums import (
    CHANGE_REQUIRES_VERIFICATION,
    AuditAction,
    ChangeStatus,
    ChangeType,
    IssueSeverity,
    IssueType,
)
from app.repositories.base import CadastreRepository
from app.services.audit import create_audit_event
from app.services.geometry import (
    calculate_area,
    calculate_volume,
    record_to_polygon,
    shape_to_geojson,
)
from app.utils import now

#: Minimum difference (m, m² or m³) for a change to be reported at all.
#: Matches the threshold used throughout the validation engine.
DEFAULT_TOLERANCE = 0.01

#: Rule id recorded on the ``UNREGISTERED_FLOOR`` finding.
UNREGISTERED_FLOOR_RULE = "CHANGE-UNREGISTERED-FLOOR"

#: Geometry fields searched, in order, on a supplied record.
GEOMETRY_FIELDS: tuple[str, ...] = ("geometry_3d", "geometry", "footprint")


class ChangeDetectionError(RuntimeError):
    """Raised when a comparison cannot be performed as asked."""


# --------------------------------------------------------------------------
# input / output shapes
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SurveyGeometry:
    """One object's geometry as recorded on one side of a comparison.

    Built by :func:`survey_geometry` from a repository record, or constructed
    directly by a caller holding survey data from elsewhere.
    """

    object_id: str
    geometry: dict[str, Any] | None = None
    z_min: float | None = None
    z_max: float | None = None
    floor_number: int | None = None
    building_id: str | None = None
    object_type: str = "PROPERTY_VOLUME"

    @property
    def has_geometry(self) -> bool:
        return isinstance(self.geometry, Mapping) and "coordinates" in self.geometry

    @property
    def has_z_range(self) -> bool:
        return self.z_min is not None and self.z_max is not None

    @property
    def z_range(self) -> tuple[float, float] | None:
        if not self.has_z_range:
            return None
        return (float(self.z_min), float(self.z_max))

    @property
    def height_m(self) -> float | None:
        band = self.z_range
        return None if band is None else band[1] - band[0]

    @property
    def area_m2(self) -> float | None:
        """Plan area, or ``None`` when the object carries no usable footprint."""
        if not self.has_geometry:
            return None
        try:
            return calculate_area(record_to_polygon({"geometry_3d": self.geometry}))
        except (KeyError, TypeError, ValueError):
            return None


@dataclass(frozen=True)
class GeometryDelta:
    """Difference between two footprints.

    ``symmetric_difference_m2`` is the area that belongs to exactly one of the
    two outlines, and ``intersection_m2`` the area they share. Both are reported
    so a reviewer can see *which side* a boundary moved to, not just that
    something moved.
    """

    area_delta_m2: float
    previous_area_m2: float
    new_area_m2: float
    symmetric_difference_m2: float
    intersection_m2: float
    union_area_m2: float
    #: Intersection over union, in [0, 1]. 1.0 means the outlines coincide.
    agreement: float

    @property
    def expanded(self) -> bool:
        """True when the new outline encloses more plan area than the old one."""
        return self.area_delta_m2 > 0


@dataclass(frozen=True)
class ChangeRecord:
    """One detected difference between the approved cadastre and a survey.

    The shape is stable and complete whether the difference is a footprint, a
    height, a volume or a storey appearing or disappearing, so a consumer can
    read every field on every record without branching.
    """

    change_type: str
    object_id: str
    previous_geometry: dict[str, Any] | None
    new_geometry: dict[str, Any] | None
    #: The symmetric difference -- the plan area belonging to exactly one of the
    #: two outlines. Retained so a change can be **drawn** later, not merely
    #: described: highlighting a change needs the changed region, and
    #: recomputing it on every read would put a different answer on screen than
    #: the one that was recorded. ``None`` when a side has no footprint -- a
    #: storey that does not exist has no outline, and an empty polygon would read
    #: as "it vanished" rather than "it was never there".
    difference_geometry: dict[str, Any] | None
    previous_z_range: tuple[float, float] | None
    new_z_range: tuple[float, float] | None
    area_delta: float
    height_delta: float
    volume_delta: float
    geometric_quality: float
    source_id: str
    detected_at: str
    status: str
    object_type: str = "PROPERTY_VOLUME"
    previous_area_m2: float | None = None
    new_area_m2: float | None = None
    previous_height_m: float | None = None
    new_height_m: float | None = None
    previous_volume_m3: float | None = None
    new_volume_m3: float | None = None
    floor_number: int | None = None
    building_id: str | None = None
    description: str = CHANGE_REQUIRES_VERIFICATION
    tolerance: float = DEFAULT_TOLERANCE
    id: str | None = None
    #: Set when this change also produced a validation finding.
    finding_id: str | None = None

    @property
    def requires_verification(self) -> bool:
        return self.status == ChangeStatus.REQUIRES_VERIFICATION.value

    def to_record(self) -> dict[str, Any]:
        """Flatten into a storable ``cadastral_changes`` record."""
        return {
            "id": self.id,
            "change_type": self.change_type,
            "object_id": self.object_id,
            "object_type": self.object_type,
            "previous_geometry": self.previous_geometry,
            "new_geometry": self.new_geometry,
            "difference_geometry": self.difference_geometry,
            "previous_z_min": self.previous_z_range[0] if self.previous_z_range else None,
            "previous_z_max": self.previous_z_range[1] if self.previous_z_range else None,
            "new_z_min": self.new_z_range[0] if self.new_z_range else None,
            "new_z_max": self.new_z_range[1] if self.new_z_range else None,
            "area_delta": self.area_delta,
            "height_delta": self.height_delta,
            "volume_delta": self.volume_delta,
            "previous_area_m2": self.previous_area_m2,
            "new_area_m2": self.new_area_m2,
            "previous_height_m": self.previous_height_m,
            "new_height_m": self.new_height_m,
            "previous_volume_m3": self.previous_volume_m3,
            "new_volume_m3": self.new_volume_m3,
            "geometric_quality": self.geometric_quality,
            "source_id": self.source_id,
            "detected_at": self.detected_at,
            "status": self.status,
            "floor_number": self.floor_number,
            "building_id": self.building_id,
            "description": self.description,
            "tolerance": self.tolerance,
            "finding_id": self.finding_id,
        }


@dataclass
class ChangeReport:
    """The result of comparing two sides of a survey.

    ``changes`` holds one entry per detected difference, and an empty list is the
    honest answer for a re-survey that found nothing -- which is a real outcome,
    not a failure.
    """

    source_id: str
    compared_objects: int
    changes: list[ChangeRecord] = field(default_factory=list)
    #: Ids that were compared and showed no difference above tolerance.
    unchanged: list[str] = field(default_factory=list)
    actor: str | None = None
    tolerance: float = DEFAULT_TOLERANCE
    generated_at: str = field(default_factory=now)
    findings_created: int = 0
    review_cases_created: int = 0

    @property
    def has_changes(self) -> bool:
        return bool(self.changes)

    @property
    def by_type(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for change in self.changes:
            counts[change.change_type] = counts.get(change.change_type, 0) + 1
        return counts

    def summary(self) -> dict[str, Any]:
        """Counts by change type, plus what was compared and left alone."""
        return {
            "source_id": self.source_id,
            "compared_objects": self.compared_objects,
            "unchanged_objects": len(self.unchanged),
            "total_changes": len(self.changes),
            "by_type": self.by_type,
            "status": (
                ChangeStatus.REQUIRES_VERIFICATION.value
                if self.has_changes
                else "NO_CHANGE"
            ),
            "message": (
                CHANGE_REQUIRES_VERIFICATION
                if self.has_changes
                else "No change detected against the approved cadastre."
            ),
            "tolerance": self.tolerance,
            "findings_created": self.findings_created,
            "review_cases_created": self.review_cases_created,
            "generated_at": self.generated_at,
        }


# --------------------------------------------------------------------------
# building inputs
# --------------------------------------------------------------------------


def survey_geometry(record: Mapping[str, Any]) -> SurveyGeometry:
    """Build a :class:`SurveyGeometry` from a repository-style record.

    Accepts any of the geometry field names the system uses, so the same helper
    serves a property volume, a building and a parcel without the caller having
    to say which.
    """
    object_id = str(record.get("id") or record.get("object_id") or "")
    geometry = None
    for name in GEOMETRY_FIELDS:
        candidate = record.get(name)
        if isinstance(candidate, Mapping) and "coordinates" in candidate:
            geometry = dict(candidate)
            break
    z_min = record.get("z_min")
    z_max = record.get("z_max")
    return SurveyGeometry(
        object_id=object_id,
        geometry=geometry,
        z_min=float(z_min) if z_min is not None else None,
        z_max=float(z_max) if z_max is not None else None,
        floor_number=(
            int(record["floor_number"]) if record.get("floor_number") is not None else None
        ),
        building_id=record.get("building_id"),
        object_type=_object_type_for(record),
    )


def _object_type_for(record: Mapping[str, Any]) -> str:
    if "floor_number" in record and "unit_label" in record:
        return "PROPERTY_VOLUME"
    if "parcel_id" in record and "floor_count" in record:
        return "BUILDING"
    if "parcel_id" in record and "geometry" in record:
        return "PARCEL"
    if "type" in record:
        return "INFRASTRUCTURE"
    return "PROPERTY_VOLUME"


def _index(
    side: Mapping[str, SurveyGeometry] | Iterable[SurveyGeometry] | Iterable[Mapping[str, Any]],
    label: str,
) -> dict[str, SurveyGeometry]:
    """Accept either a mapping keyed by id or an iterable of records."""
    if isinstance(side, Mapping):
        return {
            str(key): value if isinstance(value, SurveyGeometry) else survey_geometry(value)
            for key, value in side.items()
        }
    out: dict[str, SurveyGeometry] = {}
    for item in side:
        geometry = item if isinstance(item, SurveyGeometry) else survey_geometry(item)
        if not geometry.object_id:
            raise ChangeDetectionError(
                f"a {label} record has no id and cannot be compared"
            )
        out[geometry.object_id] = geometry
    return out


# --------------------------------------------------------------------------
# deltas
# --------------------------------------------------------------------------


def calculate_geometry_change(
    previous: SurveyGeometry, new: SurveyGeometry
) -> GeometryDelta:
    """Measure how two footprints differ.

    Returns a zero delta when either side has no usable footprint, rather than
    guessing one. A missing outline is not a zero-area outline, and treating it
    as one would report a whole building as having shrunk to nothing.
    """
    if not previous.has_geometry or not new.has_geometry:
        previous_area = previous.area_m2 or 0.0
        new_area = new.area_m2 or 0.0
        return GeometryDelta(
            area_delta_m2=new_area - previous_area,
            previous_area_m2=previous_area,
            new_area_m2=new_area,
            symmetric_difference_m2=0.0,
            intersection_m2=0.0,
            union_area_m2=max(previous_area, new_area),
            agreement=0.0,
        )

    before = record_to_polygon({"geometry_3d": previous.geometry})
    after = record_to_polygon({"geometry_3d": new.geometry})
    previous_area = float(before.area)
    new_area = float(after.area)
    intersection = float(before.intersection(after).area)
    union = float(before.union(after).area)
    symmetric = float(before.symmetric_difference(after).area)
    return GeometryDelta(
        area_delta_m2=new_area - previous_area,
        previous_area_m2=previous_area,
        new_area_m2=new_area,
        symmetric_difference_m2=symmetric,
        intersection_m2=intersection,
        union_area_m2=union,
        #: IoU, guarded against a zero union: two empty outlines agree
        #: trivially and a 0/0 must not raise.
        agreement=(intersection / union) if union > 0 else 1.0,
    )


def calculate_height_delta(previous: SurveyGeometry, new: SurveyGeometry) -> float:
    """Change in vertical extent, in metres. ``0.0`` when either side has no Z range."""
    before = previous.height_m
    after = new.height_m
    if before is None or after is None:
        return 0.0
    return after - before


def calculate_volume_delta(previous: SurveyGeometry, new: SurveyGeometry) -> float:
    """Change in enclosed volume, in m³. ``0.0`` when it cannot be computed.

    Computed from the geometry and Z range rather than from any cached
    ``volume_m3`` field, so a stale cached value cannot masquerade as a change.
    """
    before = _volume_of(previous)
    after = _volume_of(new)
    if before is None or after is None:
        return 0.0
    return after - before


def _volume_of(side: SurveyGeometry) -> float | None:
    if not side.has_geometry or not side.has_z_range:
        return None
    try:
        return float(
            calculate_volume(
                record_to_polygon({"geometry_3d": side.geometry}),
                float(side.z_min),
                float(side.z_max),
            )
        )
    except (KeyError, TypeError, ValueError):
        return None


def _difference_geojson(previous: SurveyGeometry, new: SurveyGeometry) -> dict[str, Any] | None:
    """GeoJSON of the plan area that belongs to exactly one of the two outlines.

    This is the region a reviewer actually wants highlighted: the sliver a boundary
    moved into, or the strip a demolition took away. Returning the two outlines
    and leaving the reader to subtract them would put a boolean polygon operation
    in the UI, where there is no geometry library to do it honestly.

    ``None`` when either side has no usable footprint. A storey that does not
    exist has no outline, and an empty polygon would be read as "it shrank to
    nothing" rather than "it is not there".
    """
    if not previous.has_geometry or not new.has_geometry:
        return None
    before = record_to_polygon({"geometry_3d": previous.geometry})
    after = record_to_polygon({"geometry_3d": new.geometry})
    difference = before.symmetric_difference(after)
    if difference.is_empty or difference.area <= 0:
        return None
    return shape_to_geojson(difference)


def _relative_agreement(before: float | None, after: float | None) -> float:
    """Agreement in [0, 1] for a scalar pair, from ``1 - |d| / max(|a|, |b|)``."""
    if before is None or after is None:
        return 0.0
    scale = max(abs(before), abs(after))
    if scale <= 0:
        return 1.0
    return max(0.0, 1.0 - abs(after - before) / scale)


# --------------------------------------------------------------------------
# detectors
# --------------------------------------------------------------------------


def _record(
    change_type: ChangeType,
    previous: SurveyGeometry | None,
    new: SurveyGeometry | None,
    *,
    source_id: str,
    detected_at: str,
    tolerance: float,
    area_delta: float = 0.0,
    height_delta: float = 0.0,
    volume_delta: float = 0.0,
    geometric_quality: float = 0.0,
    previous_area: float | None = None,
    new_area: float | None = None,
    previous_height: float | None = None,
    new_height: float | None = None,
    previous_volume: float | None = None,
    new_volume: float | None = None,
    object_id: str | None = None,
    description: str = CHANGE_REQUIRES_VERIFICATION,
    difference_geometry: dict[str, Any] | None = None,
) -> ChangeRecord:
    subject = new or previous
    assert subject is not None  # a change always has at least one side
    return ChangeRecord(
        id=f"CHG-{uuid.uuid4().hex[:12]}",
        change_type=change_type.value,
        object_id=object_id or subject.object_id,
        object_type=subject.object_type,
        previous_geometry=previous.geometry if previous else None,
        new_geometry=new.geometry if new else None,
        difference_geometry=difference_geometry,
        previous_z_range=previous.z_range if previous else None,
        new_z_range=new.z_range if new else None,
        area_delta=area_delta,
        height_delta=height_delta,
        volume_delta=volume_delta,
        previous_area_m2=previous_area,
        new_area_m2=new_area,
        previous_height_m=previous_height,
        new_height_m=new_height,
        previous_volume_m3=previous_volume,
        new_volume_m3=new_volume,
        geometric_quality=geometric_quality,
        source_id=source_id,
        detected_at=detected_at,
        status=ChangeStatus.REQUIRES_VERIFICATION.value,
        floor_number=subject.floor_number,
        building_id=subject.building_id,
        description=description,
        tolerance=tolerance,
    )


def detect_footprint_change(
    previous: SurveyGeometry, new: SurveyGeometry, *, source_id: str, tolerance: float = DEFAULT_TOLERANCE, detected_at: str | None = None
) -> ChangeRecord | None:
    """Report a plan-footprint difference, or ``None`` when the outlines agree.

    Uses the symmetric difference rather than the area delta alone: two outlines
    of identical area in different places produce a zero area delta and a large
    symmetric difference, and only the latter reveals that the boundary moved.
    """
    delta = calculate_geometry_change(previous, new)
    if delta.symmetric_difference_m2 <= tolerance and abs(delta.area_delta_m2) <= tolerance:
        return None
    direction = "expanded" if delta.expanded else ("contracted" if delta.area_delta_m2 else "moved")
    return _record(
        ChangeType.FOOTPRINT,
        previous,
        new,
        source_id=source_id,
        detected_at=detected_at or now(),
        tolerance=tolerance,
        area_delta=delta.area_delta_m2,
        geometric_quality=delta.agreement,
        previous_area=delta.previous_area_m2,
        new_area=delta.new_area_m2,
        difference_geometry=_difference_geojson(previous, new),
        description=(
            f"Survey footprint {direction} by {abs(delta.area_delta_m2):.1f} m² "
            f"({delta.symmetric_difference_m2:.1f} m² of boundary differs). "
            f"{CHANGE_REQUIRES_VERIFICATION}"
        ),
    )


def detect_height_change(
    previous: SurveyGeometry, new: SurveyGeometry, *, source_id: str, tolerance: float = DEFAULT_TOLERANCE, detected_at: str | None = None
) -> ChangeRecord | None:
    """Report a vertical-extent difference, or ``None`` when the extents agree."""
    delta = calculate_height_delta(previous, new)
    if abs(delta) <= tolerance:
        return None
    return _record(
        ChangeType.HEIGHT,
        previous,
        new,
        source_id=source_id,
        detected_at=detected_at or now(),
        tolerance=tolerance,
        height_delta=delta,
        geometric_quality=_relative_agreement(previous.height_m, new.height_m),
        previous_height=previous.height_m,
        new_height=new.height_m,
        description=(
            f"Survey height differs by {delta:+.2f} m "
            f"({previous.height_m:.2f} m → {new.height_m:.2f} m). "
            f"{CHANGE_REQUIRES_VERIFICATION}"
        ),
    )


def detect_volume_change(
    previous: SurveyGeometry, new: SurveyGeometry, *, source_id: str, tolerance: float = DEFAULT_TOLERANCE, detected_at: str | None = None
) -> ChangeRecord | None:
    """Report an enclosed-volume difference, or ``None`` when they agree."""
    delta = calculate_volume_delta(previous, new)
    if abs(delta) <= tolerance:
        return None
    before = _volume_of(previous)
    after = _volume_of(new)
    return _record(
        ChangeType.VOLUME,
        previous,
        new,
        source_id=source_id,
        detected_at=detected_at or now(),
        tolerance=tolerance,
        volume_delta=delta,
        geometric_quality=_relative_agreement(before, after),
        previous_volume=before,
        new_volume=after,
        description=(
            f"Survey volume differs by {delta:+.1f} m³ "
            f"({before:.1f} m³ → {after:.1f} m³). "
            f"{CHANGE_REQUIRES_VERIFICATION}"
        ),
    )


def _storeys(
    side: Mapping[str, SurveyGeometry],
) -> dict[tuple[str | None, int], list[SurveyGeometry]]:
    """Group objects into storeys by ``(building_id, floor_number)``."""
    grouped: dict[tuple[str | None, int], list[SurveyGeometry]] = {}
    for geometry in side.values():
        if geometry.floor_number is None:
            continue
        grouped.setdefault((geometry.building_id, geometry.floor_number), []).append(
            geometry
        )
    return grouped


def detect_new_floor(
    approved: Mapping[str, SurveyGeometry],
    survey: Mapping[str, SurveyGeometry],
    *,
    source_id: str,
    tolerance: float = DEFAULT_TOLERANCE,
    detected_at: str | None = None,
) -> list[ChangeRecord]:
    """Storeys the survey has that the approved cadastre does not.

    Keyed on ``(building_id, floor_number)`` rather than on object ids, because a
    re-survey legitimately renumbers the units within a storey. What matters is
    that a *storey* appeared, not that a particular unit id is new.
    """
    approved_storeys = _storeys(approved)
    survey_storeys = _storeys(survey)
    stamp = detected_at or now()
    out: list[ChangeRecord] = []
    for key, members in survey_storeys.items():
        if key in approved_storeys:
            continue
        building_id, floor_number = key
        subject = members[0]
        out.append(
            _record(
                ChangeType.NEW_FLOOR,
                None,
                subject,
                source_id=source_id,
                detected_at=stamp,
                tolerance=tolerance,
                geometric_quality=0.0,
                object_id=subject.object_id,
                description=(
                    f"Survey contains storey {floor_number}"
                    + (f" of building {building_id}" if building_id else "")
                    + f", which the approved cadastre does not have. "
                    f"{CHANGE_REQUIRES_VERIFICATION}"
                ),
            )
        )
    return out


def detect_removed_floor(
    approved: Mapping[str, SurveyGeometry],
    survey: Mapping[str, SurveyGeometry],
    *,
    source_id: str,
    tolerance: float = DEFAULT_TOLERANCE,
    detected_at: str | None = None,
) -> list[ChangeRecord]:
    """Storeys the approved cadastre has that the new survey does not cover.

    Deliberately worded as *not covered by this survey* rather than *removed*:
    absence from one survey is not evidence of demolition. The record is still
    typed ``REMOVED_FLOOR`` because that is the comparison that was made, and the
    description plus the verification status keep the claim to what is known.
    """
    approved_storeys = _storeys(approved)
    survey_storeys = _storeys(survey)
    stamp = detected_at or now()
    out: list[ChangeRecord] = []
    for key, members in approved_storeys.items():
        if key in survey_storeys:
            continue
        building_id, floor_number = key
        subject = members[0]
        out.append(
            _record(
                ChangeType.REMOVED_FLOOR,
                subject,
                None,
                source_id=source_id,
                detected_at=stamp,
                tolerance=tolerance,
                geometric_quality=0.0,
                object_id=subject.object_id,
                description=(
                    f"Storey {floor_number}"
                    + (f" of building {building_id}" if building_id else "")
                    + " is in the approved cadastre but is not covered by this "
                    "survey. This may mean it is outside the survey extent, not "
                    f"that it no longer exists. {CHANGE_REQUIRES_VERIFICATION}"
                ),
            )
        )
    return out


# --------------------------------------------------------------------------
# findings
# --------------------------------------------------------------------------


#: Change type -> the issue type its finding is filed under. Every change gets a
#: finding, so that "requires verification" always has something in the review
#: queue to refer to; a newly-appeared storey gets the specific
#: ``UNREGISTERED_FLOOR`` type the specification calls for.
FINDING_ISSUE_TYPE: dict[str, str] = {
    ChangeType.NEW_FLOOR.value: IssueType.UNREGISTERED_FLOOR.value,
    ChangeType.REMOVED_FLOOR.value: IssueType.REMOVED_FLOOR.value,
    ChangeType.FOOTPRINT.value: IssueType.FOOTPRINT_CHANGE.value,
    ChangeType.HEIGHT.value: IssueType.HEIGHT_CHANGE.value,
    ChangeType.VOLUME.value: IssueType.VOLUME_CHANGE.value,
}

#: Rule id recorded on a finding filed for a detected change.
CHANGE_RULE_PREFIX = "CHANGE-DETECTED"


def _finding_for(change: ChangeRecord) -> dict[str, Any]:
    """Build the finding a detected change is filed under.

    Shaped like a validation finding so it lives in the same ``issues`` collection
    and is readable by the same consumers, but filed by change detection rather
    than by the rule engine: telling a changed object from an unchanged one needs
    a baseline, and the rule engine deliberately evaluates the current scene alone.

    A newly-appeared storey is filed as the specific ``UNREGISTERED_FLOOR`` the
    specification requires, because "this storey is not in the register" is a
    materially different statement from "this storey's outline moved".
    """
    issue_type = FINDING_ISSUE_TYPE[change.change_type]
    is_new_floor = change.change_type == ChangeType.NEW_FLOOR.value
    return {
        "id": f"CHGF-{uuid.uuid4().hex[:10]}",
        "object_a": change.object_id,
        "object_b": change.building_id,
        "issue_type": issue_type,
        # WARNING, never CRITICAL: this is an observation needing a human, not an
        # assertion that a defect exists. The comparison establishes no fault.
        "severity": IssueSeverity.WARNING.value,
        "overlap_volume": 0.0,
        "gap_m": None,
        "description": change.description,
        "geometry": change.new_geometry,
        "status": "OPEN",
        "location": (
            f"Storey {change.floor_number}"
            + (f" of building {change.building_id}" if change.building_id else "")
            if is_new_floor
            else (change.object_id or "")
        ),
        "rule_id": UNREGISTERED_FLOOR_RULE if is_new_floor else CHANGE_RULE_PREFIX,
        "category": "CHANGE",
        "evidence": {
            "change_type": change.change_type,
            "floor_number": change.floor_number,
            "building_id": change.building_id,
            "source_id": change.source_id,
            "change_id": change.id,
            "geometric_quality": change.geometric_quality,
            "area_delta": change.area_delta,
            "height_delta": change.height_delta,
            "volume_delta": change.volume_delta,
        },
    }


# --------------------------------------------------------------------------
# the comparison
# --------------------------------------------------------------------------


def compare_approved_vs_survey(
    approved: Mapping[str, SurveyGeometry] | Iterable[SurveyGeometry] | Iterable[Mapping[str, Any]],
    survey: Mapping[str, SurveyGeometry] | Iterable[SurveyGeometry] | Iterable[Mapping[str, Any]],
    *,
    source_id: str,
    repository: CadastreRepository | None = None,
    actor: str | None = None,
    tolerance: float = DEFAULT_TOLERANCE,
    detected_at: str | None = None,
) -> ChangeReport:
    """Compare the approved cadastre against a new survey.

    ``approved`` and ``survey`` may each be a mapping keyed by object id, an
    iterable of :class:`SurveyGeometry`, or an iterable of repository-style
    records. A **re-sururvey supersedes rather than replaces** the approved
    geometry: the cadastre is not edited here, so a reader can still see what was
    approved after a change is found.

    Only objects present on **both** sides are compared for footprint, height and
    volume. A survey that renumbers its objects is a data problem, not a
    cadastral change, and reporting every such object as "new" would bury the
    real findings.

    With a ``repository``, each change is persisted, filed as a finding where one
    applies, and opened as a review case so a human can resolve it. Without one,
    the report is returned and nothing is written -- which is what makes this
    function straightforward to test in isolation.
    """
    if not source_id:
        raise ChangeDetectionError("a source_id is required to attribute a survey")
    left = _index(approved, "approved")
    right = _index(survey, "survey")
    stamp = detected_at or now()

    changes: list[ChangeRecord] = []
    unchanged: list[str] = []

    for object_id in sorted(set(left) & set(right)):
        before, after = left[object_id], right[object_id]
        found = [
            change
            for change in (
                detect_footprint_change(
                    before, after, source_id=source_id, tolerance=tolerance, detected_at=stamp
                ),
                detect_height_change(
                    before, after, source_id=source_id, tolerance=tolerance, detected_at=stamp
                ),
                detect_volume_change(
                    before, after, source_id=source_id, tolerance=tolerance, detected_at=stamp
                ),
            )
            if change is not None
        ]
        if found:
            changes.extend(found)
        else:
            unchanged.append(object_id)

    changes.extend(
        detect_new_floor(left, right, source_id=source_id, tolerance=tolerance, detected_at=stamp)
    )
    changes.extend(
        detect_removed_floor(left, right, source_id=source_id, tolerance=tolerance, detected_at=stamp)
    )

    report = ChangeReport(
        source_id=source_id,
        compared_objects=len(set(left) & set(right)),
        changes=changes,
        unchanged=unchanged,
        actor=actor,
        tolerance=tolerance,
        generated_at=stamp,
    )
    if repository is not None:
        _persist(report, repository)
    return report


def _persist(report: ChangeReport, repository: CadastreRepository) -> None:
    """Store the report's changes, file findings, and open review cases.

    Every change becomes a stored record, a finding, an audit event, and -- so
    that "requires verification" is actionable rather than decorative -- a review
    case a person can work through. New storeys are filed under the specific
    ``UNREGISTERED_FLOOR`` type.
    """
    from app.services.reviews import create_review_case

    persisted: list[ChangeRecord] = []
    for change in report.changes:
        finding = _finding_for(change)
        repository.add("issues", finding)
        report.findings_created += 1
        # ``ChangeRecord`` is frozen, so the linked finding is attached with a
        # replacement rather than an assignment. The replacement is written back
        # into the report so a caller sees the same id that was stored.
        change = replace(change, finding_id=finding["id"])
        persisted.append(change)
        repository.add("cadastral_changes", change.to_record())
        create_audit_event(
            repository,
            action=AuditAction.CHANGE_DETECTED.value,
            object_id=change.object_id,
            object_type=change.object_type,
            actor=report.actor,
            detail=change.description,
            previous_state=(
                f"area {change.previous_area_m2:.1f} m²"
                if change.previous_area_m2 is not None
                else "no geometry"
            ),
            new_state=(
                f"area {change.new_area_m2:.1f} m²"
                if change.new_area_m2 is not None
                else "no geometry"
            ),
            extra={
                "change_id": change.id,
                "change_type": change.change_type,
                "source_id": change.source_id,
                "area_delta": change.area_delta,
                "height_delta": change.height_delta,
                "volume_delta": change.volume_delta,
                "geometric_quality": change.geometric_quality,
                "finding_id": change.finding_id,
            },
        )
        create_review_case(
            repository,
            issue_id=change.finding_id,
            reason=change.description,
            actor=report.actor,
        )
        report.review_cases_created += 1
    report.changes = persisted


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------


def generate_change_report(
    repository: CadastreRepository, *, source_id: str | None = None, limit: int = 500
) -> dict[str, Any]:
    """Summarise the changes stored for a survey.

    Reads what was recorded rather than recomputing, so the report describes
    comparisons that actually happened. Passing no ``source_id`` covers every
    survey on record.
    """
    changes = [
        change
        for change in repository.records("cadastral_changes")
        if source_id is None or change.get("source_id") == source_id
    ]
    ordered = sorted(
        changes, key=lambda c: (str(c.get("detected_at") or ""), str(c.get("id") or ""))
    )
    by_type: dict[str, int] = {}
    for change in ordered:
        key = str(change.get("change_type"))
        by_type[key] = by_type.get(key, 0) + 1
    return {
        "source_id": source_id,
        "total_changes": len(ordered),
        "by_type": by_type,
        "objects_affected": len({c.get("object_id") for c in ordered}),
        "status": (
            ChangeStatus.REQUIRES_VERIFICATION.value
            if ordered
            else "NO_CHANGE"
        ),
        "all_require_verification": all(
            c.get("status") == ChangeStatus.REQUIRES_VERIFICATION.value
            for c in ordered
        ),
        "message": (
            CHANGE_REQUIRES_VERIFICATION
            if ordered
            else "No change detected against the approved cadastre."
        ),
        "changes": ordered[: max(1, limit)],
        "generated_at": now(),
    }


__all__ = [
    "CHANGE_REQUIRES_VERIFICATION",
    "DEFAULT_TOLERANCE",
    "UNREGISTERED_FLOOR_RULE",
    "ChangeDetectionError",
    "ChangeRecord",
    "ChangeReport",
    "GeometryDelta",
    "SurveyGeometry",
    "calculate_geometry_change",
    "calculate_height_delta",
    "calculate_volume_delta",
    "compare_approved_vs_survey",
    "detect_footprint_change",
    "detect_height_change",
    "detect_new_floor",
    "detect_removed_floor",
    "detect_volume_change",
    "generate_change_report",
    "survey_geometry",
]
