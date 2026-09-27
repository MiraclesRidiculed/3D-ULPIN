"""CRS management tests.

Covers the four properties that matter:

1. **CRS transformations work** â€” point and geometry, forward and back, across
   CRSs with different projections.
2. **Geometry remains valid after transformation** â€” reprojection must not
   introduce self-intersections or empty results.
3. **Metric calculations use projected coordinates** â€” area and distance are
   measured in a projected metre CRS, never in degrees.
4. **Display geometry can be converted back to WGS84** â€” and round-trips.

A deliberate non-test: nothing here asserts an *official* or government CRS. No
such requirement has been supplied by the data, so the service derives a
technical default from the data instead (see :mod:`app.services.crs`).
"""
from __future__ import annotations

import math

import pytest
from pyproj import CRS
from pyproj.exceptions import CRSError
from shapely.geometry import Point, Polygon, box

from app.services import crs
from app.services.crs import (
    UNKNOWN_CRS,
    WGS84,
    calculate_metric_area,
    calculate_metric_distance,
    crs_authority,
    crs_context,
    crs_units,
    declared_crs_from_geojson,
    get_source_crs,
    is_metric,
    is_projected,
    metric_bounds,
    normalize_geometry_crs,
    select_processing_crs,
    transform_geometry,
    transform_point,
    utm_epsg_for,
)
from app.services.geometry import (
    DEMO_ANCHOR_LAT,
    DEMO_ANCHOR_LON,
    GEOGRAPHIC_CRS,
    PROCESSING_CRS,
    calculate_area,
    calculate_volume,
    geojson_to_polygon,
    ll_to_xy,
    local_to_wgs84,
    polygon_to_geojson,
    validate_polygon,
    wgs84_to_local,
    xy_to_ll,
)

#: A square of exactly 100 m x 100 m, expressed in the engine's local projected
#: plane, so the true area is 10 000 mÂ².
SITE_100M = box(0, 0, 100, 100)
TRUE_AREA_M2 = 10_000.0


def site_in_processing_crs() -> Polygon:
    """The 100 m site in the engine's local projected plane (metres)."""
    return SITE_100M


def site_in_wgs84() -> Polygon:
    """The same site as a genuine WGS84 geometry (degrees).

    The local plane is the processing CRS *translated* so the anchor is the
    origin, so this must go through :func:`local_to_wgs84` — treating local
    coordinates as absolute UTM would place the site at the projection's false
    origin instead of near Delhi.
    """
    return local_to_wgs84(SITE_100M)


# ==========================================================================
# 1. CRS transformations work
# ==========================================================================


def test_processing_crs_is_derived_from_the_data_not_hardcoded():
    """UTM zone follows from longitude."""
    assert utm_epsg_for(77.2089, 28.6131) == "EPSG:32643"
    assert utm_epsg_for(-0.1276, 51.5072) == "EPSG:32630"   # London, zone 30N
    assert utm_epsg_for(-122.4, 37.8) == "EPSG:32610"       # San Francisco, 10N
    assert utm_epsg_for(151.2, -33.9) == "EPSG:32756"       # Sydney, south hemisphere


def test_select_processing_crs_from_a_point():
    """A point is only interpretable once its CRS is stated.

    This used to read ``select_processing_crs(point=...)`` with no
    ``source_crs``, which relied on a silent ``WGS84`` default. That default is
    gone on purpose: guessing WGS84 for a coordinate whose CRS nobody declared
    silently mis-places the data, and a derived UTM zone from a mis-placed point
    is a confidently wrong number. The CRS is now stated at every call site.
    """
    assert select_processing_crs(
        point=(77.2089, 28.6131), source_crs=WGS84
    ) == "EPSG:32643"
    assert select_processing_crs(
        point=(-0.1276, 51.5072), source_crs=WGS84
    ) == "EPSG:32630"


def test_select_processing_crs_refuses_an_unknown_source_crs():
    """No source CRS means no defensible answer, so it is an error not a default.

    The removed behaviour returned the UTM zone for the demo anchor, which would
    have measured an undeclared dataset in Delhi's grid.
    """
    for unstated in (None, UNKNOWN_CRS):
        with pytest.raises(CRSError):
            select_processing_crs(point=(77.2089, 28.6131), source_crs=unstated)


def test_select_processing_crs_requires_a_location_to_derive_from():
    """A known CRS but no geometry and no point cannot yield a zone."""
    with pytest.raises(ValueError):
        select_processing_crs(source_crs=WGS84)


def test_a_point_in_a_projected_crs_is_derived_from_its_true_position():
    """The point is reprojected before a zone is chosen, not read as degrees.

    32643 easting/northing for the Delhi site. Treating those numbers as
    longitude/latitude would select a nonsense zone from a plausible-looking one.
    """
    easting, northing = transform_point(77.2089, 28.6131, WGS84, "EPSG:32643")
    assert select_processing_crs(
        point=(easting, northing), source_crs="EPSG:32643"
    ) == "EPSG:32643"


def test_select_processing_crs_from_geometry():
    """A WGS84 polygon yields the UTM zone containing it."""
    site = site_in_wgs84()
    assert select_processing_crs(site, source_crs=WGS84) == (
        "EPSG:32643"
    )


def test_explicit_processing_crs_is_honoured_verbatim():
    """A caller-supplied authoritative CRS must win over any derived default."""
    assert select_processing_crs(
        point=(77.2089, 28.6131), processing_crs="EPSG:7755"
    ) == "EPSG:7755"


def test_already_projected_source_is_used_directly():
    """Reprojecting a metric CRS would only add error."""
    assert select_processing_crs(point=(0, 0), source_crs="EPSG:32643") == "EPSG:32643"


def test_transform_point_round_trips():
    easting, northing = transform_point(DEMO_ANCHOR_LON, DEMO_ANCHOR_LAT, WGS84, "EPSG:32643")
    lon, lat = transform_point(easting, northing, "EPSG:32643", WGS84)
    assert lon == pytest.approx(DEMO_ANCHOR_LON, abs=1e-9)
    assert lat == pytest.approx(DEMO_ANCHOR_LAT, abs=1e-9)


def test_transform_point_between_two_projected_crs():
    """Cross-projection move, e.g. WGS84 -> UTM -> Indian grid."""
    e, n = transform_point(DEMO_ANCHOR_LON, DEMO_ANCHOR_LAT, WGS84, "EPSG:32643")
    moved = transform_point(e, n, "EPSG:32643", "EPSG:7755")
    assert moved[0] > 200_000  # a plausible easting in the Indian grid
    back = transform_point(*moved, "EPSG:7755", "EPSG:32643")
    assert back[0] == pytest.approx(e, abs=0.01)
    assert back[1] == pytest.approx(n, abs=0.01)


def test_transform_point_is_identity_for_same_crs():
    assert transform_point(1.5, 2.5, "EPSG:4326", "EPSG:4326") == (1.5, 2.5)


def test_transform_geometry_moves_it_to_the_target_crs():
    site = site_in_wgs84()
    projected = transform_geometry(site, WGS84, "EPSG:32643")
    # In UTM the coordinates are ~700 km eastings, not ~0.001 degrees.
    minx, _, maxx, _ = projected.bounds
    assert minx > 600_000
    assert maxx - minx == pytest.approx(100.0, abs=0.05)


def test_resolve_crs_accepts_common_spellings():
    assert crs_authority("EPSG:4326") == "EPSG:4326"
    assert crs_authority(4326) == "EPSG:4326"
    assert crs_authority("4326") == "EPSG:4326"
    assert crs_authority("urn:ogc:def:crs:EPSG::32643") == "EPSG:32643"
    assert crs_authority(CRS.from_epsg(4326)) == "EPSG:4326"


def test_unknown_crs_is_rejected_rather_than_guessed():
    with pytest.raises(CRSError):
        crs_authority(UNKNOWN_CRS)
    with pytest.raises(CRSError):
        crs_authority(None)


# ==========================================================================
# 2. Geometry remains valid after transformation
# ==========================================================================


def test_polygon_stays_valid_after_transformation():
    site = site_in_wgs84()
    for target in ("EPSG:32643", "EPSG:7755", "EPSG:3857", "EPSG:4326"):
        moved = transform_geometry(site, WGS84, target)
        assert not moved.is_empty
        assert moved.is_valid, f"invalid after transform to {target}"
        assert validate_polygon(moved).is_valid


def test_local_plane_geometry_stays_valid_after_display_conversion():
    """The engine's own local-plane -> WGS84 hop must preserve validity too."""
    for geometry in (SITE_100M, box(10, 8, 40, 44), box(35, 8, 70, 44)):
        display = local_to_wgs84(geometry)
        assert display.is_valid
        assert validate_polygon(display).is_valid
        assert wgs84_to_local(display).equals_exact(geometry, tolerance=1e-6)


def test_area_is_preserved_across_reprojection():
    """A 100x100 m site stays 10 000 mÂ² no matter which CRS it is measured in."""
    site = site_in_wgs84()
    reference = calculate_area(transform_geometry(site, WGS84, "EPSG:32643"))
    assert reference == pytest.approx(TRUE_AREA_M2, rel=1e-4)
    for target in ("EPSG:7755", "EPSG:3857", "EPSG:32630", "EPSG:32643"):
        moved = transform_geometry(site, WGS84, target)
        # Reprojection then re-projection back is near-identity in area.
        restored = transform_geometry(moved, target, "EPSG:32643")
        assert calculate_area(restored) == pytest.approx(reference, rel=1e-6)


def test_round_trip_preserves_the_shape():
    site = site_in_wgs84()
    there = transform_geometry(site, WGS84, "EPSG:32643")
    back = transform_geometry(there, "EPSG:32643", WGS84)
    assert back.equals_exact(site, tolerance=1e-9)


def test_empty_geometry_is_handled():
    empty = Polygon()
    assert transform_geometry(empty, WGS84, "EPSG:32643").is_empty
    assert calculate_metric_area(empty) == 0.0


# ==========================================================================
# 3. Metric calculations use projected coordinates
# ==========================================================================


def test_metric_area_of_a_known_site_is_exact():
    """The anchor case: a 100 m x 100 m site is 10 000 mÂ²."""
    site = site_in_wgs84()
    measured = calculate_metric_area(site, source_crs=WGS84)
    assert measured == pytest.approx(TRUE_AREA_M2, rel=1e-6)


def test_measuring_in_degrees_would_be_wrong_by_orders_of_magnitude():
    """The trap this module exists to prevent.

    A WGS84 geometry's raw ``area`` is in square degrees. It is ~7.6e-07 for
    this site against a true 10 000 mÂ², and it does not error â€” it just looks
    like a small number.
    """
    site = site_in_wgs84()
    square_degrees = site.area
    # ~9.2e-07 square degrees for a 10 000 m² site, and it does not error.
    assert square_degrees == pytest.approx(9.2e-07, rel=0.2)
    metric = calculate_metric_area(site, source_crs=WGS84)
    assert metric == pytest.approx(TRUE_AREA_M2, rel=1e-4)
    # The metric answer differs by ~10 orders of magnitude.
    assert metric / square_degrees > 1e9


def test_metric_area_of_a_footprint_built_in_projected_metres():
    local = site_in_processing_crs()
    assert calculate_metric_area(local, source_crs=PROCESSING_CRS) == pytest.approx(
        TRUE_AREA_M2, rel=1e-6
    )


def test_metric_distance_between_neighbours():
    """Two 20 m squares whose near edges are 10 m apart."""
    a = box(0, 0, 20, 20)
    b = box(30, 0, 50, 20)
    assert calculate_metric_distance(a, b, method="min") == pytest.approx(10.0, abs=1e-6)
    # Centroids are 30 m apart in x, 0 in y.
    assert calculate_metric_distance(a, b, method="centroid") == pytest.approx(30.0, abs=1e-6)


def test_metric_distance_is_zero_for_touching_footprints():
    """A "gap" check wants this: touching means no gap."""
    assert calculate_metric_distance(box(0, 0, 10, 10), box(10, 0, 20, 10)) == 0.0


def test_metric_distance_works_across_crs():
    a = box(0, 0, 20, 20)
    b = box(30, 0, 50, 20)
    assert calculate_metric_distance(a, b, method="min", source_crs=PROCESSING_CRS) == (
        pytest.approx(10.0, abs=1e-6)
    )


def test_metric_distance_rejects_unknown_method():
    with pytest.raises(ValueError, match="unsupported distance method"):
        calculate_metric_distance(box(0, 0, 1, 1), box(2, 2, 3, 3), method="teleport")


def test_metric_volume_uses_projected_footprint():
    """Volume is plan area x height, so it inherits the projected area."""
    local = site_in_processing_crs()
    assert calculate_volume(local, 0, 3.2) == pytest.approx(TRUE_AREA_M2 * 3.2, rel=1e-6)


def test_metric_bounds_are_in_metres():
    local = site_in_processing_crs()
    minx, miny, maxx, maxy = metric_bounds(local, source_crs=PROCESSING_CRS)
    assert maxx - minx == pytest.approx(100.0, abs=1e-6)
    assert maxy - miny == pytest.approx(100.0, abs=1e-6)


def test_crs_roles_are_classified_correctly():
    assert is_projected("EPSG:32643") is True
    assert is_metric("EPSG:32643") is True
    assert crs_units("EPSG:32643") == "metre"
    assert is_projected("EPSG:4326") is False
    assert is_metric("EPSG:4326") is False
    assert crs_units("EPSG:4326") == "degree"


# ==========================================================================
# 4. Display geometry converts back to WGS84
# ==========================================================================


def test_local_geometry_converts_to_wgs84_for_display():
    local = site_in_processing_crs()
    display = local_to_wgs84(local)
    minx, miny, maxx, maxy = display.bounds
    assert 77.0 < minx < 78.0
    assert 28.0 < miny < 29.0
    # 100 m expressed in degrees at this latitude. Asserted as a range rather
    # than against a fixed divisor: the exact span comes out of the projection
    # (UTM scale factor included) and the old x/98000 shortcut is precisely what
    # this milestone removed.
    assert 0.0009 < maxx - minx < 0.0012
    assert 0.0008 < maxy - miny < 0.0011


def test_display_geometry_round_trips_back_to_the_processing_crs():
    """WGS84 display -> local metres, to within display rounding (~1 cm)."""
    local = site_in_processing_crs()
    display = local_to_wgs84(local)
    back = wgs84_to_local(display)
    assert back.equals_exact(local, tolerance=0.02)


def test_engine_transform_round_trips_through_geojson():
    """xy_to_ll / ll_to_xy are inverses, to within display rounding."""
    for x, y in ((0, 0), (80, 52), (-25, 130), (500, -300)):
        lon, lat = ll_to_xy(*xy_to_ll(x, y))
        assert lon == pytest.approx(x, abs=0.01)
        assert lat == pytest.approx(y, abs=0.01)


def test_polygon_to_geojson_uses_the_declared_geographic_crs():
    assert GEOGRAPHIC_CRS == WGS84
    assert crs_authority(GEOGRAPHIC_CRS) == WGS84
    out = polygon_to_geojson(SITE_100M)
    lon, lat = out["coordinates"][0][0]
    assert 77.0 < lon < 78.0 and 28.0 < lat < 29.0


def test_normalize_geometry_crs_brings_geometry_into_processing_crs():
    display = local_to_wgs84(SITE_100M)
    normalized = normalize_geometry_crs(display, source_crs=WGS84)
    minx, _, _, _ = normalized.bounds
    assert minx > 600_000  # back in UTM metres


def test_normalize_geometry_crs_leaves_already_projected_geometry_alone():
    local = site_in_processing_crs()
    assert normalize_geometry_crs(local) is local


# ==========================================================================
# CRS provenance on data sources
# ==========================================================================


def test_declared_crs_is_read_from_geojson():
    payload = {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "EPSG:32643"}},
        "features": [],
    }
    assert declared_crs_from_geojson(payload) == "EPSG:32643"


def test_geojson_without_crs_member_is_wgs84_per_rfc7946():
    assert declared_crs_from_geojson({"type": "FeatureCollection", "features": []}) == WGS84


def test_unusable_crs_member_is_reported_not_guessed():
    payload = {"type": "FeatureCollection", "crs": {"type": "name"}, "features": []}
    assert declared_crs_from_geojson(payload) == UNKNOWN_CRS
    payload = {"type": "FeatureCollection", "crs": "nonsense", "features": []}
    assert declared_crs_from_geojson(payload) == UNKNOWN_CRS


def test_get_source_crs_reads_the_record():
    assert get_source_crs({"crs": "EPSG:32643"}) == "EPSG:32643"
    assert get_source_crs({"metadata": {"source_crs": "EPSG:7755"}}) == "EPSG:7755"
    assert get_source_crs({"source_crs": 4326}) == "EPSG:4326"


def test_get_source_crs_does_not_default_to_wgs84():
    """Undeclared data stays unknown; assuming WGS84 would mis-place it."""
    assert get_source_crs({}) == UNKNOWN_CRS
    assert get_source_crs(None) == UNKNOWN_CRS
    assert get_source_crs({"crs": "not-a-crs"}) == UNKNOWN_CRS


def test_ingestion_preserves_declared_crs():
    from app.repositories.memory import InMemoryRepository
    from app.services.ingestion import register_geojson

    repo = InMemoryRepository()
    payload = b'{"type":"FeatureCollection","crs":{"type":"name","properties":{"name":"EPSG:32643"}},"features":[]}'
    result = register_geojson(repo, "a.geojson", payload)
    assert result["source_crs"] == "EPSG:32643"
    stored = repo.records("sources")[-1]
    assert stored["crs"] == "EPSG:32643"
    assert stored["metadata"]["source_crs"] == "EPSG:32643"
    assert stored["metadata"]["crs_declared_in_payload"] is True


def test_ingestion_records_unknown_crs_for_formats_that_carry_none():
    from app.repositories.memory import InMemoryRepository
    from app.services.ingestion import register_source

    repo = InMemoryRepository()
    result = register_source(repo, "a.las", b"junk")
    assert result["source_crs"] == UNKNOWN_CRS
    stored = repo.records("sources")[-1]
    assert stored["crs"] == UNKNOWN_CRS
    assert stored["metadata"]["crs_supplied_by_caller"] is False


def test_ingestion_accepts_a_caller_supplied_crs():
    from app.repositories.memory import InMemoryRepository
    from app.services.ingestion import register_source

    repo = InMemoryRepository()
    result = register_source(repo, "a.las", b"junk", crs="EPSG:32643")
    assert result["source_crs"] == "EPSG:32643"
    assert repo.records("sources")[-1]["metadata"]["crs_supplied_by_caller"] is True


def test_demo_source_records_all_three_crs_roles():
    from app.repositories.memory import InMemoryRepository
    from app.services.demo import seed_demo

    repo = InMemoryRepository()
    seed_demo(repo)
    source = repo.records("sources")[0]
    assert source["crs"] == WGS84
    assert source["metadata"]["source_crs"] == WGS84
    assert source["metadata"]["processing_crs"] == PROCESSING_CRS


def test_crs_context_reports_all_three_roles():
    context = crs_context(point=(DEMO_ANCHOR_LON, DEMO_ANCHOR_LAT), source_crs=WGS84)
    assert context.source_crs == "EPSG:4326"
    assert context.processing_crs == "EPSG:32643"
    assert context.display_crs == "EPSG:4326"
    assert context.describe() == {
        "source_crs": "EPSG:4326",
        "processing_crs": "EPSG:32643",
        "display_crs": "EPSG:4326",
    }


def test_the_retired_divisor_transform_was_wrong():
    """Quantifies what was replaced, so the fix cannot be silently reverted.

    The old code used fixed divisors (x/98000, y/111000). Over 80 m east that
    lands ~14 cm from the true projected position — small here, but an error
    that grows with distance from the anchor and varies with latitude.
    """
    truth_lon, _ = transform_point(
        *_ANCHOR_OFFSET(80, 0), PROCESSING_CRS, WGS84
    )
    old_lon = DEMO_ANCHOR_LON + 80 / 98000
    error_deg = abs(truth_lon - old_lon)
    # ~1.4e-06 degrees of longitude ...
    assert error_deg == pytest.approx(1.4e-06, rel=0.2)
    # ... which at this latitude is roughly 14 cm.
    metres_per_degree = math.cos(math.radians(DEMO_ANCHOR_LAT)) * 111_320
    assert error_deg * metres_per_degree == pytest.approx(0.137, rel=0.25)


def _ANCHOR_OFFSET(x: float, y: float) -> tuple[float, float]:
    from app.services.geometry import _ANCHOR_EASTING, _ANCHOR_NORTHING

    return _ANCHOR_EASTING + x, _ANCHOR_NORTHING + y
