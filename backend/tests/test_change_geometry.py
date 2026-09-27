"""Tests for the visual verification surface: change geometry a client can draw.

The seven tools in ``frontend/lib/compare.ts`` need a *region* to highlight, not
just two outlines to subtract. That region is ``difference_geometry`` on the
change record, and it is only meaningful if the serialiser handles what a
symmetric difference actually produces.

A symmetric difference between two footprints is routinely **multi-part** -- a
hole left where a building stood, a sliver on two sides of a moved boundary --
and it often has **interior rings**. A serialiser that emitted only the exterior
ring would fill the hole back in, showing a reviewer the *opposite* of the region
that changed. That is the failure these tests exist to prevent.
"""
from __future__ import annotations

import pytest

from app.repositories.memory import InMemoryRepository
from app.services import change_detection as cd
from app.services.demo import seed_demo
from app.services.geometry import create_rectangle, shape_to_geojson
from app.services.validation import validate


def obj(
    object_id="PV-1",
    box=(0.0, 0.0, 10.0, 10.0),
    z_min=0.0,
    z_max=3.0,
    floor=1,
    building="B-001",
):
    from app.services.geometry import polygon_to_geojson

    return {
        "id": object_id,
        "geometry_3d": polygon_to_geojson(create_rectangle(*box)),
        "z_min": z_min,
        "z_max": z_max,
        "floor_number": floor,
        "building_id": building,
    }


# ==========================================================================
# shape_to_geojson
# ==========================================================================


def test_a_simple_polygon_serialises_as_one_ring():
    shape = create_rectangle(0, 0, 10, 10)
    geo = shape_to_geojson(shape)
    assert geo["type"] == "Polygon"
    assert len(geo["coordinates"]) == 1


def test_an_interior_ring_is_kept():
    """A hole must survive, or a removed building is drawn as still standing."""
    outer = create_rectangle(0, 0, 20, 20)
    inner = create_rectangle(7, 7, 13, 13)
    geo = shape_to_geojson(outer.difference(inner))
    assert geo["type"] == "Polygon"
    assert len(geo["coordinates"]) == 2, "the hole must be kept as an interior ring"
    assert len(geo["coordinates"][1]) > 3


def test_a_multipolygon_is_kept_whole():
    a = create_rectangle(0, 0, 10, 10)
    b = create_rectangle(50, 50, 60, 60)
    geo = shape_to_geojson(a.symmetric_difference(b))
    assert geo["type"] == "MultiPolygon"
    assert len(geo["coordinates"]) == 2


def test_an_empty_shape_is_none_not_an_empty_polygon():
    """'Nothing left' must be distinguishable from 'a region of no area'."""
    a = create_rectangle(0, 0, 10, 10)
    assert shape_to_geojson(a.difference(a)) is None
    assert shape_to_geojson(None) is None


def test_a_collection_keeps_only_its_polygonal_parts():
    from shapely.geometry import GeometryCollection, LineString

    from app.services.geometry import xy_to_ll  # noqa: F401 - documents the module under test

    polygon = create_rectangle(0, 0, 5, 5)
    collection = GeometryCollection([polygon, LineString([(0, 0), (1, 1)])])
    geo = shape_to_geojson(collection)
    assert geo is not None
    assert geo["type"] == "Polygon"


# ==========================================================================
# difference_geometry on the change record
# ==========================================================================


def test_a_footprint_change_records_the_region_that_changed():
    report = cd.compare_approved_vs_survey(
        [obj(box=(0, 0, 10, 10))], [obj(box=(0, 0, 12, 10))], source_id="DS-1"
    )
    change = next(c for c in report.changes if c.change_type == "FOOTPRINT")
    assert change.difference_geometry is not None
    assert change.difference_geometry["coordinates"]
    # The region is a sliver, not the whole new footprint.
    assert change.difference_geometry != change.new_geometry


def test_the_record_carries_the_difference():
    store = InMemoryRepository()
    seed_demo(store)
    cd.compare_approved_vs_survey(
        [obj(box=(0, 0, 10, 10))],
        [obj(box=(0, 0, 12, 10))],
        source_id="DS-1",
        repository=store,
    )
    stored = next(
        c for c in store.records("cadastral_changes") if c["change_type"] == "FOOTPRINT"
    )
    assert stored["difference_geometry"] is not None


def test_a_translated_boundary_still_has_a_difference():
    """Same area, moved: an area-only comparison would report nothing."""
    before = obj(box=(0, 0, 10, 10))
    after = obj(box=(1, 0, 11, 10))
    report = cd.compare_approved_vs_survey([before], [after], source_id="DS-1")
    change = next(c for c in report.changes if c.change_type == "FOOTPRINT")
    assert change.area_delta == pytest.approx(0.0, abs=0.01)
    assert change.difference_geometry is not None


def test_a_storey_with_no_outline_has_no_difference():
    """A new storey has no previous outline, so there is no region to draw."""
    report = cd.compare_approved_vs_survey(
        [obj(floor=1)], [obj(floor=1), obj("PV-2", floor=2)], source_id="DS-1"
    )
    new_floor = next(c for c in report.changes if c.change_type == "NEW_FLOOR")
    assert new_floor.difference_geometry is None
    assert new_floor.previous_geometry is None


def test_a_height_only_change_has_no_plan_difference():
    """Only the Z band moved, so the plan region is identical and stays empty."""
    report = cd.compare_approved_vs_survey(
        [obj(z_max=3.0)], [obj(z_max=6.0)], source_id="DS-1"
    )
    volume = next(c for c in report.changes if c.change_type == "VOLUME")
    assert volume.difference_geometry is None
    assert volume.area_delta == pytest.approx(0.0, abs=0.01)


def test_identical_geometry_records_no_difference():
    record = obj()
    report = cd.compare_approved_vs_survey([record], [dict(record)], source_id="DS-1")
    assert report.changes == []


# ==========================================================================
# the demo scene is untouched
# ==========================================================================


def test_the_demo_scene_records_no_changes(repo=None):
    store = InMemoryRepository()
    seed_demo(store)
    assert store.records("cadastral_changes") == []


def test_validation_still_finds_exactly_three_findings():
    store = InMemoryRepository()
    seed_demo(store)
    assert validate(store) == {"issues": 3, "critical": 2, "warning": 1}


def test_the_difference_survives_a_re_validation():
    """A re-run rebuilds the findings; the recorded change must not be lost."""
    store = InMemoryRepository()
    seed_demo(store)
    cd.compare_approved_vs_survey(
        [obj(box=(0, 0, 10, 10))],
        [obj(box=(0, 0, 12, 10))],
        source_id="DS-1",
        repository=store,
    )
    before = len(store.records("cadastral_changes"))
    validate(store)
    assert len(store.records("cadastral_changes")) == before
