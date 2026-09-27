"""Deterministic demo scene.

Builds the synthetic ``P-001`` / ``B-001`` city: 8 floors, 16 apartments, one
basement and one underground utility corridor (17 property volumes).

.. warning::
   Three geometry conflicts are **intentional showcase data**, not defects:

   1. a floor-2 apartment overlap,
   2. a floor-5 apartment extending past the parcel boundary,
   3. the utility corridor intersecting the basement.

   The demo workflow in the README focuses the viewer on these findings, so
   these coordinates are load-bearing. Changing them is a regression.

Seeding replaces the entire store, then assigns stable identity and captures
version 1 of each geometry, so a freshly seeded scene always reports the same
three issues and is immediately queryable by history.
"""
from __future__ import annotations

from typing import Any


from app.models.enums import (
    InfrastructureType,
    PropertyStatus,
    PropertyType,
    SourceType,
)
from app.repositories.base import CadastreRepository
from app.services.geometry import (
    GEOGRAPHIC_CRS,
    PROCESSING_CRS,
    calculate_area,
    calculate_geometry_hash,
    create_rectangle,
    make_volume,
    polygon_to_geojson,
    record_to_polygon,
)
from app.services.geometry_versioning import (
    INITIAL_CHANGE_REASON,
    VERSIONED_COLLECTIONS,
    snapshot_geometry,
)
from app.services.ulpin import assign_ulpins
from app.services.validation import validate
from app.utils import now

PARCEL_BUSINESS_ID = "P-001"
BUILDING_BUSINESS_ID = "B-001"

PARCEL_CELLS = (0, 0, 80, 52)
BUILDING_CELLS = (10, 8, 70, 44)
UTILITY_CELLS = (18, 21, 62, 25)

FLOOR_COUNT = 8
FLOOR_HEIGHT = 3.2
BUILDING_HEIGHT = 25.6

BASEMENT_Z_MIN, BASEMENT_Z_MAX = -3.2, 0
UTILITY_Z_MIN, UTILITY_Z_MAX = -2.2, 0.0

#: Retired. These were literals, not measurements, and were being persisted onto
#: seeded buildings and property volumes and served by ``GET /buildings`` and
#: ``GET /properties``. Kept as names so an import of them fails loudly rather
#: than silently resolving to nothing:
_RETIRED_CONFIDENCE_LITERAL = (
    "a confidence literal is not a measurement; the honest score is "
    "geometric_quality, a regularity measure of the recovered shape"
)

#: Floors carrying a deliberate geometry conflict.
CONFLICT_OVERLAP_FLOOR = 2
CONFLICT_ENCROACHMENT_FLOOR = 5

DEMO_SOURCE_ID = "DS-001"


def _make_parcel(timestamp: str) -> dict[str, Any]:
    poly = create_rectangle(*PARCEL_CELLS)
    return {
        "id": "parcel-001",
        "parcel_id": PARCEL_BUSINESS_ID,
        "prototype_ulpin": None,
        "geometry": polygon_to_geojson(poly),
        "area": calculate_area(poly),
        "land_use": "Mixed residential",
        "survey_reference": "Demo Ward 17 / Sheet 04",
        "created_at": timestamp,
        "updated_at": timestamp,
        "version": 1,
    }


def _make_building(timestamp: str) -> dict[str, Any]:
    poly = create_rectangle(*BUILDING_CELLS)
    return {
        "id": "building-001",
        "building_id": BUILDING_BUSINESS_ID,
        "parcel_id": PARCEL_BUSINESS_ID,
        "footprint": polygon_to_geojson(poly),
        "height": BUILDING_HEIGHT,
        "floor_count": FLOOR_COUNT,
        "geometry_3d": {
            "z_min": 0,
            "z_max": BUILDING_HEIGHT,
            "footprint": polygon_to_geojson(poly),
        },
        "created_at": timestamp,
        "updated_at": timestamp,
        "version": 1,
    }


def _make_floors() -> list[dict[str, Any]]:
    """Storey bands for ``B-001``.

    Basement levels are deliberately excluded: the public ``/floors`` contract has
    always reported floors 1..8 only, and this keeps the seeded data consistent
    with that response.
    """
    return [
        {
            "id": f"FLOOR-{f}",
            "building_id": BUILDING_BUSINESS_ID,
            "floor_number": f,
            "z_min": (f - 1) * FLOOR_HEIGHT,
            "z_max": f * FLOOR_HEIGHT,
            "version": 1,
        }
        for f in range(1, FLOOR_COUNT + 1)
    ]


def _make_property_volumes() -> list[dict[str, Any]]:
    building_poly = create_rectangle(*BUILDING_CELLS)
    props: list[dict[str, Any]] = [
        make_volume(
            ident="PV-B001",
            parcel=PARCEL_BUSINESS_ID,
            building=BUILDING_BUSINESS_ID,
            prop_type=PropertyType.BASEMENT_PARKING.value,
            floor=-1,
            label="Basement Parking",
            footprint=building_poly,
            zmin=BASEMENT_Z_MIN,
            zmax=BASEMENT_Z_MAX,
        )
    ]
    for floor in range(1, FLOOR_COUNT + 1):
        zmin, zmax = (floor - 1) * FLOOR_HEIGHT, floor * FLOOR_HEIGHT
        left = create_rectangle(10, 8, 40, 44)
        right = create_rectangle(40, 8, 70, 44)
        # Deliberate floor 2 overlap and floor 5 parcel-boundary encroachment.
        if floor == CONFLICT_OVERLAP_FLOOR:
            right = create_rectangle(35, 8, 70, 44)
        if floor == CONFLICT_ENCROACHMENT_FLOOR:
            right = create_rectangle(40, 8, 86, 44)
        props.extend(
            [
                make_volume(
                    ident=f"PV-{floor}01",
                    parcel=PARCEL_BUSINESS_ID,
                    building=BUILDING_BUSINESS_ID,
                    prop_type=PropertyType.APARTMENT.value,
                    floor=floor,
                    label=f"Apartment {floor}01",
                    footprint=left,
                    zmin=zmin,
                    zmax=zmax,
                        ),
                make_volume(
                    ident=f"PV-{floor}02",
                    parcel=PARCEL_BUSINESS_ID,
                    building=BUILDING_BUSINESS_ID,
                    prop_type=PropertyType.APARTMENT.value,
                    floor=floor,
                    label=f"Apartment {floor}02",
                    footprint=right,
                    zmin=zmin,
                    zmax=zmax,
                            status=(
                        PropertyStatus.HUMAN_REVIEW_REQUIRED.value
                        if floor == CONFLICT_ENCROACHMENT_FLOOR
                        else PropertyStatus.MAPPED.value
                    ),
                ),
            ]
        )
    return props


def _make_infrastructure(timestamp: str) -> dict[str, Any]:
    return {
        "id": "INF-U-001",
        "type": InfrastructureType.UNDERGROUND_UTILITY_CORRIDOR.value,
        "geometry_3d": polygon_to_geojson(create_rectangle(*UTILITY_CELLS)),
        "z_min": UTILITY_Z_MIN,
        "z_max": UTILITY_Z_MAX,
        "owner": "Demo Municipal Utility Cell",
        "reference": "UG-UTIL-17",
        "created_at": timestamp,
        "updated_at": timestamp,
        "version": 1,
    }


def _make_sources(timestamp: str) -> list[dict[str, Any]]:
    return [
        {
            "id": DEMO_SOURCE_ID,
            "source_type": SourceType.SYNTHETIC_GEOJSON.value,
            "filename": "demo_city.geojson",
            # The demo geometry is authored in local projected metres and served
            # as WGS84 GeoJSON, so that is the source CRS of record.
            "crs": GEOGRAPHIC_CRS,
            "acquisition_date": "2026-09-01",
            "metadata": {
                "generated": True,
                "source_crs": GEOGRAPHIC_CRS,
                "processing_crs": PROCESSING_CRS,
                "crs_declared_in_payload": True,
            },
            # Set explicitly so the in-memory store returns the same shape as
            # PostGIS, where the column defaults server-side.
            "created_at": timestamp,
        }
    ]


def seed_demo(repo: CadastreRepository) -> dict[str, Any]:
    """Replace the store with the synthetic demo city and validate it.

    Works against either backend: the repository contract is identical, so the
    PostGIS path writes the same records the demo store holds.
    """
    timestamp = now()
    props = _make_property_volumes()

    repo.replace_all(
        {
            "parcels": [_make_parcel(timestamp)],
            "buildings": [_make_building(timestamp)],
            "floors": _make_floors(),
            "properties": props,
            "infrastructure": [_make_infrastructure(timestamp)],
            "issues": [],
            "sources": _make_sources(timestamp),
            "geometry_versions": [],
            "processing_jobs": [],
            "extracted_buildings": [],
            "generated_property_volumes": [],
            "extracted_floors": [],
            # The review and audit collections start empty on purpose: the demo
            # shows the *unreviewed* state, which is what a fresh load of real
            # data looks like. An empty log is also the honest starting point --
            # seeding fabricated approvals would put decisions in a cadastral
            # trail that nobody actually made.
            "review_cases": [],
            "review_decisions": [],
            "audit_events": [],
            "cadastral_changes": [],
            "provenance_links": [],
        }
    )

    # Seed records carry geometry but no identifier or version yet. Assign the
    # stable identity first, then capture version 1 of each geometry so history
    # and comparison work from the moment the scene is loaded.
    assign_ulpins(repo)
    for collection in VERSIONED_COLLECTIONS:
        for record in repo.records(collection):
            z_min = record.get("z_min")
            z_max = record.get("z_max")
            changes: dict[str, Any] = {
                "geometry_version": record.get("geometry_version", 1)
            }
            polygon = record_to_polygon(record)
            changes["geometry_hash"] = calculate_geometry_hash(
                polygon,
                float(z_min) if z_min is not None else 0.0,
                float(z_max) if z_max is not None else 0.0,
            )
            repo.update(collection, record["id"], changes)
            snapshot_geometry(
                repo,
                record["id"],
                version=1,
                source_id=DEMO_SOURCE_ID,
                change_reason=INITIAL_CHANGE_REASON,
            )

    validate(repo)
    return {
        "message": "Demo City loaded",
        "properties": len(props),
        "issues": len(repo.records("issues")),
    }
