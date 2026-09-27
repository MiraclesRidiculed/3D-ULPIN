"""Rule-based validation engine.

Thirty independent rules across six categories, each declared once and runnable
on its own. This module replaces the monolithic rule selection in
:mod:`app.services.validation`; **the observable behaviour of ``validate()`` is
unchanged**, including the two pre-existing crashes that the test suite pins.

Three things are worth knowing before changing anything here.

Order is behaviour
------------------
Rules run in registration order, and that order is load-bearing. The original
implementation appended an ``INVALID_GEOMETRY`` finding and then *continued*
into the containment check on the same invalid geometry, which makes GEOS raise.
Reproducing that required keeping validity ahead of containment. Likewise the
catch-all infrastructure rule runs after the typed ones so its finding text and
identifier stay exactly as they were.

Two crashes are preserved deliberately
---------------------------------------
``GEOM-INVALID`` is followed by ``PARCEL-PROPERTY-OUTSIDE``, which operates on the
very geometry just found invalid, so a bow-tie polygon still raises
``GEOSException`` after recording the finding. And ``CAD-DUPLICATE-ULPIN``
serialises an intersection that is a ``LineString`` when two footprints merely
touch, still raising ``AttributeError``. Both are asserted by
``tests/test_services.py``. Do not "fix" them without also changing those tests
and saying why.

Evidence, not adjectives
------------------------
Every finding carries a ``evidence`` dict of the numbers that produced it, plus
the legacy ``overlap_volume``/``gap_m`` fields so the stored record contract is
unchanged. A finding that cannot state its evidence is a finding that should not
be filed.

The demo scene reports exactly three findings
---------------------------------------------
The twenty-two rules added here must stay silent on the seeded scene, which is a
deliberate showcase. Any rule that fires on it is a rule with a bug.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.models.enums import (
    InfrastructureType,
    IssueSeverity,
    IssueType,
    RuleCategory,
)
from app.repositories.base import CadastreRepository
from app.services.geometry import (
    calculate_area,
    calculate_footprint_difference,
    calculate_horizontal_intersection,
    calculate_outside_area,
    calculate_volume,
    calculate_volume_intersection,
    polygon_to_geojson,
    record_to_polygon,
    repair_polygon,
    union_footprints,
    validate_polygon,
    validate_z_range,
)
from app.utils import now

if TYPE_CHECKING:  # pragma: no cover - typing only
    from shapely.geometry.base import BaseGeometry

# --------------------------------------------------------------------------
# Thresholds. Deliberately identical to the originals.
# --------------------------------------------------------------------------

#: Minimum plan area / overlap volume (m² / m³) for a finding to be reported.
AREA_EPSILON = 0.01

#: Minimum vertical gap (m) between bands expected to be adjacent floors.
GAP_EPSILON = 0.05

#: Storey heights may differ from the building's median by this fraction before
#: ``VERT-FLOOR-HEIGHT`` reports. Wider than the gap epsilon because a real
#: building legitimately has a taller ground or top storey.
FLOOR_HEIGHT_TOLERANCE = 0.30

#: Overlap detection is restricted to volumes sharing a floor band. Genuine
#: inter-floor 3D overlaps are therefore NOT detected; a known limitation,
#: preserved from the original implementation.
SAME_FLOOR_ONLY = True

#: Demo identifiers referenced in finding text. Hardcoded, as in the original.
DEMO_PARCEL_BUSINESS_ID = "P-001"
DEMO_BUILDING_BUSINESS_ID = "B-001"

STATUS_OPEN = "OPEN"

#: Infrastructure types that get their own dedicated rule. The catch-all rule
#: covers the utility corridor; these are additional, more specific findings.
TYPED_INFRASTRUCTURE = {
    "TUNNEL": IssueType.TUNNEL_COLLISION,
    "BASEMENT": IssueType.BASEMENT_COLLISION,
    "FOUNDATION": IssueType.FOUNDATION_COLLISION,
    "ELEVATED_STRUCTURE": IssueType.ELEVATED_STRUCTURE_COLLISION,
}


# --------------------------------------------------------------------------
# Result
# --------------------------------------------------------------------------


@dataclass
class ValidationResult:
    """One finding.

    Carries the numbers that produced it in ``evidence``, so a reviewer can see
    *why* something was flagged rather than being asked to trust a severity.
    """

    rule_id: str
    issue_type: str
    severity: str
    category: str
    object_a: str
    description: str
    object_b: str | None = None
    geometry: "BaseGeometry | None" = None
    evidence: dict[str, Any] = field(default_factory=dict)
    status: str = STATUS_OPEN
    location: str = ""
    ident: str | None = None

    @property
    def id(self) -> str:
        return self.ident or self.rule_id

    def to_issue_record(self) -> dict[str, Any]:
        """Flatten into the stored ``issues`` record shape.

        The eight original fields are unchanged, so existing consumers and the
        demo's pinned findings are unaffected. ``rule_id``, ``category`` and
        ``evidence`` are additive.
        """
        return {
            "id": self.id,
            "object_a": self.object_a,
            "object_b": self.object_b,
            "issue_type": self.issue_type,
            "severity": self.severity,
            "overlap_volume": round(float(self.evidence.get("overlap_volume_m3", 0)), 2),
            "gap_m": self.evidence.get("gap_m"),
            "description": self.description,
            "geometry": (
                polygon_to_geojson(self.geometry)
                if self.geometry is not None and not self.geometry.is_empty
                else None
            ),
            "status": self.status,
            "location": self.location,
            "rule_id": self.rule_id,
            "category": self.category,
            "evidence": self.evidence,
        }


# --------------------------------------------------------------------------
# Rule
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationRule:
    """A single declarative rule.

    ``check`` receives a :class:`ValidationContext` and yields zero or more
    results. It should yield nothing rather than raise when it has nothing to
    report; raising is reserved for genuinely broken input, which the engine
    surfaces rather than hiding.
    """

    rule_id: str
    category: RuleCategory
    issue_type: str
    severity: str
    description: str
    check: Callable[["ValidationContext"], Iterator[ValidationResult]]
    #: Collections the rule reads. Documentation for the reader, and available
    #: to callers that want to explain a rule before running it.
    collections: tuple[str, ...] = ("properties",)

    def definition(self) -> dict[str, Any]:
        """Machine-readable description, for ``get_rule_definitions()``."""
        return {
            "rule_id": self.rule_id,
            "category": self.category.value,
            "issue_type": self.issue_type,
            "severity": self.severity,
            "description": self.description,
            "collections": list(self.collections),
        }


# --------------------------------------------------------------------------
# Context
# --------------------------------------------------------------------------


class ValidationContext:
    """Everything a rule needs, with the collections read once per run.

    ``primary_parcel()`` and ``primary_infrastructure()`` index ``[0]`` on
    purpose: the original implementation did, and on an empty store it raised
    ``IndexError``. Rules that need a real "find the parent" answer should use
    :meth:`parcel_containing` instead.
    """

    def __init__(
        self,
        repo: CadastreRepository,
        *,
        area_epsilon: float = AREA_EPSILON,
        gap_epsilon: float = GAP_EPSILON,
    ) -> None:
        self.repo = repo
        self.area_epsilon = area_epsilon
        self.gap_epsilon = gap_epsilon
        self._cache: dict[str, list[dict[str, Any]]] = {}

    def records(self, kind: str) -> list[dict[str, Any]]:
        if kind not in self._cache:
            self._cache[kind] = self.repo.records(kind)
        return self._cache[kind]

    @property
    def parcels(self) -> list[dict[str, Any]]:
        return self.records("parcels")

    @property
    def properties(self) -> list[dict[str, Any]]:
        return self.records("properties")

    @property
    def buildings(self) -> list[dict[str, Any]]:
        return self.records("buildings")

    @property
    def floors(self) -> list[dict[str, Any]]:
        return self.records("floors")

    @property
    def infrastructure(self) -> list[dict[str, Any]]:
        return self.records("infrastructure")

    @property
    def geometry_versions(self) -> list[dict[str, Any]]:
        return self.records("geometry_versions")

    def primary_parcel(self) -> dict[str, Any]:
        return self.parcels[0]

    def primary_infrastructure(self) -> dict[str, Any]:
        return self.infrastructure[0]

    def parcel_containing(self, footprint: "BaseGeometry") -> str | None:
        """Id of the parcel fully containing ``footprint``, else ``None``."""
        tolerance = max(1e-9, footprint.area * 1e-9)
        for parcel in self.parcels:
            geojson = parcel.get("geometry")
            if not isinstance(geojson, dict) or "coordinates" not in geojson:
                continue
            shape = record_to_polygon(parcel)
            if footprint.area > 0 and footprint.difference(shape).area <= tolerance:
                return str(parcel.get("parcel_id") or parcel.get("id"))
        return None

    def result(
        self,
        rule: ValidationRule,
        *,
        ident: str,
        object_a: str,
        description: str,
        object_b: str | None = None,
        geometry: "BaseGeometry | None" = None,
        evidence: dict[str, Any] | None = None,
        location: str = "",
        severity: str | None = None,
    ) -> ValidationResult:
        return ValidationResult(
            rule_id=rule.rule_id,
            issue_type=rule.issue_type,
            severity=severity or rule.severity,
            category=rule.category.value,
            object_a=object_a,
            object_b=object_b,
            description=description,
            geometry=geometry,
            evidence=evidence or {},
            location=location,
            ident=ident,
        )


# --------------------------------------------------------------------------
# GEOMETRY rules
# --------------------------------------------------------------------------


def _check_invalid_geometry(ctx: ValidationContext) -> Iterator[ValidationResult]:
    """INVALID_GEOMETRY (critical): the footprint is not a valid polygon."""
    for p in ctx.properties:
        shape = record_to_polygon(p)
        validity = validate_polygon(shape)
        if not validity.is_valid:
            yield ctx.result(
                _RULES["GEOM-INVALID"],
                ident=f"VAL-INV-{p['id']}",
                object_a=p["id"],
                description=validity.reason or "Invalid geometry",
                geometry=shape,
                evidence={"reason": validity.reason, "is_valid": False},
                location=p["unit_label"],
            )


def _check_self_intersection(ctx: ValidationContext) -> Iterator[ValidationResult]:
    """SELF_INTERSECTION (critical): the ring crosses itself."""
    for p in ctx.properties:
        shape = record_to_polygon(p)
        if not shape.is_valid and shape.is_simple is False:
            yield ctx.result(
                _RULES["GEOM-SELF-INTERSECTION"],
                ident=f"VAL-SELF-{p['id']}",
                object_a=p["id"],
                description=(
                    f"{p['unit_label']} has a self-intersecting footprint ring."
                ),
                geometry=shape,
                evidence={"is_simple": False},
                location=p["unit_label"],
            )


def _check_zero_area(ctx: ValidationContext) -> Iterator[ValidationResult]:
    """ZERO_AREA (warning): a footprint that encloses nothing."""
    for p in ctx.properties:
        shape = record_to_polygon(p)
        area = calculate_area(shape)
        if area <= ctx.area_epsilon:
            yield ctx.result(
                _RULES["GEOM-ZERO-AREA"],
                ident=f"VAL-ZAREA-{p['id']}",
                object_a=p["id"],
                description=(
                    f"{p['unit_label']} has a plan area of {area:.4f} m², which is "
                    "effectively zero."
                ),
                geometry=shape,
                evidence={"area_m2": round(area, 6)},
                location=p["unit_label"],
            )


def _check_zero_volume(ctx: ValidationContext) -> Iterator[ValidationResult]:
    """ZERO_VOLUME (critical): no enclosed 3D space."""
    for p in ctx.properties:
        shape = record_to_polygon(p)
        volume = calculate_volume(shape, p["z_min"], p["z_max"])
        if volume <= ctx.area_epsilon:
            yield ctx.result(
                _RULES["GEOM-ZERO-VOLUME"],
                ident=f"VAL-ZVOL-{p['id']}",
                object_a=p["id"],
                description=(
                    f"{p['unit_label']} encloses {volume:.4f} m³, which is "
                    "effectively zero."
                ),
                geometry=shape,
                evidence={"volume_m3": round(volume, 6)},
                location=p["unit_label"],
            )


def _check_invalid_z_range(ctx: ValidationContext) -> Iterator[ValidationResult]:
    """INVALID_Z_RANGE (critical): z_max is not above z_min."""
    for p in ctx.properties:
        if not validate_z_range(p["z_min"], p["z_max"]).is_valid:
            yield ctx.result(
                _RULES["GEOM-INVALID-Z-RANGE"],
                ident=f"VAL-ZRANGE-{p['id']}",
                object_a=p["id"],
                description=(
                    f"{p['unit_label']} has z_min {p['z_min']} and z_max "
                    f"{p['z_max']}, so it has no vertical extent."
                ),
                geometry=record_to_polygon(p),
                evidence={"z_min": p["z_min"], "z_max": p["z_max"]},
                location=p["unit_label"],
            )


# --------------------------------------------------------------------------
# PARCEL rules
# --------------------------------------------------------------------------


def _check_property_outside_parcel(
    ctx: ValidationContext,
) -> Iterator[ValidationResult]:
    """OUTSIDE_PARENT_PARCEL (warning): a property not contained by its parcel."""
    parent_poly = record_to_polygon(ctx.primary_parcel())
    for p in ctx.properties:
        shape = record_to_polygon(p)
        outside = calculate_footprint_difference(shape, parent_poly)
        outside_area = calculate_outside_area(shape, parent_poly)
        if outside_area > ctx.area_epsilon:
            yield ctx.result(
                _RULES["PARCEL-PROPERTY-OUTSIDE"],
                ident=f"VAL-OUT-{p['id']}",
                object_a=p["id"],
                object_b=DEMO_PARCEL_BUSINESS_ID,
                description=(
                    f"{p['unit_label']} extends {outside_area:.1f} m² "
                    f"outside parent parcel {DEMO_PARCEL_BUSINESS_ID}."
                ),
                geometry=outside,
                evidence={"outside_area_m2": round(outside_area, 2)},
                location=p["unit_label"],
            )


def _check_building_outside_parcel(ctx: ValidationContext) -> Iterator[ValidationResult]:
    """BUILDING_OUTSIDE_PARCEL (warning): a building not contained by a parcel."""
    for b in ctx.buildings:
        shape = record_to_polygon(b)
        if shape.is_empty or shape.area <= 0:
            continue
        parent_id = b.get("parcel_id")
        parent = next(
            (p for p in ctx.parcels if (p.get("parcel_id") or p.get("id")) == parent_id),
            None,
        )
        if parent is None:
            continue
        outside_area = calculate_outside_area(shape, record_to_polygon(parent))
        if outside_area > ctx.area_epsilon:
            yield ctx.result(
                _RULES["PARCEL-BUILDING-OUTSIDE"],
                ident=f"VAL-BOUT-{b['id']}",
                object_a=b["id"],
                object_b=str(parent_id),
                description=(
                    f"Building {b['id']} extends {outside_area:.1f} m² outside its "
                    f"parent parcel {parent_id}."
                ),
                geometry=calculate_footprint_difference(
                    shape, record_to_polygon(parent)
                ),
                evidence={"outside_area_m2": round(outside_area, 2)},
                location=b["id"],
            )


def _check_unassigned_building(ctx: ValidationContext) -> Iterator[ValidationResult]:
    """UNASSIGNED_BUILDING (warning): a building inside a parcel but not linked."""
    for b in ctx.buildings:
        if b.get("parcel_id"):
            continue
        shape = record_to_polygon(b)
        if shape.is_empty or shape.area <= 0:
            continue
        containing = ctx.parcel_containing(shape)
        if containing:
            yield ctx.result(
                _RULES["PARCEL-UNASSIGNED-BUILDING"],
                ident=f"VAL-BUNAS-{b['id']}",
                object_a=b["id"],
                object_b=containing,
                description=(
                    f"Building {b['id']} lies inside parcel {containing} but has no "
                    "parent parcel assigned."
                ),
                geometry=shape,
                evidence={"containing_parcel_id": containing},
                location=b["id"],
            )


def _check_unassigned_property(ctx: ValidationContext) -> Iterator[ValidationResult]:
    """UNASSIGNED_PROPERTY (warning): a property inside a parcel but not linked."""
    for p in ctx.properties:
        if p.get("parent_parcel_id"):
            continue
        shape = record_to_polygon(p)
        if shape.is_empty or shape.area <= 0:
            continue
        containing = ctx.parcel_containing(shape)
        if containing:
            yield ctx.result(
                _RULES["PARCEL-UNASSIGNED-PROPERTY"],
                ident=f"VAL-PUNAS-{p['id']}",
                object_a=p["id"],
                object_b=containing,
                description=(
                    f"{p['unit_label']} lies inside parcel {containing} but has no "
                    "parent parcel assigned."
                ),
                geometry=shape,
                evidence={"containing_parcel_id": containing},
                location=p["unit_label"],
            )


# --------------------------------------------------------------------------
# VERTICAL rules
# --------------------------------------------------------------------------


def _check_floor_overlap(ctx: ValidationContext) -> Iterator[ValidationResult]:
    """FLOOR_OVERLAP (critical): two storey plates occupy the same 3D space.

    Only storeys that actually carry plan geometry are tested. A floor record can
    legitimately be a vertical band with no footprint -- the demo's floor stack is
    exactly that -- and a band with no plan cannot overlap another in plan.
    """
    floors = [
        f
        for f in ctx.floors
        if f.get("z_min") is not None and _has_footprint(f)
    ]
    for index, a in enumerate(floors):
        for b in floors[index + 1 :]:
            shape_a, vol = calculate_volume_intersection(a, b)
            if vol > ctx.area_epsilon:
                yield ctx.result(
                    _RULES["VERT-FLOOR-OVERLAP"],
                    ident=f"VAL-FOVR-{a['floor_number']}-{b['floor_number']}",
                    object_a=a["id"],
                    object_b=b["id"],
                    description=(
                        f"Floors {a['floor_number']} and {b['floor_number']} occupy "
                        f"the same 3D space ({vol:.1f} m³)."
                    ),
                    geometry=shape_a,
                    evidence={"overlap_volume_m3": round(vol, 2)},
                    location=(
                        f"{DEMO_BUILDING_BUSINESS_ID} / Floor {a['floor_number']}"
                    ),
                )


def _has_footprint(record: dict[str, Any]) -> bool:
    """True when a record carries a GeoJSON polygon in a known geometry field."""
    for name in ("geometry_3d", "geometry", "footprint"):
        value = record.get(name)
        if isinstance(value, Mapping) and "coordinates" in value:
            return True
    return False


def _check_property_overlap(ctx: ValidationContext) -> Iterator[ValidationResult]:
    """VERTICAL_VOLUME_OVERLAP (critical), within a single floor band only."""
    props = ctx.properties
    for i, a in enumerate(props):
        for b in props[i + 1 :]:
            if SAME_FLOOR_ONLY and a["floor_number"] != b["floor_number"]:
                continue
            shape, vol = calculate_volume_intersection(a, b)
            if vol > ctx.area_epsilon:
                yield ctx.result(
                    _RULES["VERT-PROPERTY-OVERLAP"],
                    ident=f"VAL-OVR-{a['id']}-{b['id']}",
                    object_a=a["id"],
                    object_b=b["id"],
                    description=(
                        f"{a['unit_label']} and {b['unit_label']} occupy the same "
                        f"3D space ({vol:.1f} m³)."
                    ),
                    geometry=shape,
                    evidence={"overlap_volume_m3": round(vol, 2)},
                    location=(
                        f"{DEMO_BUILDING_BUSINESS_ID} / Floor {a['floor_number']}"
                    ),
                )


def _floor_bands(
    ctx: ValidationContext,
) -> list[tuple[int, float, float, "BaseGeometry"]]:
    """``(floor, z_min, z_max, plan union)`` per storey, ordered by elevation."""
    by_floor: dict[int, list[dict[str, Any]]] = {}
    for p in ctx.properties:
        if p["floor_number"] is not None:
            by_floor.setdefault(p["floor_number"], []).append(p)
    bands = [
        (
            n,
            min(x["z_min"] for x in ps),
            max(x["z_max"] for x in ps),
            union_footprints(record_to_polygon(x) for x in ps),
        )
        for n, ps in by_floor.items()
    ]
    return sorted(bands, key=lambda band: band[1])


def _check_floor_gap(ctx: ValidationContext) -> Iterator[ValidationResult]:
    """EXPECTED_ADJACENT_FLOOR_GAP (warning): a hole between expected floors."""
    bands = _floor_bands(ctx)
    for (n1, _, top, _), (n2, bottom, _, _) in zip(bands, bands[1:]):
        if bottom - top > ctx.gap_epsilon:
            yield ctx.result(
                _RULES["VERT-FLOOR-GAP"],
                ident=f"VAL-GAP-{n1}-{n2}",
                object_a=f"FLOOR-{n1}",
                object_b=f"FLOOR-{n2}",
                description=(
                    f"A {bottom - top:.2f} m vertical gap separates expected "
                    f"adjacent floors {n1} and {n2}."
                ),
                evidence={"gap_m": round(bottom - top, 2)},
                location=DEMO_BUILDING_BUSINESS_ID,
            )


def _check_disconnected_stack(ctx: ValidationContext) -> Iterator[ValidationResult]:
    """DISCONNECTED_FLOOR_STACK (warning): no plan connection between floors."""
    bands = _floor_bands(ctx)
    for (n1, _, _, shape1), (n2, _, _, shape2) in zip(bands, bands[1:]):
        shared = calculate_area(calculate_horizontal_intersection(shape1, shape2))
        if shared < ctx.area_epsilon:
            yield ctx.result(
                _RULES["VERT-DISCONNECTED-STACK"],
                ident=f"VAL-DISC-{n1}-{n2}",
                object_a=f"FLOOR-{n1}",
                object_b=f"FLOOR-{n2}",
                description=f"Floor {n2} has no plan-area connection to floor {n1}.",
                evidence={"shared_area_m2": round(shared, 4)},
                location=DEMO_BUILDING_BUSINESS_ID,
            )


def _check_z_order(ctx: ValidationContext) -> Iterator[ValidationResult]:
    """FLOOR_Z_ORDER_INCONSISTENCY (critical): z_min at or above z_max."""
    for p in ctx.properties:
        if not validate_z_range(p["z_min"], p["z_max"]).is_valid:
            yield ctx.result(
                _RULES["VERT-Z-ORDER"],
                ident=f"VAL-Z-{p['id']}",
                object_a=p["id"],
                description=(
                    f"{p['unit_label']} has z_min greater than or equal to z_max."
                ),
                geometry=record_to_polygon(p),
                evidence={"z_min": p["z_min"], "z_max": p["z_max"]},
                location=p["unit_label"],
            )


def _check_floor_height(ctx: ValidationContext) -> Iterator[ValidationResult]:
    """INCONSISTENT_FLOOR_HEIGHT (warning): a storey unlike its neighbours."""
    heights: list[tuple[int, float]] = []
    for floor in ctx.floors:
        z_min, z_max = floor.get("z_min"), floor.get("z_max")
        if z_min is None or z_max is None or z_max <= z_min:
            continue
        heights.append((int(floor["floor_number"]), z_max - z_min))
    if len(heights) < 3:
        # A median needs a distribution; with one or two storeys there is
        # nothing to be inconsistent with.
        return
    ordered = sorted(height for _, height in heights)
    middle = len(ordered) // 2
    median = (
        ordered[middle]
        if len(ordered) % 2
        else (ordered[middle - 1] + ordered[middle]) / 2.0
    )
    if median <= 0:
        return
    for number, height in heights:
        deviation = abs(height - median) / median
        if deviation > FLOOR_HEIGHT_TOLERANCE:
            yield ctx.result(
                _RULES["VERT-FLOOR-HEIGHT"],
                ident=f"VAL-FHT-{number}",
                object_a=f"FLOOR-{number}",
                description=(
                    f"Floor {number} is {height:.2f} m tall against a median of "
                    f"{median:.2f} m ({deviation * 100:.0f}% different). A double-height "
                    "or low storey is plausible; confirm it is intended."
                ),
                evidence={
                    "floor_height_m": round(height, 3),
                    "median_height_m": round(median, 3),
                    "deviation_ratio": round(deviation, 4),
                },
                location=DEMO_BUILDING_BUSINESS_ID,
            )


# --------------------------------------------------------------------------
# CADASTRAL rules
# --------------------------------------------------------------------------


def _check_duplicate_ulpin(ctx: ValidationContext) -> Iterator[ValidationResult]:
    """DUPLICATE_ULPIN_GEOMETRY (critical): one identifier on two records.

    The intersection is serialised unguarded, so two records that merely share an
    edge raise ``AttributeError``. That is pre-existing behaviour, pinned by
    ``tests/test_services.py::test_duplicate_identifier_touching_only_raises``.
    """
    seen: dict[str, dict[str, Any]] = {}
    for p in ctx.properties:
        key = p.get("prototype_ulpin")
        if key in seen:
            other = seen[key]
            shape, vol = calculate_volume_intersection(p, other)
            yield ctx.result(
                _RULES["CAD-DUPLICATE-ULPIN"],
                ident=f"VAL-DUP-{p['id']}",
                object_a=p["id"],
                object_b=other["id"],
                description=(
                    f"Prototype 3D ULPIN {key} is assigned to two property records."
                ),
                geometry=shape,
                evidence={
                    "overlap_volume_m3": round(vol, 2),
                    "prototype_ulpin": key,
                },
                location=p["unit_label"],
            )
        elif key:
            seen[key] = p


def _check_duplicate_geometry(ctx: ValidationContext) -> Iterator[ValidationResult]:
    """DUPLICATE_GEOMETRY (warning): two records occupying the same space."""
    seen: dict[str, dict[str, Any]] = {}
    for p in ctx.properties:
        digest = p.get("geometry_hash")
        if not digest:
            continue
        if digest in seen:
            other = seen[digest]
            yield ctx.result(
                _RULES["CAD-DUPLICATE-GEOMETRY"],
                ident=f"VAL-DUPGEO-{p['id']}-{other['id']}",
                object_a=p["id"],
                object_b=other["id"],
                description=(
                    f"{p['unit_label']} and {other['unit_label']} share geometry hash "
                    f"{digest} and occupy the same space."
                ),
                geometry=record_to_polygon(p),
                evidence={"geometry_hash": digest},
                location=p["unit_label"],
            )
        else:
            seen[digest] = p


def _check_missing_parent(ctx: ValidationContext) -> Iterator[ValidationResult]:
    """MISSING_PARENT (critical): a parent parcel that does not exist."""
    known = {str(p.get("parcel_id") or p.get("id")) for p in ctx.parcels}
    for p in ctx.properties:
        parent = p.get("parent_parcel_id")
        if parent and str(parent) not in known:
            yield ctx.result(
                _RULES["CAD-MISSING-PARENT"],
                ident=f"VAL-NOPAR-{p['id']}",
                object_a=p["id"],
                object_b=str(parent),
                description=(
                    f"{p['unit_label']} references parent parcel {parent}, which "
                    "does not exist."
                ),
                geometry=record_to_polygon(p),
                evidence={"parent_parcel_id": str(parent)},
                location=p["unit_label"],
            )


def _check_orphan_property(ctx: ValidationContext) -> Iterator[ValidationResult]:
    """ORPHAN_PROPERTY (critical): a building that does not exist."""
    known = {str(b.get("building_id") or b.get("id")) for b in ctx.buildings}
    for p in ctx.properties:
        building = p.get("building_id")
        if building and str(building) not in known:
            yield ctx.result(
                _RULES["CAD-ORPHAN-PROPERTY"],
                ident=f"VAL-ORPH-{p['id']}",
                object_a=p["id"],
                object_b=str(building),
                description=(
                    f"{p['unit_label']} references building {building}, which does "
                    "not exist."
                ),
                geometry=record_to_polygon(p),
                evidence={"building_id": str(building)},
                location=p["unit_label"],
            )


def _check_invalid_hierarchy(ctx: ValidationContext) -> Iterator[ValidationResult]:
    """INVALID_HIERARCHY (warning): a property's Z band contradicts its storey.

    Compares each property against **its own** declared floor, not against the
    building's overall range: a basement sitting below the lowest storey is
    correct, not a hierarchy violation, and only a property that claims floor N
    while occupying some other band is inconsistent.
    """
    known = {str(b.get("building_id") or b.get("id")) for b in ctx.buildings}
    bands: dict[tuple[str, int], tuple[float, float]] = {}
    for floor in ctx.floors:
        building_id = str(floor.get("building_id") or "")
        z_min, z_max = floor.get("z_min"), floor.get("z_max")
        number = floor.get("floor_number")
        if z_min is None or z_max is None or number is None:
            continue
        bands[(building_id, int(number))] = (z_min, z_max)

    for p in ctx.properties:
        building_id = str(p.get("building_id") or "")
        if building_id not in known:
            continue
        number = p.get("floor_number")
        if number is None:
            continue
        band = bands.get((building_id, int(number)))
        if band is None:
            # No declared storey to contradict. Basements and out-of-range storeys
            # are reported by the CHANGE rules instead.
            continue
        low, high = band
        if (
            abs(p["z_min"] - low) > ctx.gap_epsilon
            or abs(p["z_max"] - high) > ctx.gap_epsilon
        ):
            yield ctx.result(
                _RULES["CAD-INVALID-HIERARCHY"],
                ident=f"VAL-HIER-{p['id']}",
                object_a=p["id"],
                object_b=f"FLOOR-{number}",
                description=(
                    f"{p['unit_label']} claims floor {number} but occupies z "
                    f"[{p['z_min']}, {p['z_max']}] against that storey's "
                    f"[{low}, {high}]."
                ),
                geometry=record_to_polygon(p),
                evidence={
                    "z_min": p["z_min"],
                    "z_max": p["z_max"],
                    "floor_number": int(number),
                    "declared_floor_band": [low, high],
                },
                location=p["unit_label"],
            )


# --------------------------------------------------------------------------
# INFRASTRUCTURE rules
# --------------------------------------------------------------------------


def _infra_proxy(infra: dict[str, Any]) -> dict[str, Any]:
    return {
        "geometry_3d": infra["geometry_3d"],
        "z_min": infra["z_min"],
        "z_max": infra["z_max"],
    }


def _collisions(
    ctx: ValidationContext, infra: dict[str, Any]
) -> Iterator[tuple[dict[str, Any], Any, float]]:
    for p in ctx.properties:
        shape, vol = calculate_volume_intersection(p, _infra_proxy(infra))
        if vol > ctx.area_epsilon:
            yield p, shape, vol


def _check_underground_utility_collision(
    ctx: ValidationContext,
) -> Iterator[ValidationResult]:
    """UNDERGROUND_INFRASTRUCTURE_COLLISION (critical), catch-all for ``infrastructure[0]``.

    Scope and text are exactly the original: every property is tested against the
    first infrastructure record regardless of its type, because narrowing it would
    change which findings a caller sees. The typed rules below are additional.
    """
    infra = ctx.primary_infrastructure()
    for p, shape, vol in _collisions(ctx, infra):
        yield ctx.result(
            _RULES["INFRA-UTILITY-COLLISION"],
            ident=f"VAL-INF-{p['id']}",
            object_a=p["id"],
            object_b=infra["id"],
            description=(
                f"{infra['type'].replace('_', ' ').title()} intersects "
                f"{p['unit_label']} by {vol:.1f} m³."
            ),
            geometry=shape,
            evidence={
                "overlap_volume_m3": round(vol, 2),
                "infrastructure_type": infra["type"],
            },
            location="Basement / Utility layer",
        )


def _typed_infrastructure_rule(rule_key: str):
    """Build a rule for one declared infrastructure type."""

    def check(ctx: ValidationContext) -> Iterator[ValidationResult]:
        issue_type = TYPED_INFRASTRUCTURE[rule_key]
        rule = _RULES[f"INFRA-{rule_key}"]
        for infra in ctx.infrastructure:
            if infra.get("type") != rule_key:
                continue
            for p, shape, vol in _collisions(ctx, infra):
                yield ctx.result(
                    rule,
                    ident=f"VAL-{rule_key[:4]}-{p['id']}-{infra['id']}",
                    object_a=p["id"],
                    object_b=infra["id"],
                    description=(
                        f"{rule_key.replace('_', ' ').title()} intersects "
                        f"{p['unit_label']} by {vol:.1f} m³."
                    ),
                    geometry=shape,
                    evidence={
                        "overlap_volume_m3": round(vol, 2),
                        "infrastructure_type": infra["type"],
                    },
                    location=f"{rule_key} / {p['unit_label']}",
                )

    return check


# --------------------------------------------------------------------------
# CHANGE rules
# --------------------------------------------------------------------------


def _prior_versions(
    ctx: ValidationContext,
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for version in ctx.geometry_versions:
        grouped.setdefault(str(version.get("object_id")), []).append(version)
    for versions in grouped.values():
        versions.sort(key=lambda v: v.get("version", 0))
    return grouped


def _latest_change(
    ctx: ValidationContext,
) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    """``(current record, its previous geometry version)`` for changed objects."""
    properties = {str(p["id"]): p for p in ctx.properties}
    for object_id, versions in _prior_versions(ctx).items():
        current = properties.get(object_id)
        if current is None or not versions:
            continue
        yield current, versions[-1]


def _check_footprint_change(ctx: ValidationContext) -> Iterator[ValidationResult]:
    """FOOTPRINT_CHANGE (info): the plan geometry has been revised."""
    for current, previous in _latest_change(ctx):
        if previous.get("geometry_hash") == current.get("geometry_hash"):
            continue
        yield ctx.result(
            _RULES["CHANGE-FOOTPRINT"],
            ident=f"VAL-CHFP-{current['id']}",
            object_a=current["id"],
            object_b=str(previous.get("version")),
            description=(
                f"{current['unit_label']} footprint changed at version "
                f"{previous.get('version')}."
            ),
            geometry=record_to_polygon(current),
            evidence={
                "previous_geometry_hash": previous.get("geometry_hash"),
                "current_geometry_hash": current.get("geometry_hash"),
            },
            location=current["unit_label"],
            severity=IssueSeverity.INFO.value,
        )


def _check_height_change(ctx: ValidationContext) -> Iterator[ValidationResult]:
    """HEIGHT_CHANGE (info): the vertical extent has been revised."""
    for current, previous in _latest_change(ctx):
        old = (previous.get("z_max") or 0) - (previous.get("z_min") or 0)
        new = current["z_max"] - current["z_min"]
        if previous.get("z_min") is None or abs(new - old) <= 1e-6:
            continue
        yield ctx.result(
            _RULES["CHANGE-HEIGHT"],
            ident=f"VAL-CHHT-{current['id']}",
            object_a=current["id"],
            object_b=str(previous.get("version")),
            description=(
                f"{current['unit_label']} height changed from {old:.2f} m to "
                f"{new:.2f} m at version {previous.get('version')}."
            ),
            geometry=record_to_polygon(current),
            evidence={
                "previous_height_m": round(old, 3),
                "current_height_m": round(new, 3),
                "delta_m": round(new - old, 3),
            },
            location=current["unit_label"],
            severity=IssueSeverity.INFO.value,
        )


def _check_volume_change(ctx: ValidationContext) -> Iterator[ValidationResult]:
    """VOLUME_CHANGE (info): the enclosed volume has been revised."""
    for current, previous in _latest_change(ctx):
        if previous.get("z_min") is None or previous.get("z_max") is None:
            continue
        shape = record_to_polygon(current)
        old = calculate_volume(shape, previous["z_min"], previous["z_max"])
        new = calculate_volume(shape, current["z_min"], current["z_max"])
        if abs(new - old) <= 1e-6:
            continue
        yield ctx.result(
            _RULES["CHANGE-VOLUME"],
            ident=f"VAL-CHVOL-{current['id']}",
            object_a=current["id"],
            object_b=str(previous.get("version")),
            description=(
                f"{current['unit_label']} volume changed from {old:.1f} m³ to "
                f"{new:.1f} m³ at version {previous.get('version')}."
            ),
            geometry=shape,
            evidence={
                "previous_volume_m3": round(old, 2),
                "current_volume_m3": round(new, 2),
                "delta_m3": round(new - old, 2),
            },
            location=current["unit_label"],
            severity=IssueSeverity.INFO.value,
        )


def _check_new_floor(ctx: ValidationContext) -> Iterator[ValidationResult]:
    """NEW_FLOOR (info): property volumes on a storey the building does not declare."""
    declared = {
        str(b.get("building_id") or b.get("id")): b.get("floor_count")
        for b in ctx.buildings
    }
    for p in ctx.properties:
        building_id = str(p.get("building_id") or "")
        count = declared.get(building_id)
        floor_number = p.get("floor_number")
        if count is None or floor_number is None:
            continue
        # Basements sit below the ground storey and are not part of the count.
        if floor_number > count:
            yield ctx.result(
                _RULES["CHANGE-NEW-FLOOR"],
                ident=f"VAL-CHNEW-{building_id}-{floor_number}",
                object_a=p["id"],
                object_b=building_id,
                description=(
                    f"{p['unit_label']} sits on floor {floor_number}, but its building "
                    f"declares {count} floors."
                ),
                geometry=record_to_polygon(p),
                evidence={"floor_number": floor_number, "declared_floor_count": count},
                location=p["unit_label"],
                severity=IssueSeverity.INFO.value,
            )


def _check_removed_floor(ctx: ValidationContext) -> Iterator[ValidationResult]:
    """REMOVED_FLOOR (info): a declared storey carrying no property volumes."""
    declared = {
        str(b.get("building_id") or b.get("id")): b.get("floor_count")
        for b in ctx.buildings
    }
    present: dict[str, set[int]] = {}
    for p in ctx.properties:
        if p.get("floor_number") is not None:
            present.setdefault(str(p.get("building_id") or ""), set()).add(
                p["floor_number"]
            )
    for building_id, count in declared.items():
        if not count:
            continue
        floors = present.get(building_id, set())
        for number in range(1, int(count) + 1):
            if number not in floors:
                yield ctx.result(
                    _RULES["CHANGE-REMOVED-FLOOR"],
                    ident=f"VAL-CHDEL-{building_id}-{number}",
                    object_a=f"FLOOR-{number}",
                    object_b=building_id,
                    description=(
                        f"Building {building_id} declares {count} floors but floor "
                        f"{number} carries no property volume."
                    ),
                    evidence={"floor_number": number, "declared_floor_count": count},
                    location=building_id,
                    severity=IssueSeverity.INFO.value,
                )


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------


#: All rules, keyed by id, in **run order**. Order is load-bearing: see the
#: module docstring. Values are replaced as each rule registers.
_RULES: dict[str, ValidationRule] = {}


def _register(
    rule_id: str,
    category: RuleCategory,
    issue_type: str,
    severity: str,
    description: str,
    check: Callable[[ValidationContext], Iterator[ValidationResult]],
    collections: tuple[str, ...] = ("properties",),
) -> None:
    _RULES[rule_id] = ValidationRule(
        rule_id=rule_id,
        category=category,
        issue_type=issue_type,
        severity=severity,
        description=description,
        check=check,
        collections=collections,
    )


_register(
    "GEOM-INVALID", RuleCategory.GEOMETRY, IssueType.INVALID_GEOMETRY,
    IssueSeverity.CRITICAL, "Footprint is not a valid polygon",
    _check_invalid_geometry,
)
_register(
    "GEOM-SELF-INTERSECTION", RuleCategory.GEOMETRY, IssueType.SELF_INTERSECTION,
    IssueSeverity.CRITICAL, "Footprint ring crosses itself",
    _check_self_intersection,
)
_register(
    "GEOM-ZERO-AREA", RuleCategory.GEOMETRY, IssueType.ZERO_AREA,
    IssueSeverity.WARNING, "Footprint encloses no plan area",
    _check_zero_area,
)
_register(
    "GEOM-ZERO-VOLUME", RuleCategory.GEOMETRY, IssueType.ZERO_VOLUME,
    IssueSeverity.CRITICAL, "Record encloses no 3D volume",
    _check_zero_volume,
)
_register(
    "GEOM-INVALID-Z-RANGE", RuleCategory.GEOMETRY, IssueType.INVALID_Z_RANGE,
    IssueSeverity.CRITICAL, "z_max is not above z_min",
    _check_invalid_z_range,
)
# Containment runs immediately after validity, as in the original: a bow-tie
# polygon is recorded as invalid and *then* raises inside this rule.
_register(
    "PARCEL-PROPERTY-OUTSIDE", RuleCategory.PARCEL, IssueType.OUTSIDE_PARENT_PARCEL,
    IssueSeverity.WARNING, "Property is not contained by its parent parcel",
    _check_property_outside_parcel, ("parcels", "properties"),
)
_register(
    "PARCEL-BUILDING-OUTSIDE", RuleCategory.PARCEL, IssueType.BUILDING_OUTSIDE_PARCEL,
    IssueSeverity.WARNING, "Building is not contained by its parent parcel",
    _check_building_outside_parcel, ("parcels", "buildings"),
)
_register(
    "PARCEL-UNASSIGNED-BUILDING", RuleCategory.PARCEL, IssueType.UNASSIGNED_BUILDING,
    IssueSeverity.WARNING, "Building lies in a parcel but has no parent assigned",
    _check_unassigned_building, ("parcels", "buildings"),
)
_register(
    "PARCEL-UNASSIGNED-PROPERTY", RuleCategory.PARCEL, IssueType.UNASSIGNED_PROPERTY,
    IssueSeverity.WARNING, "Property lies in a parcel but has no parent assigned",
    _check_unassigned_property, ("parcels", "properties"),
)
_register(
    "VERT-FLOOR-OVERLAP", RuleCategory.VERTICAL, IssueType.FLOOR_OVERLAP,
    IssueSeverity.CRITICAL, "Two storey plates occupy the same 3D space",
    _check_floor_overlap, ("floors",),
)
_register(
    "VERT-PROPERTY-OVERLAP", RuleCategory.VERTICAL, IssueType.VERTICAL_VOLUME_OVERLAP,
    IssueSeverity.CRITICAL, "Two property volumes occupy the same 3D space",
    _check_property_overlap, ("properties",),
)
_register(
    "VERT-FLOOR-GAP", RuleCategory.VERTICAL, IssueType.EXPECTED_ADJACENT_FLOOR_GAP,
    IssueSeverity.WARNING, "Vertical gap between expected adjacent floors",
    _check_floor_gap, ("properties",),
)
_register(
    "VERT-DISCONNECTED-STACK", RuleCategory.VERTICAL, IssueType.DISCONNECTED_FLOOR_STACK,
    IssueSeverity.WARNING, "Floors have no plan-area connection",
    _check_disconnected_stack, ("properties",),
)
_register(
    "VERT-Z-ORDER", RuleCategory.VERTICAL, IssueType.FLOOR_Z_ORDER_INCONSISTENCY,
    IssueSeverity.CRITICAL, "z_min is at or above z_max",
    _check_z_order, ("properties",),
)
_register(
    "VERT-FLOOR-HEIGHT", RuleCategory.VERTICAL, IssueType.INCONSISTENT_FLOOR_HEIGHT,
    IssueSeverity.WARNING, "Storey height departs from the building's median",
    _check_floor_height, ("floors",),
)
_register(
    "CAD-DUPLICATE-ULPIN", RuleCategory.CADASTRAL, IssueType.DUPLICATE_ULPIN_GEOMETRY,
    IssueSeverity.CRITICAL, "One prototype ULPIN is assigned to two records",
    _check_duplicate_ulpin, ("properties",),
)
_register(
    "CAD-DUPLICATE-GEOMETRY", RuleCategory.CADASTRAL, IssueType.DUPLICATE_GEOMETRY,
    IssueSeverity.WARNING, "Two records occupy the same geometry",
    _check_duplicate_geometry, ("properties",),
)
_register(
    "CAD-MISSING-PARENT", RuleCategory.CADASTRAL, IssueType.MISSING_PARENT,
    IssueSeverity.CRITICAL, "Parent parcel does not exist",
    _check_missing_parent, ("parcels", "properties"),
)
_register(
    "CAD-ORPHAN-PROPERTY", RuleCategory.CADASTRAL, IssueType.ORPHAN_PROPERTY,
    IssueSeverity.CRITICAL, "Referenced building does not exist",
    _check_orphan_property, ("buildings", "properties"),
)
_register(
    "CAD-INVALID-HIERARCHY", RuleCategory.CADASTRAL, IssueType.INVALID_HIERARCHY,
    IssueSeverity.WARNING, "Property lies outside its building's storey range",
    _check_invalid_hierarchy, ("buildings", "floors", "properties"),
)
_register(
    "INFRA-UTILITY-COLLISION", RuleCategory.INFRASTRUCTURE,
    IssueType.UNDERGROUND_INFRASTRUCTURE_COLLISION, IssueSeverity.CRITICAL,
    "Underground infrastructure intersects a property volume",
    _check_underground_utility_collision, ("infrastructure", "properties"),
)
for _key in ("TUNNEL", "BASEMENT", "FOUNDATION", "ELEVATED_STRUCTURE"):
    _register(
        f"INFRA-{_key}", RuleCategory.INFRASTRUCTURE, TYPED_INFRASTRUCTURE[_key],
        IssueSeverity.CRITICAL,
        f"{_key.replace('_', ' ').title()} intersects a property volume",
        _typed_infrastructure_rule(_key), ("infrastructure", "properties"),
    )
_register(
    "CHANGE-FOOTPRINT", RuleCategory.CHANGE, IssueType.FOOTPRINT_CHANGE,
    IssueSeverity.INFO, "Plan geometry has been revised since the last version",
    _check_footprint_change, ("geometry_versions", "properties"),
)
_register(
    "CHANGE-HEIGHT", RuleCategory.CHANGE, IssueType.HEIGHT_CHANGE,
    IssueSeverity.INFO, "Vertical extent has been revised since the last version",
    _check_height_change, ("geometry_versions", "properties"),
)
_register(
    "CHANGE-VOLUME", RuleCategory.CHANGE, IssueType.VOLUME_CHANGE,
    IssueSeverity.INFO, "Enclosed volume has been revised since the last version",
    _check_volume_change, ("geometry_versions", "properties"),
)
_register(
    "CHANGE-NEW-FLOOR", RuleCategory.CHANGE, IssueType.NEW_FLOOR,
    IssueSeverity.INFO, "Property volume sits on a storey the building does not declare",
    _check_new_floor, ("buildings", "properties"),
)
_register(
    "CHANGE-REMOVED-FLOOR", RuleCategory.CHANGE, IssueType.REMOVED_FLOOR,
    IssueSeverity.INFO, "Declared storey carries no property volume",
    _check_removed_floor, ("buildings", "properties"),
)


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------


class ValidationEngine:
    """A registry of rules and a way to run them.

    Holds no scene state, so one engine can serve every request.
    """

    def __init__(
        self,
        rules: list[ValidationRule] | None = None,
        *,
        area_epsilon: float = AREA_EPSILON,
        gap_epsilon: float = GAP_EPSILON,
    ) -> None:
        self._rules: list[ValidationRule] = list(rules if rules is not None else _RULES.values())
        self.area_epsilon = area_epsilon
        self.gap_epsilon = gap_epsilon

    @property
    def rules(self) -> list[ValidationRule]:
        return list(self._rules)

    def rule(self, rule_id: str) -> ValidationRule:
        for candidate in self._rules:
            if candidate.rule_id == rule_id:
                return candidate
        raise KeyError(f"unknown rule {rule_id!r}")

    def definitions(self) -> list[dict[str, Any]]:
        return [rule.definition() for rule in self._rules]

    def run_all_rules(
        self,
        repo: CadastreRepository,
        *,
        on_result: Callable[[ValidationResult], None] | None = None,
    ) -> list[ValidationResult]:
        """Run every rule in order, in a single pass over the scene.

        ``on_result`` is called **as each finding is produced**, not at the end.
        That matters: a rule that raises leaves the findings before it already
        recorded, which is what the original implementation did and what
        ``tests/test_services.py::test_invalid_geometry_is_appended_then_raises``
        pins.

        Results keep rule order, which is what makes a run reproducible.
        """
        context = ValidationContext(
            repo, area_epsilon=self.area_epsilon, gap_epsilon=self.gap_epsilon
        )
        results: list[ValidationResult] = []
        for rule in self._rules:
            for result in rule.check(context):
                results.append(result)
                if on_result is not None:
                    on_result(result)
        return results

    def run_rule(
        self, repo: CadastreRepository, rule_id: str
    ) -> list[ValidationResult]:
        """Run one rule on its own, for explaining or debugging a finding."""
        context = ValidationContext(
            repo, area_epsilon=self.area_epsilon, gap_epsilon=self.gap_epsilon
        )
        return list(self.rule(rule_id).check(context))

    def summary(self, results: list[ValidationResult]) -> dict[str, Any]:
        """Counts by severity, category and rule, plus totals."""
        by_severity: dict[str, int] = {}
        by_category: dict[str, int] = {}
        by_rule: dict[str, int] = {}
        for result in results:
            by_severity[result.severity] = by_severity.get(result.severity, 0) + 1
            by_category[result.category] = by_category.get(result.category, 0) + 1
            by_rule[result.rule_id] = by_rule.get(result.rule_id, 0) + 1
        return {
            "total": len(results),
            "by_severity": by_severity,
            "by_category": by_category,
            "by_rule": by_rule,
            "rules_evaluated": len(self._rules),
            "generated_at": now(),
        }


_ENGINE: ValidationEngine | None = None


def get_engine() -> ValidationEngine:
    """The shared engine carrying all rules. Rebuilt if the registry changes."""
    global _ENGINE
    if _ENGINE is None or len(_ENGINE.rules) != len(_RULES):
        _ENGINE = ValidationEngine()
    return _ENGINE


def run_all_rules(
    repo: CadastreRepository,
    *,
    engine: ValidationEngine | None = None,
    on_result: Callable[[ValidationResult], None] | None = None,
) -> list[ValidationResult]:
    """Run every registered rule against a store.

    Pass ``on_result`` to receive findings as they are produced; see
    :meth:`ValidationEngine.run_all_rules` for why that matters.
    """
    return (engine or get_engine()).run_all_rules(repo, on_result=on_result)


def run_rule(
    repo: CadastreRepository, rule_id: str, *, engine: ValidationEngine | None = None
) -> list[ValidationResult]:
    """Run one rule by id. Raises ``KeyError`` for an unknown id."""
    return (engine or get_engine()).run_rule(repo, rule_id)


def get_rule_definitions(*, engine: ValidationEngine | None = None) -> list[dict[str, Any]]:
    """Machine-readable description of every registered rule."""
    return (engine or get_engine()).definitions()


def get_validation_summary(
    repo: CadastreRepository | None = None,
    *,
    results: list[ValidationResult] | None = None,
    engine: ValidationEngine | None = None,
) -> dict[str, Any]:
    """Summary of a validation run: counts by severity, category and rule.

    Pass ``results`` to summarise an in-memory run, or ``repo`` to summarise the
    findings already stored. This never runs the rules itself, so it is safe to
    call against an empty store -- several rules assume a seeded scene.

    To summarise a fresh run, compose the two calls:

    .. code-block:: python

        summary = get_validation_summary(results=run_all_rules(repo))
    """
    if results is None:
        stored = repo.records("issues") if repo is not None else []
        by_severity: dict[str, int] = {}
        by_category: dict[str, int] = {}
        by_rule: dict[str, int] = {}
        for issue in stored:
            by_severity[issue["severity"]] = by_severity.get(issue["severity"], 0) + 1
            category = issue.get("category")
            if category:
                by_category[category] = by_category.get(category, 0) + 1
            rule_id = issue.get("rule_id")
            if rule_id:
                by_rule[rule_id] = by_rule.get(rule_id, 0) + 1
        return {
            "total": len(stored),
            "by_severity": by_severity,
            "by_category": by_category,
            "by_rule": by_rule,
            "rules_evaluated": len((engine or get_engine()).rules),
            "source": "stored",
            "generated_at": now(),
        }
    summary = (engine or get_engine()).summary(results)
    summary["source"] = "run"
    return summary


def persist_results(
    repo: CadastreRepository, results: list[ValidationResult]
) -> list[dict[str, Any]]:
    """Replace the stored findings with a run's results."""
    repo.clear("issues")
    return [repo.add("issues", result.to_issue_record()) for result in results]


__all__ = [
    "AREA_EPSILON",
    "GAP_EPSILON",
    "FLOOR_HEIGHT_TOLERANCE",
    "RULES",
    "ValidationContext",
    "ValidationEngine",
    "ValidationResult",
    "ValidationRule",
    "get_engine",
    "get_rule_definitions",
    "get_validation_summary",
    "persist_results",
    "run_all_rules",
    "run_rule",
]
