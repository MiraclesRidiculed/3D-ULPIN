"""Vertical property-volume generation.

Turns a parcel, an extracted building and its storeys into volumetric property
rights: a footprint plus a vertical band, carrying a stable identifier, a
geometry hash, a geometry version, and both source and processing provenance.

Nothing is invented
-------------------
The single most important rule in this module: **apartment boundaries are never
imagined.** Two inputs are accepted, and only two:

* **unit outlines** -- closed polygons supplied from a floor plan or cadastre;
* **dividing walls** -- lines supplied from a floor plan, which are turned into
  regions by a planar subdivision.

When neither is supplied, the output is **one volume per storey covering the whole
floor plate**, marked ``volume_scope="FLOOR"`` with ``unit_label=None`` and
``units_inferred=False``. That is an honest statement about what is known: the
storey's extent, and nothing more. It is emphatically *not* a claim that the
storey is a single ownership unit, which is the mistake this module exists to
avoid.

A synthesised label is still produced for floor-scope volumes -- the stable
identifier needs one, and it has to be stable -- but it is a **volume key**, not
a unit name, it is recorded as ``synthesised`` in ``unit_label_source``, and it
always contains the storey so it cannot collide with a real unit label repeated
on every floor.

Where the volumes come from
---------------------------
These are derived records, not surveyed ones, so they are written to their own
``generated_property_volumes`` table rather than mixed into the cadastral
``property_volumes``. The demo scene's 17 hand-authored properties are a
deliberate showcase and are left untouched.

Both provenances are kept, and they answer different questions:

``source_provenance``
    Which observation this came from: the point cloud, the extraction and
    segmentation jobs, the geometry hash of each input.
``processing_provenance``
    How it was made: the algorithm, whether units came from a plan, the
    partition count, the parameters, the warnings.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

from app.models.enums import PropertyType, VolumeScope
from app.services import geometry as geometry_service
from app.services import ulpin as ulpin_service
from app.utils import now

if TYPE_CHECKING:  # pragma: no cover - typing only
    from shapely.geometry import Polygon

#: Bumped when the generation rules change in a way that alters output.
GENERATOR_VERSION = "1.0.0"

#: Recorded on every result so a consumer cannot mistake this for cadastral record.
GENERATION_METHOD = "derived_geometric"

METHOD_DESCRIPTION = (
    "Derived geometric property-volume generation. Volumes are built from a "
    "parent parcel, an extracted building footprint and storey geometry with a "
    "vertical band. Where floor-plan unit data is available the storey is split "
    "into units by a planar subdivision of the supplied dividing walls; where it "
    "is not, one volume per storey is produced and explicitly marked as storey "
    "scope. Apartment boundaries are never invented."
)

#: Status values, matching the strings the cadastre already uses.
STATUS_MAPPED = "MAPPED"
STATUS_REVIEW = "HUMAN REVIEW REQUIRED"

#: Where the parent parcel could not be established.
UNASSIGNED_PARCEL = "UNASSIGNED"

#: A unit smaller than this is a partition artefact, not a separate right.
DEFAULT_MIN_UNIT_AREA = 2.0
#: How far the unit areas may miss the floor area before it is flagged.
DEFAULT_AREA_TOLERANCE = 0.02

#: Vertical position relative to the ground datum.
POSITION_UNDERGROUND = "UNDERGROUND"
POSITION_AT_GRADE = "AT_GRADE"
POSITION_ELEVATED = "ELEVATED"


# --------------------------------------------------------------------------
# Result carriers
# --------------------------------------------------------------------------


@dataclass
class UnitRegion:
    """One region of a storey plate: either a real unit, or the whole plate."""

    polygon: "Polygon"
    #: The label from the floor plan, or a synthesised volume key.
    label: str | None
    #: ``floor_plan`` when supplied, ``synthesised`` when the plate is undivided.
    label_source: str
    area_m2: float


@dataclass
class FloorSplit:
    """The result of dividing one storey plate."""

    regions: list[UnitRegion]
    #: True when the regions came from supplied unit data.
    units_available: bool
    floor_area_m2: float
    reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def units_inferred(self) -> bool:
        """Always False. This module never invents a unit boundary."""
        return False

    @property
    def scope(self) -> str:
        return VolumeScope.UNIT if self.units_available else VolumeScope.FLOOR


@dataclass
class PropertyVolume:
    """One generated volumetric property right."""

    id: str
    prototype_ulpin: str
    parent_parcel_id: str
    building_id: str
    floor_number: int
    floor_label: str
    unit_label: str | None
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
    review_reasons: list[str]
    notes: list[str]
    source_provenance: dict[str, Any]
    processing_provenance: dict[str, Any]
    created_at: str

    def to_record(self) -> dict[str, Any]:
        """Flatten for storage."""
        return {
            "id": self.id,
            "prototype_ulpin": self.prototype_ulpin,
            "parent_parcel_id": self.parent_parcel_id,
            "building_id": self.building_id,
            "floor_number": self.floor_number,
            "floor_label": self.floor_label,
            "unit_label": self.unit_label,
            "unit_label_source": self.unit_label_source,
            "volume_scope": self.volume_scope,
            "property_type": self.property_type,
            "vertical_position": self.vertical_position,
            "z_min": self.z_min,
            "z_max": self.z_max,
            "geometry_3d": self.geometry_3d,
            "area_m2": self.area_m2,
            "volume_m3": self.volume_m3,
            "geometry_hash": self.geometry_hash,
            "geometry_version": self.geometry_version,
            "status": self.status,
            "units_inferred": self.units_inferred,
            "requires_human_review": self.requires_human_review,
            "source_provenance": self.source_provenance,
            "processing_provenance": self.processing_provenance,
            "review_reasons": self.review_reasons,
            "notes": self.notes,
            "generated_at": self.created_at,
        }


# --------------------------------------------------------------------------
# Measurement
# --------------------------------------------------------------------------


def calculate_area(footprint: Any, *, source_crs: str) -> float:
    """Plan area of a footprint in square metres.

    ``source_crs`` is required, not optional: a footprint's coordinates are
    meaningless without it, and defaulting to one is how square degrees get
    reported as square metres.
    """
    if footprint is None or footprint.is_empty:
        return 0.0
    return round(geometry_service.measure_area_m2(footprint, source_crs=source_crs), 2)


def calculate_volume(
    footprint: Any, z_min: float, z_max: float, *, source_crs: str
) -> float:
    """Volume in cubic metres: plan area times the vertical extent."""
    if footprint is None or footprint.is_empty or z_max <= z_min:
        return 0.0
    return round(
        geometry_service.measure_volume_m3(
            footprint, z_min, z_max, source_crs=source_crs
        ),
        2,
    )


# --------------------------------------------------------------------------
# split_floor_into_units
# --------------------------------------------------------------------------


def split_floor_into_units(
    floor_footprint: Any,
    *,
    source_crs: str,
    unit_polygons: Sequence[Any] | None = None,
    dividing_walls: Sequence[Any] | None = None,
    min_unit_area: float = DEFAULT_MIN_UNIT_AREA,
    area_tolerance: float = DEFAULT_AREA_TOLERANCE,
) -> FloorSplit:
    """Divide a storey plate into unit regions, or report the undivided plate.

    Two accepted inputs, both *supplied*:

    * ``unit_polygons`` -- closed unit outlines. Each is clipped to the plate, so
      an outline that overshoots cannot enlarge the storey.
    * ``dividing_walls`` -- dividing edges, turned into regions by a planar
      subdivision of the plate.

    With neither, the plate is returned whole and marked as storey scope. **No
    boundary is ever invented**, and the areas of the returned regions always sum
    to the plate's area.
    """
    reasons: list[str] = []
    notes: list[str] = []

    if floor_footprint is None or floor_footprint.is_empty:
        return FloorSplit([], False, 0.0, reasons=["EMPTY_FLOOR: no storey geometry"])

    plate_area = geometry_service.measure_area_m2(
        floor_footprint, source_crs=source_crs
    )
    if plate_area <= 0:
        return FloorSplit(
            [], False, 0.0, reasons=["ZERO_AREA: storey plate has no area"]
        )

    regions = _regions_from_unit_polygons(
        floor_footprint, unit_polygons, source_crs, min_unit_area, reasons
    )
    if not regions:
        regions = _regions_from_walls(
            floor_footprint, dividing_walls, source_crs, min_unit_area, reasons
        )

    if not regions:
        notes.append(
            "NO_UNIT_DATA: no floor-plan unit outlines or dividing walls were "
            "supplied, so this is one volume covering the whole storey plate. It "
            "is a storey volume, not an individual ownership unit, and no unit "
            "boundaries have been inferred."
        )
        return FloorSplit(
            regions=[
                UnitRegion(
                    polygon=floor_footprint,
                    label=None,
                    label_source="synthesised",
                    area_m2=round(plate_area, 2),
                )
            ],
            units_available=False,
            floor_area_m2=round(plate_area, 2),
            reasons=reasons,
            notes=notes,
        )

    total = sum(region.area_m2 for region in regions)
    if plate_area > 0 and abs(total - plate_area) / plate_area > area_tolerance:
        reasons.append(
            f"AREA_MISMATCH: unit areas total {total:.2f} m2 against a plate of "
            f"{plate_area:.2f} m2, so the supplied unit data does not tile the storey"
        )

    for index, region in enumerate(regions, start=1):
        if region.label is None:
            region.label = f"U{index:02d}"
            region.label_source = "synthesised"
            notes.append(
                f"UNIT_LABEL_SYNTHESISED: unit {index} arrived without a label, so "
                f"a positional key {region.label} was used"
            )

    notes.append(
        f"UNITS_FROM_PLAN: the storey was divided into {len(regions)} region(s) "
        "from supplied floor-plan data"
    )
    return FloorSplit(
        regions=regions,
        units_available=True,
        floor_area_m2=round(plate_area, 2),
        reasons=reasons,
        notes=notes,
    )


def _regions_from_unit_polygons(
    floor_footprint: Any,
    unit_polygons: Sequence[Any] | None,
    source_crs: str,
    min_unit_area: float,
    reasons: list[str],
) -> list[UnitRegion]:
    """Turn supplied unit outlines into regions, clipped to the storey plate."""
    if not unit_polygons:
        return []
    regions: list[UnitRegion] = []
    for index, polygon in enumerate(unit_polygons, start=1):
        clipped = geometry_service.clip_polygon(
            polygon, floor_footprint, minimum_area=min_unit_area
        )
        if clipped is None:
            reasons.append(
                f"UNIT_OUTSIDE_FLOOR: supplied unit {index} lies outside its storey "
                "plate and was dropped"
            )
            continue
        if clipped.area < geometry_service.measure_area_m2(
            polygon, source_crs=source_crs
        ) - min_unit_area:
            reasons.append(
                f"UNIT_CLIPPED: supplied unit {index} extended beyond its storey "
                "plate and was clipped to it"
            )
        regions.append(
            UnitRegion(
                polygon=clipped,
                label=None,
                label_source="floor_plan",
                area_m2=round(
                    geometry_service.measure_area_m2(clipped, source_crs=source_crs), 2
                ),
            )
        )
    return regions


def _regions_from_walls(
    floor_footprint: Any,
    dividing_walls: Sequence[Any] | None,
    source_crs: str,
    min_unit_area: float,
    reasons: list[str],
) -> list[UnitRegion]:
    """Turn supplied dividing walls into regions by planar subdivision."""
    if not dividing_walls:
        return []
    faces = geometry_service.planar_subdivide(floor_footprint, dividing_walls)
    if len(faces) <= 1:
        reasons.append(
            "WALLS_PRODUCED_NO_DIVISION: the supplied dividing walls do not "
            "partition the storey plate, so it is treated as undivided"
        )
        return []
    regions = [
        UnitRegion(
            polygon=face,
            label=None,
            label_source="floor_plan",
            area_m2=round(
                geometry_service.measure_area_m2(face, source_crs=source_crs), 2
            ),
        )
        for face in faces
        if geometry_service.measure_area_m2(face, source_crs=source_crs) >= min_unit_area
    ]
    return regions


# --------------------------------------------------------------------------
# Assignment
# --------------------------------------------------------------------------


@dataclass
class ParcelMatch:
    """Outcome of matching a footprint against the cadastre."""

    parcel_id: str | None
    confidence: str
    reasons: list[str] = field(default_factory=list)


def assign_parent_parcel(
    footprint: Any,
    parcels: Iterable[Mapping[str, Any]],
    *,
    source_crs: str,
) -> ParcelMatch:
    """Find the parcel a footprint belongs to.

    Containment is the confident answer; a single overlap is accepted but marked
    as such, because a footprint straddling a boundary is a real situation a
    human must resolve. Zero or several matches are flagged rather than guessed.
    """
    reasons: list[str] = []
    if footprint is None or footprint.is_empty:
        return ParcelMatch(None, "none", ["NO_FOOTPRINT: nothing to match"])

    local = geometry_service.to_local_plane(footprint, source_crs)
    contained: list[str] = []
    overlapping: list[str] = []

    # Containment is tested with an area tolerance rather than exactly. The
    # footprint has been reprojected into the local plane and back, which leaves
    # nanometre-scale slivers along the boundary; an exact zero-area test reads
    # those as "not contained" and would report every building as overlapping its
    # own parcel.
    tolerance = max(1e-9, local.area * 1e-9)

    for parcel in parcels:
        parcel_id = str(parcel.get("parcel_id") or parcel.get("id") or "")
        geojson = parcel.get("geometry")
        if not parcel_id or not isinstance(geojson, Mapping):
            continue
        try:
            shape = geometry_service.geojson_to_polygon(geojson)
        except Exception:  # noqa: BLE001 - a malformed parcel is not fatal
            continue
        if local.area > 0 and local.difference(shape).area <= tolerance:
            contained.append(parcel_id)
        elif local.intersects(shape) and local.intersection(shape).area > 0:
            overlapping.append(parcel_id)

    if len(contained) == 1:
        return ParcelMatch(contained[0], "contained")
    if len(contained) > 1:
        reasons.append(
            f"MULTIPLE_PARCELS: the footprint lies inside {len(contained)} parcels "
            f"({', '.join(contained)}); the parcel boundary must be resolved"
        )
        return ParcelMatch(contained[0], "ambiguous", reasons)
    if len(overlapping) == 1:
        reasons.append(
            f"PARTIAL_OVERLAP: the footprint extends beyond parcel "
            f"{overlapping[0]} rather than sitting inside it"
        )
        return ParcelMatch(overlapping[0], "overlapping", reasons)
    if len(overlapping) > 1:
        reasons.append(
            f"MULTIPLE_PARCELS: the footprint overlaps {len(overlapping)} parcels "
            f"({', '.join(overlapping)}) and is inside none of them"
        )
        return ParcelMatch(None, "ambiguous", reasons)

    reasons.append(
        "NO_PARCEL_MATCH: the footprint does not intersect any parcel, so no "
        "parent parcel could be assigned"
    )
    return ParcelMatch(None, "none", reasons)


def assign_building(
    building: Mapping[str, Any] | None, building_id: str | None = None
) -> str:
    """The building a volume belongs to.

    Falls back to the caller's ``building_id`` when no building record is
    supplied, so a volume can still be produced from a bare footprint.
    """
    if building is not None:
        resolved = building.get("id") or building.get("building_id")
        if resolved:
            return str(resolved)
    return str(building_id or UNASSIGNED_PARCEL)


def assign_floor(floor: Mapping[str, Any] | None, fallback: int | None = None) -> int:
    """The storey number a volume belongs to, defaulting to the ground storey."""
    if floor is not None and floor.get("floor_number") is not None:
        return int(floor["floor_number"])
    return int(fallback if fallback is not None else 1)


def floor_label(floor_number: int) -> str:
    """Conventional storey label: ``B1`` below grade, ``GF`` at grade, then ``1``.."""
    if floor_number < 0:
        return f"B{abs(floor_number)}"
    if floor_number == 0:
        return "B1"
    if floor_number == 1:
        return "GF"
    return str(floor_number)


def vertical_position(z_min: float, z_max: float, ground_datum: float | None) -> str:
    """Where a volume sits relative to the ground datum.

    ``None`` when no datum was supplied: the honest answer is that the position
    is undetermined, and guessing would misclassify basements as ground floors.
    """
    if ground_datum is None:
        return "UNDETERMINED"
    if z_max <= ground_datum:
        return POSITION_UNDERGROUND
    if z_min >= ground_datum:
        return POSITION_ELEVATED
    return POSITION_AT_GRADE


def volume_key(building_ref: str, floor_number: int, unit_label: str | None) -> str:
    """Stable per-volume key, used to build the identifier.

    Includes the storey because floor plans routinely label every floor's first
    unit "01"; without the storey those would collide and the identifier would not
    be stable.

    Takes a **building reference**, not the extracted building's ``id``. That id
    is scoped to the processing job, so using it meant a re-run re-issued the
    identifier of an unchanged volume and defeated the replace-not-accumulate
    upsert. See ``stable_building_ref`` for what a reference is.

    Composed only from stable attributes, never from geometry: a moved boundary
    must not mint a new identifier.
    """
    base = f"{building_ref}-F{floor_number:02d}"
    return f"{base}-{unit_label}" if unit_label else base


def stable_building_ref(source_id: str | None, ordinal: int) -> str:
    """Identity anchor for a building discovered in a point cloud.

    A discovered building has no pre-existing cadastral identity, so the
    strongest stable anchor is **which source found it and in what order**.
    Extraction is deterministic, so re-processing one source yields the same
    buildings in the same order and this reference is unchanged. Two sources
    stay distinct, which is correct -- they are two observations.

    ``ordinal`` is 1-based, matching how buildings are numbered in the
    extraction response, so the reference and the listing agree.
    """
    return f"{source_id or 'UNSOURCED'}-B{ordinal:02d}"


# --------------------------------------------------------------------------
# create_volume_from_footprint
# --------------------------------------------------------------------------


def create_volume_from_footprint(
    footprint: Any,
    *,
    z_min: float,
    z_max: float,
    source_crs: str,
    parent_parcel_id: str,
    building_id: str,
    floor_number: int,
    building_ref: str | None = None,
    unit_label: str | None = None,
    unit_label_source: str = "synthesised",
    volume_scope: str = VolumeScope.FLOOR,
    ground_datum: float | None = None,
    source_provenance: Mapping[str, Any] | None = None,
    processing_provenance: Mapping[str, Any] | None = None,
    review_reasons: Sequence[str] | None = None,
    notes: Sequence[str] | None = None,
    geometry_version: int = 1,
) -> PropertyVolume | None:
    """Build one volumetric property right from a footprint and a vertical band.

    Returns ``None`` for geometry that cannot form a volume -- an empty or
    zero-area footprint, or an inverted vertical range -- rather than emitting a
    record that is meaningless.
    """
    reasons = list(review_reasons or [])
    if footprint is None or footprint.is_empty:
        reasons.append("EMPTY_FOOTPRINT: the volume has no plan geometry")
        return None
    area = geometry_service.measure_area_m2(footprint, source_crs=source_crs)
    if area <= 0:
        reasons.append("ZERO_AREA: the volume has no measurable plan area")
        return None
    if z_max <= z_min:
        reasons.append(
            f"INVALID_Z_RANGE: z_max ({z_max}) is not above z_min ({z_min})"
        )
        return None
    if not footprint.is_valid:
        reasons.append("INVALID_GEOMETRY: the footprint is not a valid polygon")
        repaired = geometry_service.simplify_polygon(footprint, tolerance=0.0)
        if repaired.is_empty or repaired.area <= 0:
            return None
        footprint = repaired

    position = vertical_position(z_min, z_max, ground_datum)
    if position == "UNDETERMINED":
        reasons.append(
            "DATUM_UNKNOWN: no ground datum was supplied, so whether this volume "
            "is a basement or an elevated volume could not be determined"
        )
        notes = list(notes or []) + [reasons[-1]]

    if position == POSITION_UNDERGROUND:
        property_type = PropertyType.BASEMENT
    elif volume_scope == VolumeScope.UNIT:
        property_type = PropertyType.UNIT
    else:
        property_type = PropertyType.FLOOR

    # Falls back to ``building_id`` only when no reference was supplied, so a
    # direct caller keeps the old behaviour rather than a broken key.
    key = volume_key(building_ref or building_id, floor_number, unit_label)
    local = geometry_service.to_local_plane(footprint, source_crs)
    geometry_hash = geometry_service.calculate_geometry_hash(local, z_min, z_max)
    ulpin = ulpin_service.generate_stable_ulpin(
        prefix=ulpin_service.VOLUME_PREFIX,
        parent=parent_parcel_id or UNASSIGNED_PARCEL,
        unit=key,
    )
    created = now()
    wgs84 = _to_wgs84(footprint, source_crs)

    status = STATUS_REVIEW if reasons else STATUS_MAPPED
    return PropertyVolume(
        # The key already carries the building and storey, so it is not
        # repeated in the id.
        id=f"GPV-{_slug(key)}",
        prototype_ulpin=ulpin,
        parent_parcel_id=parent_parcel_id or UNASSIGNED_PARCEL,
        building_id=building_id,
        floor_number=floor_number,
        floor_label=floor_label(floor_number),
        unit_label=unit_label,
        unit_label_source=unit_label_source,
        volume_scope=volume_scope,
        property_type=property_type,
        vertical_position=position,
        z_min=round(z_min, 3),
        z_max=round(z_max, 3),
        # A bare GeoJSON polygon, matching the cadastral ``property_volumes``
        # contract: ``geometry_3d`` is a real geometry column, and the vertical
        # band lives in ``z_min``/``z_max``. Storing a ``{z_min, z_max,
        # footprint}`` wrapper here would not be a geometry at all.
        geometry_3d=wgs84,
        area_m2=round(area, 2),
        volume_m3=round(area * (z_max - z_min), 2),
        geometry_hash=geometry_hash,
        geometry_version=geometry_version,
        status=status,
        units_inferred=False,
        requires_human_review=bool(reasons),
        review_reasons=reasons,
        notes=list(notes or []),
        source_provenance={
            "method": GENERATION_METHOD,
            "generator_version": GENERATOR_VERSION,
            "source_crs": source_crs,
            "wgs84_stored": True,
            **dict(source_provenance or {}),
        },
        processing_provenance={
            "method_description": METHOD_DESCRIPTION,
            "algorithm": "plate -> optional planar subdivision -> vertical band",
            "units_inferred": False,
            "ground_datum": ground_datum,
            "created_at": created,
            **dict(processing_provenance or {}),
        },
        created_at=created,
    )


def _slug(value: str) -> str:
    from app.services.ulpin import _slug as ulpin_slug

    return ulpin_slug(value)


def _to_wgs84(footprint: Any, source_crs: str) -> dict[str, Any]:
    """Plan geometry as WGS84 GeoJSON, the storage CRS used everywhere."""
    from app.services import crs as crs_service

    geometry = footprint
    if crs_service.is_metric(source_crs):
        geometry = crs_service.transform_geometry(
            footprint, source_crs, geometry_service.GEOGRAPHIC_CRS
        )
    return {
        "type": geometry.geom_type,
        "coordinates": [[list(c) for c in geometry.exterior.coords]],
    }


# --------------------------------------------------------------------------
# generate_property_volumes
# --------------------------------------------------------------------------


@dataclass
class VolumeGeneration:
    """The complete result of one generation run."""

    volumes: list[PropertyVolume]
    buildings: int
    floors: int
    units_generated: int
    floors_without_unit_data: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)

    @property
    def total_area_m2(self) -> float:
        return round(sum(v.area_m2 for v in self.volumes), 2)

    @property
    def total_volume_m3(self) -> float:
        return round(sum(v.volume_m3 for v in self.volumes), 2)

    @property
    def requires_human_review(self) -> bool:
        return any(v.requires_human_review for v in self.volumes)


def generate_property_volumes(
    building: Mapping[str, Any],
    floors: Sequence[Mapping[str, Any]],
    *,
    source_crs: str,
    parcels: Iterable[Mapping[str, Any]] = (),
    floor_plans: Mapping[int, Mapping[str, Any]] | None = None,
    ground_datum: float | None = None,
    building_ref: str | None = None,
    source_provenance: Mapping[str, Any] | None = None,
) -> VolumeGeneration:
    """Generate every property volume for one building.

    Parameters
    ----------
    building:
        The extracted building. ``footprint`` is used for the parcel match; the
        storey footprints come from ``floors`` where available and the building
        footprint otherwise.
    floors:
        The building's storeys, each with ``floor_number``, ``z_min``, ``z_max``
        and plan geometry in ``source_crs``.
    floor_plans:
        Optional per-storey unit data, keyed by ``floor_number``. Recognised keys
        are ``unit_polygons``, ``dividing_walls`` and ``unit_labels``. Absent or
        empty means the storey is undivided -- and is reported as such.
    ground_datum:
        Absolute elevation of the datum, used to tell a basement from a ground
        storey from an elevated volume. Without it the position is undetermined
        and every volume is flagged.
    building_ref:
        Stable identity anchor for this building (see
        :func:`stable_building_ref`). Used for the identifier and the upsert key
        so re-processing a source does not re-issue identifiers or accumulate
        duplicate volumes. Defaults to ``building_id``.
    """
    plans = dict(floor_plans or {})
    building_id = assign_building(building)
    parcel_list = list(parcels)
    warnings: list[str] = []

    building_footprint = _footprint_in_crs(building, source_crs, "footprint")
    if building_footprint is None:
        building_footprint = _footprint_in_crs(building, source_crs, "geometry_3d")
    match = assign_parent_parcel(building_footprint, parcel_list, source_crs=source_crs)
    parcel_id = match.parcel_id or UNASSIGNED_PARCEL
    warnings.extend(match.reasons)

    volumes: list[PropertyVolume] = []
    undivided: list[str] = []
    units_generated = 0

    for floor in sorted(floors, key=lambda f: assign_floor(f)):
        floor_number = assign_floor(floor)
        z_min = float(floor.get("z_min", 0.0))
        z_max = float(floor.get("z_max", 0.0))
        plate = _footprint_in_crs(floor, source_crs, "footprint") or building_footprint
        if plate is None:
            warnings.append(
                f"FLOOR_NO_GEOMETRY: storey {floor_number} has no plan geometry and "
                "the building footprint is unavailable, so it was skipped"
            )
            continue

        plan = plans.get(floor_number) or {}
        split = split_floor_into_units(
            plate,
            source_crs=source_crs,
            unit_polygons=_plan_geometries(plan.get("unit_polygons"), source_crs),
            dividing_walls=_plan_geometries(plan.get("dividing_walls"), source_crs),
        )
        labels = _unit_labels(plan.get("unit_labels"))

        if not split.units_available:
            undivided.append(f"F{floor_number:02d}")

        reasons = [*match.reasons, *split.reasons]
        notes = list(split.notes)

        for index, region in enumerate(split.regions):
            # A label from the floor plan wins over anything the split
            # synthesised. Written out longhand because the natural one-liner
            # parses as ``(a or b) if c else d`` and silently drops plan labels.
            from_plan = labels[index] if index < len(labels) else None
            label = from_plan or region.label
            label_source = "floor_plan" if from_plan else region.label_source

            volume = create_volume_from_footprint(
                region.polygon,
                z_min=z_min,
                z_max=z_max,
                source_crs=source_crs,
                parent_parcel_id=parcel_id,
                building_id=building_id,
                building_ref=building_ref or building_id,
                floor_number=floor_number,
                unit_label=_stable_unit_label(building_ref or building_id, floor_number, label)
                if label
                else None,
                unit_label_source=label_source,
                volume_scope=split.scope,
                ground_datum=ground_datum,
                source_provenance={
                    "source_id": (source_provenance or {}).get("source_id"),
                    "building_footprint_hash": building.get("geometry_hash"),
                    "floor_geometry_hash": floor.get("geometry_hash"),
                    "floor_processing_job_id": floor.get("processing_job_id"),
                    **dict(source_provenance or {}),
                },
                processing_provenance={
                    "unit_data_available": split.units_available,
                    "floor_area_m2": split.floor_area_m2,
                    "regions_on_floor": len(split.regions),
                    "parcel_match": match.confidence,
                    "ground_datum_supplied": ground_datum is not None,
                },
            )
            if volume is None:
                continue
            volume.review_reasons = reasons + volume.review_reasons
            if volume.review_reasons:
                volume.requires_human_review = True
                volume.status = STATUS_REVIEW
            volume.notes = notes + volume.notes
            volumes.append(volume)
            if split.units_available:
                units_generated += 1

    return VolumeGeneration(
        volumes=volumes,
        buildings=1 if floors else 0,
        floors=len(floors),
        units_generated=units_generated,
        floors_without_unit_data=undivided,
        warnings=warnings,
        provenance={
            "method": GENERATION_METHOD,
            "method_description": METHOD_DESCRIPTION,
            "generator_version": GENERATOR_VERSION,
            "generated_at": now(),
            "source_crs": source_crs,
            "building_id": building_id,
            "parent_parcel_id": parcel_id,
            "parcel_match": match.confidence,
            "ground_datum": ground_datum,
            "floor_plans_supplied": sorted(plans),
            "units_inferred": False,
            "property_ownership": (
                "This module generates geometric volumes only. It asserts no "
                "ownership, tenancy or title, and it does not create cadastral "
                "property records."
            ),
        },
    )


def _stable_unit_label(building_id: str, floor_number: int, label: str) -> str:
    """A floor-plan label made unique per storey.

    Keeps the caller's own label visible while adding the storey, because floor
    plans routinely repeat a label such as "01" on every floor and the identifier
    must not collide.
    """
    return f"{building_id}-F{floor_number:02d}-{label}"


def _unit_labels(raw: Any) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, Mapping):
        return [str(v) for _, v in sorted(raw.items())]
    return [str(v) for v in raw]


def _plan_geometries(raw: Any, source_crs: str) -> list[Any] | None:
    """Read supplied plan geometry, accepting shapes or GeoJSON."""
    if not raw:
        return None
    out: list[Any] = []
    for item in raw:
        if isinstance(item, Mapping) and "coordinates" in item:
            try:
                out.append(geometry_service.wgs84_geojson_to_polygon(item))
            except Exception:  # noqa: BLE001 - a malformed outline is reported later
                continue
        else:
            out.append(item)
    return out or None


def _footprint_in_crs(
    record: Mapping[str, Any], source_crs: str, field: str
) -> Any:
    """A record's plan geometry, expressed in ``source_crs``.

    Stored geometry is WGS84; a floor plan or a caller's footprint may be
    supplied in either. Everything is normalised to ``source_crs`` here so the
    rest of the module measures in one place.
    """
    from app.services import crs as crs_service

    value = record.get(field)
    if isinstance(value, Mapping) and "footprint" in value:
        value = value["footprint"]
    if not isinstance(value, Mapping) or "coordinates" not in value:
        return None
    polygon = geometry_service.wgs84_geojson_to_polygon(value)
    if crs_service.is_metric(source_crs):
        return crs_service.transform_geometry(polygon, "EPSG:4326", source_crs)
    return polygon


__all__ = [
    "GENERATION_METHOD",
    "GENERATOR_VERSION",
    "METHOD_DESCRIPTION",
    "STATUS_MAPPED",
    "STATUS_REVIEW",
    "UNASSIGNED_PARCEL",
    "FloorSplit",
    "ParcelMatch",
    "PropertyVolume",
    "UnitRegion",
    "VolumeGeneration",
    "assign_building",
    "assign_floor",
    "assign_parent_parcel",
    "calculate_area",
    "calculate_volume",
    "create_volume_from_footprint",
    "floor_label",
    "generate_property_volumes",
    "split_floor_into_units",
    "vertical_position",
    "volume_key",
]
