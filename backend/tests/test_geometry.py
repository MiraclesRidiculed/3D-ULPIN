"""Geometry engine unit tests.

Covers the engine in isolation: conversions, validity and repair, measures,
Z-range handling, topology, volumetric intersection and geometry identity.

The planar transform rounds WGS84 to 7 decimal places, so anything that
round-trips through GeoJSON carries roughly 1e-3 relative error. Tests use exact
assertions on local-plane polygons and ``pytest.approx`` where a conversion is
involved.
"""
from __future__ import annotations

import pytest
from shapely.geometry import Polygon, box

from app.services.geometry import (
    DEMO_ANCHOR_LAT,
    DEMO_ANCHOR_LON,
    calculate_area,
    calculate_centroid,
    calculate_footprint_difference,
    calculate_geometry_hash,
    calculate_height_difference,
    calculate_horizontal_intersection,
    calculate_outside_area,
    calculate_spatial_code,
    calculate_vertical_overlap,
    calculate_volume,
    calculate_volume_difference,
    calculate_volume_intersection,
    geojson_to_polygon,
    is_polygon_inside,
    ll_to_xy,
    polygon_to_geojson,
    record_to_polygon,
    repair_polygon,
    union_footprints,
    validate_polygon,
    validate_z_range,
    xy_to_ll,
)

#: Relative tolerance for values that pass through the WGS84 round-trip.
TOL = 1e-3


def geo(ring: list[tuple[float, float]]) -> dict:
    """Local-plane ring (with implied closing point) -> GeoJSON mapping."""
    closed = list(ring) + [ring[0]]
    return {
        "type": "Polygon",
        "coordinates": [[xy_to_ll(x, y) for x, y in closed]],
    }


def record(ring, z_min, z_max, ident="T-1") -> dict:
    """Minimal property-volume-shaped record for the record-level helpers."""
    return {
        "id": ident,
        "geometry_3d": geo(ring),
        "z_min": z_min,
        "z_max": z_max,
    }


# --------------------------------------------------------------------------
# conversion
# --------------------------------------------------------------------------


def test_geojson_polygon_roundtrip():
    poly = box(0, 0, 80, 52)
    back = geojson_to_polygon(polygon_to_geojson(poly))
    assert back.area == pytest.approx(poly.area, rel=TOL)
    for actual, expected in zip(back.bounds, poly.bounds):
        assert actual == pytest.approx(expected, abs=0.01)


def test_polygon_to_geojson_emits_a_closed_wgs84_ring():
    out = polygon_to_geojson(box(0, 0, 80, 52))
    assert out["type"] == "Polygon"
    ring = out["coordinates"][0]
    assert ring[0] == ring[-1]
    assert all(len(pt) == 2 for pt in ring)
    # The anchor corner is the real WGS84 position of the demo origin.
    assert ring[0] == pytest.approx(
        [DEMO_ANCHOR_LON, DEMO_ANCHOR_LAT], abs=1e-7
    ) or [DEMO_ANCHOR_LON, DEMO_ANCHOR_LAT] in ring


def test_ll_to_xy_is_the_inverse_of_xy_to_ll():
    lon, lat = xy_to_ll(123.5, -47.25)
    x, y = ll_to_xy(lon, lat)
    assert x == pytest.approx(123.5, abs=0.01)
    assert y == pytest.approx(-47.25, abs=0.01)


def test_record_to_polygon_reads_each_footprint_field():
    ring = [(0, 0), (10, 0), (10, 10), (0, 10)]
    for field in ("geometry_3d", "geometry", "footprint"):
        assert calculate_area(record_to_polygon({field: geo(ring)})) == pytest.approx(
            100, rel=TOL
        )


def test_record_to_polygon_rejects_a_record_without_geometry():
    with pytest.raises(KeyError):
        record_to_polygon({"id": "X-1"})


# --------------------------------------------------------------------------
# validity and repair
# --------------------------------------------------------------------------


def test_valid_polygon_reports_no_reason():
    result = validate_polygon(box(0, 0, 10, 10))
    assert result.is_valid is True
    assert result.reason is None


def test_invalid_polygon_reports_a_reason():
    bowtie = Polygon([(0, 0), (10, 10), (10, 0), (0, 10)])
    result = validate_polygon(bowtie)
    assert result.is_valid is False
    assert result.reason  # a human-readable explanation is supplied


def test_repair_polygon_fixes_a_bowtie():
    bowtie = Polygon([(0, 0), (10, 10), (10, 0), (0, 10)])
    repaired = repair_polygon(bowtie)
    assert validate_polygon(repaired).is_valid is True
    # buffer(0) resolves the self-intersection to one of the two triangles.
    assert repaired.area == pytest.approx(25.0)


def test_repair_polygon_leaves_valid_geometry_untouched():
    poly = box(0, 0, 10, 10)
    assert repair_polygon(poly) is poly


# --------------------------------------------------------------------------
# measures
# --------------------------------------------------------------------------


def test_calculate_area():
    assert calculate_area(box(0, 0, 10, 10)) == 100.0
    assert calculate_area(box(0, 0, 80, 52)) == 4160.0


def test_calculate_centroid():
    assert calculate_centroid(box(0, 0, 10, 20)) == (5.0, 10.0)


def test_calculate_volume():
    # 100 m^2 footprint over a 3.2 m storey.
    assert calculate_volume(box(0, 0, 10, 10), 0, 3.2) == pytest.approx(320.0)


def test_calculate_volume_is_zero_for_a_flat_range():
    assert calculate_volume(box(0, 0, 10, 10), 5, 5) == 0.0


def test_calculate_height_difference():
    a = record([(0, 0), (10, 0), (10, 10)], 0, 10)
    b = record([(0, 0), (10, 0), (10, 10)], 0, 6)
    assert calculate_height_difference(a, b) == 4.0
    assert calculate_height_difference(b, a) == -4.0


def test_calculate_volume_difference():
    sq = [(0, 0), (10, 0), (10, 10), (0, 10)]
    a = record(sq, 0, 10, "A")   # 100 m^2 x 10 m
    b = record(sq, 0, 4, "B")    # 100 m^2 x 4 m
    assert calculate_volume_difference(a, b) == pytest.approx(600, rel=TOL)
    assert calculate_volume_difference(b, a) == pytest.approx(-600, rel=TOL)


# --------------------------------------------------------------------------
# Z range
# --------------------------------------------------------------------------


def test_valid_z_range():
    result = validate_z_range(-3.2, 0)
    assert result.is_valid is True
    assert result.reason is None


def test_zero_z_range_is_invalid():
    result = validate_z_range(5, 5)
    assert result.is_valid is False
    assert "zero height" in result.reason


def test_negative_z_range_is_invalid():
    result = validate_z_range(10, 0)
    assert result.is_valid is False
    assert "greater than" in result.reason


def test_vertical_overlap():
    assert calculate_vertical_overlap(0, 10, 5, 15) == 5.0
    assert calculate_vertical_overlap(-3.2, 0, -2.2, 0.0) == 2.2


def test_vertical_overlap_clamps_at_zero_when_disjoint():
    assert calculate_vertical_overlap(0, 5, 8, 12) == 0.0
    # Merely touching is not an overlap.
    assert calculate_vertical_overlap(0, 5, 5, 12) == 0.0


# --------------------------------------------------------------------------
# topology
# --------------------------------------------------------------------------


def test_containment_true():
    assert is_polygon_inside(box(1, 1, 2, 2), box(0, 0, 10, 10)) is True


def test_containment_false_when_partially_outside():
    assert is_polygon_inside(box(5, 5, 15, 15), box(0, 0, 10, 10)) is False


def test_containment_edge_contact_counts_as_inside():
    assert is_polygon_inside(box(0, 0, 10, 10), box(0, 0, 10, 10)) is True


def test_outside_area_is_zero_when_contained():
    assert calculate_outside_area(box(1, 1, 2, 2), box(0, 0, 10, 10)) == 0.0


def test_outside_area_for_an_encroaching_parcel():
    """The demo's floor-5 unit runs 6 m past an 80 m parcel edge."""
    parcel = box(0, 0, 80, 52)
    unit = box(40, 8, 86, 44)
    assert calculate_outside_area(unit, parcel) == pytest.approx(216.0)


def test_footprint_difference_returns_the_excess():
    parcel = box(0, 0, 10, 10)
    unit = box(5, 0, 15, 10)
    excess = calculate_footprint_difference(unit, parcel)
    assert calculate_area(excess) == pytest.approx(50.0)


def test_horizontal_intersection():
    shared = calculate_horizontal_intersection(box(0, 0, 10, 10), box(5, 0, 15, 10))
    assert calculate_area(shared) == pytest.approx(50.0)


def test_horizontal_intersection_of_disjoint_footprints_is_empty():
    shared = calculate_horizontal_intersection(box(0, 0, 1, 1), box(5, 5, 6, 6))
    assert shared.is_empty
    assert calculate_area(shared) == 0.0


def test_horizontal_intersection_of_edge_contact_has_no_area():
    """Touching footprints intersect as a LineString, not an area."""
    shared = calculate_horizontal_intersection(box(0, 0, 10, 10), box(10, 0, 20, 10))
    assert calculate_area(shared) == 0.0


def test_union_footprints_dissolves_a_floor():
    """Two adjacent units on one floor dissolve into a single 60 x 36 slab."""
    dissolved = union_footprints([box(10, 8, 40, 44), box(40, 8, 70, 44)])
    assert calculate_area(dissolved) == pytest.approx(60 * 36)


# --------------------------------------------------------------------------
# volumetric intersection
# --------------------------------------------------------------------------


def test_overlapping_volumes():
    """Same footprint, 5 m of shared height: 100 m^2 x 5 m = 500 m^3."""
    a = record([(0, 0), (10, 0), (10, 10), (0, 10)], 0, 10, "A")
    b = record([(0, 0), (10, 0), (10, 10), (0, 10)], 5, 15, "B")
    overlap, volume = calculate_volume_intersection(a, b)
    assert volume == pytest.approx(500, rel=TOL)
    assert calculate_area(overlap) == pytest.approx(100, rel=TOL)


def test_non_overlapping_volumes_have_zero_volume():
    """Identical footprints but disjoint heights."""
    a = record([(0, 0), (10, 0), (10, 10), (0, 10)], 0, 5, "A")
    b = record([(0, 0), (10, 0), (10, 10), (0, 10)], 8, 12, "B")
    _, volume = calculate_volume_intersection(a, b)
    assert volume == 0.0


def test_non_overlapping_footprints_have_zero_volume():
    a = record([(0, 0), (10, 0), (10, 10), (0, 10)], 0, 10, "A")
    b = record([(50, 50), (60, 50), (60, 60), (50, 60)], 0, 10, "B")
    overlap, volume = calculate_volume_intersection(a, b)
    assert volume == 0.0
    assert overlap.is_empty


def test_volume_intersection_against_an_infrastructure_proxy():
    """Mirrors the demo's utility corridor vs basement collision."""
    basement = record([(10, 8), (70, 8), (70, 44), (10, 44)], -3.2, 0, "PV-B001")
    corridor = {
        "id": "INF-U-001",
        "geometry_3d": geo([(18, 21), (62, 21), (62, 25), (18, 25)]),
        "z_min": -2.2,
        "z_max": 0.0,
    }
    _, volume = calculate_volume_intersection(basement, corridor)
    # 44 m x 4 m of plan overlap, 2.2 m of shared depth.
    assert volume == pytest.approx(44 * 4 * 2.2, rel=TOL)


# --------------------------------------------------------------------------
# identity
# --------------------------------------------------------------------------


def test_geometry_hash_is_stable_across_calls():
    poly = box(10, 8, 70, 44)
    assert calculate_geometry_hash(poly, -3.2, 0) == calculate_geometry_hash(
        poly, -3.2, 0
    )


def test_geometry_hash_shape():
    h = calculate_geometry_hash(box(10, 8, 70, 44), -3.2, 0)
    assert len(h) == 12
    assert h.isupper()
    assert all(c in "0123456789ABCDEF" for c in h)


def test_geometry_hash_survives_float_noise():
    """Coordinates are rounded to 3 dp before hashing."""
    a = box(10, 8, 70, 44)
    b = Polygon([(x + 1e-9, y + 1e-9) for x, y in a.exterior.coords])
    assert calculate_geometry_hash(a, 0, 1) == calculate_geometry_hash(b, 0, 1)


def test_geometry_hash_depends_on_z_extent():
    poly = box(10, 8, 70, 44)
    assert calculate_geometry_hash(poly, 0, 3.2) != calculate_geometry_hash(poly, 0, 6.4)


def test_geometry_hash_depends_on_footprint():
    assert calculate_geometry_hash(box(0, 0, 10, 10), 0, 1) != calculate_geometry_hash(
        box(0, 0, 10, 11), 0, 1
    )


def test_spatial_code_shape():
    code = calculate_spatial_code(box(10, 8, 70, 44))
    assert len(code) == 10
    assert code.isdigit()


def test_geometry_hash_normalises_int_vs_float_z_bounds():
    """Z bounds are coerced to float, so ``0`` and ``0.0`` cannot diverge.

    The payload is JSON, where ``0`` and ``0.0`` render differently. Normalising
    here is safe because identity no longer derives from this hash.
    """
    poly = box(10, 8, 70, 44)
    assert calculate_geometry_hash(poly, -3.2, 0) == calculate_geometry_hash(
        poly, -3.2, 0.0
    )
    assert calculate_geometry_hash(poly, 0, 0) == calculate_geometry_hash(poly, 0.0, 0.0)
