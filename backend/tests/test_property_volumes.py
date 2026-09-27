"""Tests for vertical property-volume generation.

The load-bearing properties under test:

* the required output fields are all present on every volume;
* **apartment boundaries are never invented** -- with no floor-plan data the
  output is explicitly a storey volume, and ``units_inferred`` is false always;
* with floor-plan data the storey *is* divided, and the unit areas sum to the
  storey area;
* the identifier is **stable** and unique, and does not change when geometry does;
* the scenarios asked for: one floor, multiple units, basement, underground
  volume, elevated volume;
* **no ownership is asserted** and the cadastral property table is untouched.
"""
from __future__ import annotations

import numpy as np
import pytest

from app.models.enums import PropertyType, VolumeScope
from app.services import geometry as geometry_service
from app.services import property_volumes as pv
from tests.fixtures import buildings as fx

CRS = "EPSG:32643"
#: A small UTM-space footprint: 20 m x 10 m, anchored near the demo origin.
PLATE = geometry_service.create_rectangle(
    fx.ORIGIN_E, fx.ORIGIN_N, fx.ORIGIN_E + 20.0, fx.ORIGIN_N + 10.0
)
PLATE_AREA = 200.0


def wgs84(geometry):
    """A plate as WGS84 GeoJSON, the shape callers and the store use."""
    from app.services import crs as crs_service

    projected = crs_service.transform_geometry(
        geometry, CRS, geometry_service.GEOGRAPHIC_CRS
    )
    return {
        "type": projected.geom_type,
        "coordinates": [[list(c) for c in projected.exterior.coords]],
    }


def wall(x: float, y0: float = 0.0, y1: float = 10.0):
    from shapely.geometry import LineString

    return LineString(
        [
            (fx.ORIGIN_E + x, fx.ORIGIN_N + y0),
            (fx.ORIGIN_E + x, fx.ORIGIN_N + y1),
        ]
    )


def a_parcel():
    """A parcel comfortably containing the plate, in WGS84."""
    bigger = geometry_service.create_rectangle(
        fx.ORIGIN_E - 20, fx.ORIGIN_N - 20, fx.ORIGIN_E + 40, fx.ORIGIN_N + 30
    )
    return {
        "parcel_id": "P-TEST",
        "id": "parcel-test",
        "geometry": wgs84(bigger),
    }


def a_building():
    return {
        "id": "XB-JOB1-001",
        "source_id": "DS-TEST",
        "footprint": wgs84(PLATE),
        "height_m": 10.5,
        "geometry_hash": "BUILDINGHASH",
    }


def a_floor(number: int, z_min: float, z_max: float, plate=PLATE):
    return {
        "floor_number": number,
        "z_min": z_min,
        "z_max": z_max,
        "footprint": wgs84(plate),
        "geometry_hash": f"FLOOR{number}HASH",
        "processing_job_id": "JOB-SEG",
    }


# ==========================================================================
# split_floor_into_units: the honesty rule
# ==========================================================================


def test_no_unit_data_yields_the_whole_plate_as_one_floor_region():
    split = pv.split_floor_into_units(PLATE, source_crs=CRS)
    assert split.units_available is False
    assert split.units_inferred is False
    assert split.scope == VolumeScope.FLOOR
    assert len(split.regions) == 1
    assert split.regions[0].polygon.area == pytest.approx(PLATE_AREA)
    assert split.regions[0].label is None
    assert any("NO_UNIT_DATA" in note for note in split.notes)


def test_no_unit_data_never_invents_a_boundary():
    """The plate comes back untouched: one region, the same area."""
    split = pv.split_floor_into_units(PLATE, source_crs=CRS)
    assert split.units_inferred is False
    assert len(split.regions) == 1
    assert split.regions[0].area_m2 == pytest.approx(PLATE_AREA)


def test_dividing_walls_do_divide_the_plate():
    split = pv.split_floor_into_units(
        PLATE, source_crs=CRS, dividing_walls=[wall(10.0)]
    )
    assert split.units_available is True
    assert split.scope == VolumeScope.UNIT
    assert len(split.regions) == 2
    for region in split.regions:
        assert region.area_m2 == pytest.approx(PLATE_AREA / 2, rel=0.01)


def test_unit_polygons_do_divide_the_plate():
    from shapely.geometry import box

    left = box(fx.ORIGIN_E, fx.ORIGIN_N, fx.ORIGIN_E + 8, fx.ORIGIN_N + 10)
    right = box(fx.ORIGIN_E + 8, fx.ORIGIN_N, fx.ORIGIN_E + 20, fx.ORIGIN_N + 10)
    split = pv.split_floor_into_units(
        PLATE, source_crs=CRS, unit_polygons=[left, right]
    )
    assert split.units_available is True
    assert len(split.regions) == 2
    assert sum(r.area_m2 for r in split.regions) == pytest.approx(PLATE_AREA, rel=0.01)


def test_unit_areas_always_sum_to_the_plate_area():
    cases = [
        {"dividing_walls": [wall(10.0)]},
        {"dividing_walls": [wall(10.0), wall(15.0, 0.0, 5.0)]},
        {"dividing_walls": [wall(6.0), wall(12.0)]},
        {"dividing_walls": [wall(10.0), wall(10.0)]},
        {"dividing_walls": [wall(50.0)]},
        {"dividing_walls": []},
    ]
    for kwargs in cases:
        split = pv.split_floor_into_units(PLATE, source_crs=CRS, **kwargs)
        total = sum(r.area_m2 for r in split.regions)
        assert total == pytest.approx(PLATE_AREA, rel=0.005), kwargs


def test_subdivision_is_independent_of_wall_order():
    """The same walls must give the same regions whatever order they arrive in.

    Repeated two-way splitting is order-dependent: measured on a T-plan it gave
    three regions in one order and two in the other. A planar subdivision is not,
    and this is the property that would otherwise change the unit count between
    runs over the same plan.
    """
    from shapely.geometry import LineString

    spine = wall(10.0)
    branch = LineString(
        [
            (fx.ORIGIN_E + 10, fx.ORIGIN_N + 5),
            (fx.ORIGIN_E + 20, fx.ORIGIN_N + 5),
        ]
    )
    forward = pv.split_floor_into_units(
        PLATE, source_crs=CRS, dividing_walls=[spine, branch]
    )
    reverse = pv.split_floor_into_units(
        PLATE, source_crs=CRS, dividing_walls=[branch, spine]
    )
    assert len(forward.regions) == len(reverse.regions) == 3
    assert sorted(r.area_m2 for r in forward.regions) == sorted(
        r.area_m2 for r in reverse.regions
    )
    assert sum(r.area_m2 for r in forward.regions) == pytest.approx(
        PLATE_AREA, rel=0.005
    )


def test_collinear_overlapping_walls_do_not_multiply_regions():
    """A wall drawn twice, or in two overlapping pieces, is still one wall."""
    split = pv.split_floor_into_units(
        PLATE, source_crs=CRS, dividing_walls=[wall(10.0), wall(10.0, 0.0, 6.0)]
    )
    assert len(split.regions) == 2
    assert sum(r.area_m2 for r in split.regions) == pytest.approx(PLATE_AREA, rel=0.005)


def test_walls_that_do_not_divide_leave_the_plate_whole():
    """A stub wall is not extended into an invented division."""
    from shapely.geometry import LineString

    stub = LineString(
        [
            (fx.ORIGIN_E + 8, fx.ORIGIN_N + 3),
            (fx.ORIGIN_E + 12, fx.ORIGIN_N + 7),
        ]
    )
    split = pv.split_floor_into_units(
        PLATE, source_crs=CRS, dividing_walls=[stub]
    )
    assert len(split.regions) == 1
    # Walls were supplied, so the plate is not "no data"; but they did not divide
    # it, so the result is still a single storey-scoped region.
    assert split.units_available is False
    assert any("WALLS_PRODUCED_NO_DIVISION" in r for r in split.reasons)


def test_a_divider_missing_the_boundary_by_a_hair_still_divides():
    """The division must not be lost to a sub-millimetre numerical gap.

    A surface that has been round-tripped through WGS84 no longer shares exact
    coordinates with a hand-drawn plan wall, and an unsnapped subdivision then
    returns the whole plate -- a missing division disguised as an undivided
    storey.
    """
    off = 0.0005  # half a millimetre
    hairline = geometry_service.create_rectangle(
        fx.ORIGIN_E - off, fx.ORIGIN_N - off, fx.ORIGIN_E + 20 + off, fx.ORIGIN_N + 10 + off
    )
    split = pv.split_floor_into_units(
        hairline, source_crs=CRS, dividing_walls=[wall(10.0, 0.0, 10.0)]
    )
    assert len(split.regions) == 2
    assert sum(r.area_m2 for r in split.regions) == pytest.approx(
        hairline.area, rel=0.005
    )


def test_a_wall_plan_survives_a_wgs84_round_trip():
    """The case the snapping exists for, end to end.

    A plate stored as WGS84 and read back no longer lines up exactly with walls
    authored in projected coordinates, and the division must still happen.
    """
    from app.services import crs as crs_service

    stored = wgs84(PLATE)
    recovered = crs_service.transform_geometry(
        geometry_service.wgs84_geojson_to_polygon(stored),
        "EPSG:4326",
        CRS,
    )
    split = pv.split_floor_into_units(
        recovered, source_crs=CRS, dividing_walls=[wall(10.0)]
    )
    assert len(split.regions) == 2


def test_a_unit_outside_the_plate_is_reported_not_silently_kept():
    from shapely.geometry import box

    inside = box(fx.ORIGIN_E, fx.ORIGIN_N, fx.ORIGIN_E + 10, fx.ORIGIN_N + 10)
    outside = box(fx.ORIGIN_E + 200, fx.ORIGIN_N, fx.ORIGIN_E + 210, fx.ORIGIN_N + 10)
    split = pv.split_floor_into_units(
        PLATE, source_crs=CRS, unit_polygons=[inside, outside]
    )
    assert len(split.regions) == 1
    assert any("UNIT_OUTSIDE_FLOOR" in r for r in split.reasons)


def test_a_unit_overshooting_is_clipped_to_the_plate():
    from shapely.geometry import box

    overshoot = box(fx.ORIGIN_E - 5, fx.ORIGIN_N, fx.ORIGIN_E + 10, fx.ORIGIN_N + 10)
    split = pv.split_floor_into_units(
        PLATE, source_crs=CRS, unit_polygons=[overshoot]
    )
    assert split.regions[0].area_m2 == pytest.approx(100.0, rel=0.01)
    assert any("UNIT_CLIPPED" in r for r in split.reasons)


def test_an_empty_plate_is_refused():
    empty = geometry_service.create_rectangle(0, 0, 0, 0)
    split = pv.split_floor_into_units(empty, source_crs=CRS)
    assert split.regions == []
    assert any("ZERO_AREA" in r for r in split.reasons)


# ==========================================================================
# Measurement
# ==========================================================================


def test_area_is_measured_in_metres_not_degrees():
    area = pv.calculate_area(PLATE, source_crs=CRS)
    assert area == pytest.approx(PLATE_AREA, rel=0.001)
    # A square-degree reading would be ~1e-9, off by ten orders of magnitude.
    assert area > 1.0


def test_volume_is_area_times_height():
    volume = pv.calculate_volume(PLATE, 100.0, 103.5, source_crs=CRS)
    assert volume == pytest.approx(PLATE_AREA * 3.5, rel=0.001)


def test_an_inverted_band_has_no_volume():
    assert pv.calculate_volume(PLATE, 10.0, 5.0, source_crs=CRS) == 0.0


# ==========================================================================
# Assignment
# ==========================================================================


def test_footprint_inside_one_parcel_is_contained():
    match = pv.assign_parent_parcel(PLATE, [a_parcel()], source_crs=CRS)
    assert match.parcel_id == "P-TEST"
    assert match.confidence == "contained"
    assert match.reasons == []


def test_a_footprint_overlapping_one_parcel_is_flagged():
    overlapping = geometry_service.create_rectangle(
        fx.ORIGIN_E - 40, fx.ORIGIN_N - 20, fx.ORIGIN_E + 10, fx.ORIGIN_N + 30
    )
    parcel = {
        "parcel_id": "P-OVER",
        "geometry": wgs84(overlapping),
    }
    match = pv.assign_parent_parcel(PLATE, [parcel], source_crs=CRS)
    assert match.parcel_id == "P-OVER"
    assert match.confidence == "overlapping"
    assert any("PARTIAL_OVERLAP" in r for r in match.reasons)


def test_a_footprint_matching_no_parcel_is_flagged_not_guessed():
    match = pv.assign_parent_parcel(PLATE, [], source_crs=CRS)
    assert match.parcel_id is None
    assert match.confidence == "none"
    assert any("NO_PARCEL_MATCH" in r for r in match.reasons)


def test_a_footprint_inside_two_parcels_is_ambiguous():
    inner = {
        "parcel_id": "P-A",
        "geometry": wgs84(PLATE),
    }
    outer = {
        "parcel_id": "P-B",
        "geometry": wgs84(
            geometry_service.create_rectangle(
                fx.ORIGIN_E - 5, fx.ORIGIN_N - 5, fx.ORIGIN_E + 25, fx.ORIGIN_N + 15
            )
        ),
    }
    match = pv.assign_parent_parcel(PLATE, [inner, outer], source_crs=CRS)
    assert match.confidence == "ambiguous"
    assert any("MULTIPLE_PARCELS" in r for r in match.reasons)


def test_building_and_floor_assignment():
    assert pv.assign_building({"id": "XB-1"}) == "XB-1"
    assert pv.assign_building(None, building_id="XB-2") == "XB-2"
    assert pv.assign_building(None) == pv.UNASSIGNED_PARCEL
    assert pv.assign_floor({"floor_number": 3}) == 3
    assert pv.assign_floor({}, fallback=1) == 1
    assert pv.assign_floor(None) == 1


def test_floor_labels_follow_convention():
    assert pv.floor_label(-1) == "B1"
    assert pv.floor_label(0) == "B1"
    assert pv.floor_label(1) == "GF"
    assert pv.floor_label(4) == "4"


def test_vertical_position_needs_a_datum_to_be_meaningful():
    assert pv.vertical_position(0.0, -3.0, 0.0) == pv.POSITION_UNDERGROUND
    assert pv.vertical_position(-1.0, 1.0, 0.0) == pv.POSITION_AT_GRADE
    assert pv.vertical_position(3.0, 6.0, 0.0) == pv.POSITION_ELEVATED
    assert pv.vertical_position(3.0, 6.0, None) == "UNDETERMINED"


# ==========================================================================
# create_volume_from_footprint: the output contract
# ==========================================================================


def _volume(**kwargs):
    defaults = dict(
        z_min=100.0,
        z_max=103.5,
        source_crs=CRS,
        parent_parcel_id="P-TEST",
        building_id="XB-1",
        floor_number=1,
        ground_datum=0.0,
    )
    defaults.update(kwargs)
    return pv.create_volume_from_footprint(PLATE, **defaults)


def test_volume_carries_every_required_field():
    volume = _volume()
    record = volume.to_record()
    # stable ULPIN
    assert record["prototype_ulpin"].startswith("VC-VP-")
    # parent parcel, building, floor
    assert record["parent_parcel_id"] == "P-TEST"
    assert record["building_id"] == "XB-1"
    assert record["floor_number"] == 1
    assert record["floor_label"] == "GF"
    # unit label where available
    assert record["unit_label"] is None
    assert record["unit_label_source"] == "synthesised"
    # z range, geometry, area, volume
    assert record["z_min"] == 100.0 and record["z_max"] == 103.5
    # A bare GeoJSON polygon, matching the cadastral property-volume contract.
    assert record["geometry_3d"]["type"] == "Polygon"
    assert record["geometry_3d"]["coordinates"][0]
    assert record["area_m2"] == pytest.approx(PLATE_AREA, rel=0.01)
    assert record["volume_m3"] == pytest.approx(PLATE_AREA * 3.5, rel=0.01)
    # geometry hash and version
    assert len(record["geometry_hash"]) == 12
    assert record["geometry_version"] == 1
    # status
    assert record["status"] == pv.STATUS_MAPPED
    assert record["requires_human_review"] is False
    # both provenances
    assert record["source_provenance"]["source_crs"] == CRS
    assert "algorithm" in record["processing_provenance"]


def test_units_inferred_is_always_false():
    assert _volume().units_inferred is False
    assert _volume(volume_scope=VolumeScope.UNIT, unit_label="A").units_inferred is False


def test_no_confidence_or_ownership_field_is_produced():
    record = _volume().to_record()
    for forbidden in ("confidence", "owner", "ownership", "tenant", "title", "accuracy"):
        assert forbidden not in record
    text = repr(record).lower()
    assert "'confidence'" not in text


def test_a_floor_scope_volume_is_never_labelled_a_unit():
    volume = _volume()
    assert volume.volume_scope == VolumeScope.FLOOR
    assert volume.property_type == PropertyType.FLOOR
    assert volume.unit_label is None


def test_a_basement_is_typed_as_a_basement():
    volume = _volume(z_min=-3.0, z_max=0.0, floor_number=-1, ground_datum=0.0)
    assert volume.property_type == PropertyType.BASEMENT
    assert volume.vertical_position == pv.POSITION_UNDERGROUND
    assert volume.floor_label == "B1"


def test_a_missing_datum_is_flagged_not_guessed():
    volume = _volume(ground_datum=None)
    assert volume.vertical_position == "UNDETERMINED"
    assert volume.requires_human_review is True
    assert any("DATUM_UNKNOWN" in r for r in volume.review_reasons)
    assert volume.status == pv.STATUS_REVIEW


def test_an_inverted_band_produces_no_volume():
    assert _volume(z_min=10.0, z_max=5.0) is None


def test_a_zero_area_footprint_produces_no_volume():
    empty = geometry_service.create_rectangle(0, 0, 0, 0)
    assert (
        pv.create_volume_from_footprint(
            empty,
            z_min=0.0,
            z_max=3.0,
            source_crs=CRS,
            parent_parcel_id="P",
            building_id="B",
            floor_number=1,
        )
        is None
    )


# ==========================================================================
# Identifier stability
# ==========================================================================


def test_identifier_is_deterministic_across_runs():
    assert _volume().prototype_ulpin == _volume().prototype_ulpin


def test_identifier_survives_a_geometry_change():
    """Identity is derived from stable attributes, not from geometry."""
    smaller = geometry_service.create_rectangle(
        fx.ORIGIN_E, fx.ORIGIN_N, fx.ORIGIN_E + 12, fx.ORIGIN_N + 8
    )
    first = _volume()
    second = pv.create_volume_from_footprint(
        smaller,
        z_min=100.0,
        z_max=103.5,
        source_crs=CRS,
        parent_parcel_id="P-TEST",
        building_id="XB-1",
        floor_number=1,
    )
    assert first.prototype_ulpin == second.prototype_ulpin
    assert first.geometry_hash != second.geometry_hash


def test_identifier_differs_per_floor():
    first = _volume(floor_number=1)
    second = _volume(floor_number=2)
    assert first.prototype_ulpin != second.prototype_ulpin


def test_repeated_unit_labels_on_different_floors_do_not_collide():
    """Floor plans routinely label every floor's first unit "01"."""
    a = _volume(floor_number=1, unit_label="XB-1-F01-01")
    b = _volume(floor_number=2, unit_label="XB-1-F02-01")
    assert a.prototype_ulpin != b.prototype_ulpin


def test_identifier_is_scoped_to_the_parent_parcel():
    a = _volume(parent_parcel_id="P-A")
    b = _volume(parent_parcel_id="P-B")
    assert a.prototype_ulpin != b.prototype_ulpin


def test_volume_key_includes_the_floor():
    key = pv.volume_key("XB-1", 3, "01")
    assert "F03" in key and key.endswith("01")


# ==========================================================================
# generate_property_volumes
# ==========================================================================


def test_one_floor_with_no_unit_data_yields_one_floor_volume():
    result = pv.generate_property_volumes(
        a_building(),
        [a_floor(1, 100.0, 103.5)],
        source_crs=CRS,
        parcels=[a_parcel()],
        ground_datum=0.0,
    )
    assert len(result.volumes) == 1
    volume = result.volumes[0]
    assert volume.volume_scope == VolumeScope.FLOOR
    assert volume.unit_label is None
    assert volume.units_inferred is False
    assert result.floors_without_unit_data == ["F01"]
    assert result.units_generated == 0
    assert any("NO_UNIT_DATA" in n for n in volume.notes)


def test_multiple_units_on_a_floor_are_generated_from_a_plan():
    result = pv.generate_property_volumes(
        a_building(),
        [a_floor(1, 100.0, 103.5)],
        source_crs=CRS,
        parcels=[a_parcel()],
        ground_datum=0.0,
        floor_plans={
            1: {
                "dividing_walls": [wall(10.0)],
                "unit_labels": ["01", "02"],
            }
        },
    )
    assert len(result.volumes) == 2
    assert result.units_generated == 2
    assert result.floors_without_unit_data == []
    for volume in result.volumes:
        assert volume.volume_scope == VolumeScope.UNIT
        assert volume.property_type == PropertyType.UNIT
        assert volume.units_inferred is False
    labels = sorted(v.unit_label for v in result.volumes)
    assert labels == ["XB-JOB1-001-F01-01", "XB-JOB1-001-F01-02"]
    assert len({v.prototype_ulpin for v in result.volumes}) == 2


def test_three_units_from_a_two_wall_plan():
    result = pv.generate_property_volumes(
        a_building(),
        [a_floor(1, 100.0, 103.5)],
        source_crs=CRS,
        parcels=[a_parcel()],
        ground_datum=0.0,
        floor_plans={1: {"dividing_walls": [wall(7.0), wall(14.0)]}},
    )
    assert len(result.volumes) == 3
    assert len({v.prototype_ulpin for v in result.volumes}) == 3


def test_unit_volumes_preserve_the_storey_area():
    result = pv.generate_property_volumes(
        a_building(),
        [a_floor(1, 100.0, 103.5)],
        source_crs=CRS,
        parcels=[a_parcel()],
        ground_datum=0.0,
        floor_plans={1: {"dividing_walls": [wall(10.0)]}},
    )
    assert result.total_area_m2 == pytest.approx(PLATE_AREA, rel=0.01)


def test_a_basement_floor_yields_a_basement_volume():
    result = pv.generate_property_volumes(
        a_building(),
        [a_floor(-1, 97.0, 100.0)],
        source_crs=CRS,
        parcels=[a_parcel()],
        ground_datum=100.0,
    )
    assert len(result.volumes) == 1
    volume = result.volumes[0]
    assert volume.floor_number == -1
    assert volume.floor_label == "B1"
    assert volume.property_type == PropertyType.BASEMENT
    assert volume.vertical_position == pv.POSITION_UNDERGROUND
    assert volume.z_min == 97.0 and volume.z_max == 100.0


def test_an_underground_volume_sits_below_the_datum():
    result = pv.generate_property_volumes(
        a_building(),
        [a_floor(-2, 90.0, 94.0), a_floor(1, 100.0, 103.5)],
        source_crs=CRS,
        parcels=[a_parcel()],
        ground_datum=100.0,
    )
    by_floor = {v.floor_number: v for v in result.volumes}
    assert by_floor[-2].vertical_position == pv.POSITION_UNDERGROUND
    assert by_floor[-2].property_type == PropertyType.BASEMENT
    assert by_floor[1].vertical_position == pv.POSITION_ELEVATED


def test_an_elevated_volume_sits_above_the_datum():
    result = pv.generate_property_volumes(
        a_building(),
        [a_floor(3, 110.0, 113.5)],
        source_crs=CRS,
        parcels=[a_parcel()],
        ground_datum=100.0,
    )
    volume = result.volumes[0]
    assert volume.vertical_position == pv.POSITION_ELEVATED
    assert volume.floor_number == 3
    assert volume.floor_label == "3"
    assert volume.z_min == 110.0


def test_a_multi_storey_building_gets_one_volume_per_storey():
    floors = [a_floor(n, 100.0 + (n - 1) * 3.5, 100.0 + n * 3.5) for n in range(1, 6)]
    result = pv.generate_property_volumes(
        a_building(),
        floors,
        source_crs=CRS,
        parcels=[a_parcel()],
        ground_datum=0.0,
    )
    assert len(result.volumes) == 5
    assert sorted(v.floor_number for v in result.volumes) == [1, 2, 3, 4, 5]
    assert len({v.prototype_ulpin for v in result.volumes}) == 5
    # Storeys are emitted bottom-up.
    assert [v.floor_number for v in result.volumes] == [1, 2, 3, 4, 5]


def test_floors_are_sorted_by_number_not_input_order():
    floors = [a_floor(3, 110.0, 113.5), a_floor(1, 100.0, 103.5), a_floor(2, 103.5, 107.0)]
    result = pv.generate_property_volumes(
        a_building(), floors, source_crs=CRS, parcels=[a_parcel()], ground_datum=0.0
    )
    assert [v.floor_number for v in result.volumes] == [1, 2, 3]


def test_units_on_one_floor_and_none_on_another():
    result = pv.generate_property_volumes(
        a_building(),
        [a_floor(1, 100.0, 103.5), a_floor(2, 103.5, 107.0)],
        source_crs=CRS,
        parcels=[a_parcel()],
        ground_datum=0.0,
        floor_plans={1: {"dividing_walls": [wall(10.0)]}},
    )
    by_floor: dict[int, list] = {}
    for volume in result.volumes:
        by_floor.setdefault(volume.floor_number, []).append(volume)
    assert len(by_floor[1]) == 2
    assert len(by_floor[2]) == 1
    assert by_floor[2][0].volume_scope == VolumeScope.FLOOR
    assert result.floors_without_unit_data == ["F02"]


def test_generation_is_reproducible():
    kwargs = dict(
        source_crs=CRS, parcels=[a_parcel()], ground_datum=0.0
    )
    first = pv.generate_property_volumes(
        a_building(), [a_floor(1, 100.0, 103.5)], **kwargs
    )
    second = pv.generate_property_volumes(
        a_building(), [a_floor(1, 100.0, 103.5)], **kwargs
    )
    assert [v.prototype_ulpin for v in first.volumes] == [
        v.prototype_ulpin for v in second.volumes
    ]
    assert [v.geometry_hash for v in first.volumes] == [
        v.geometry_hash for v in second.volumes
    ]


def test_an_unmatched_building_is_flagged_on_every_volume():
    result = pv.generate_property_volumes(
        a_building(), [a_floor(1, 100.0, 103.5)], source_crs=CRS, parcels=[]
    )
    volume = result.volumes[0]
    assert volume.parent_parcel_id == pv.UNASSIGNED_PARCEL
    assert volume.requires_human_review is True
    assert any("NO_PARCEL_MATCH" in r for r in volume.review_reasons)


def test_a_floor_without_geometry_is_skipped_with_a_warning():
    headless = {"floor_number": 9, "z_min": 130.0, "z_max": 133.5}
    result = pv.generate_property_volumes(
        a_building(), [headless], source_crs=CRS, parcels=[a_parcel()], ground_datum=0.0
    )
    # The building footprint is used as a fallback plate, so the storey survives.
    assert len(result.volumes) == 1
    assert result.volumes[0].floor_number == 9


def test_run_provenance_asserts_no_ownership():
    result = pv.generate_property_volumes(
        a_building(), [a_floor(1, 100.0, 103.5)], source_crs=CRS, parcels=[a_parcel()]
    )
    assert result.provenance["units_inferred"] is False
    assert "no ownership" in result.provenance["property_ownership"].lower()
    assert "does not create cadastral" in result.provenance["property_ownership"].lower()


# ==========================================================================
# No cadastral side effects
# ==========================================================================


def test_a_reprojected_plate_still_subdivides():
    """The numerical hazard this milestone actually hit, pinned as a test.

    Reprojecting a surface transforms its corners independently, so its edges end
    up tilted by nanometres. A wall drawn in the target CRS then shares no exact
    node with that edge and the subdivision returns the whole plate -- a division
    that vanishes with no error raised. Snapping to a fine grid restores it.
    """
    from app.services import crs as crs_service

    stored = wgs84(PLATE)
    for label in ("one wall", "two walls"):
        walls = (
            [wall(10.0)]
            if label == "one wall"
            else [wall(7.0), wall(14.0)]
        )
        recovered = crs_service.transform_geometry(
            geometry_service.wgs84_geojson_to_polygon(stored),
            "EPSG:4326",
            CRS,
        )
        split = pv.split_floor_into_units(
            recovered, source_crs=CRS, dividing_walls=walls
        )
        expected = 2 if label == "one wall" else 3
        assert len(split.regions) == expected, label
        assert sum(r.area_m2 for r in split.regions) == pytest.approx(
            recovered.area, rel=0.005
        ), label


def test_subdivision_works_at_true_utm_magnitudes():
    """A plate far from the origin subdivides as reliably as one near it.

    Precision degrades with coordinate magnitude, so a geometry at a large
    easting is the worst case for the noding step.
    """
    far = geometry_service.create_rectangle(
        fx.ORIGIN_E + 500_000.0, fx.ORIGIN_N + 400_000.0,
        fx.ORIGIN_E + 500_020.0, fx.ORIGIN_N + 400_010.0,
    )
    from shapely.geometry import LineString

    far_wall = LineString(
        [
            (fx.ORIGIN_E + 500_010.0, fx.ORIGIN_N + 400_000.0),
            (fx.ORIGIN_E + 500_010.0, fx.ORIGIN_N + 400_010.0),
        ]
    )
    split = pv.split_floor_into_units(far, source_crs=CRS, dividing_walls=[far_wall])
    assert len(split.regions) == 2
    assert sum(r.area_m2 for r in split.regions) == pytest.approx(200.0, rel=0.005)


def test_generation_creates_no_cadastral_property_volume():
    from app.repositories.memory import InMemoryRepository
    from app.services.demo import seed_demo

    repo = InMemoryRepository()
    seed_demo(repo)
    before = len(repo.records("properties"))
    pv.generate_property_volumes(
        a_building(),
        [a_floor(1, 100.0, 103.5), a_floor(2, 103.5, 107.0)],
        source_crs=CRS,
        parcels=repo.records("parcels"),
        ground_datum=0.0,
    )
    assert len(repo.records("properties")) == before


def test_generation_does_not_rewrite_existing_ulpins():
    from app.repositories.memory import InMemoryRepository
    from app.services.demo import seed_demo

    repo = InMemoryRepository()
    seed_demo(repo)
    before = {p["id"]: p.get("prototype_ulpin") for p in repo.records("properties")}
    pv.generate_property_volumes(
        a_building(),
        [a_floor(1, 100.0, 103.5)],
        source_crs=CRS,
        parcels=repo.records("parcels"),
    )
    after = {p["id"]: p.get("prototype_ulpin") for p in repo.records("properties")}
    assert before == after


def test_no_ownership_word_appears_in_the_output():
    volume = _volume().to_record()
    assert "owner" not in volume
    assert "tenant" not in volume
    assert "title" not in volume
