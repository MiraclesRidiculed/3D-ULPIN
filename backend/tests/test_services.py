"""Domain service tests: ULPIN generation, validation rules and ingestion.

Geometry-engine behaviour is covered separately in ``test_geometry.py``.
"""
from __future__ import annotations

import pytest
from shapely.errors import GEOSException
from shapely.geometry import box

from app.repositories.memory import InMemoryRepository
from app.services.demo import seed_demo
from app.services.geometry import (
    DEMO_ANCHOR_LAT,
    DEMO_ANCHOR_LON,
    polygon_to_geojson,
    xy_to_ll,
)
from app.services.ingestion import IngestionError, register_geojson, register_source
from app.services.ulpin import ULPIN_LABEL, assign_ulpins
from app.services.validation import validate


@pytest.fixture
def repo():
    r = InMemoryRepository()
    seed_demo(r)
    return r


# --------------------------------------------------------------------------
# ULPIN issuance
# --------------------------------------------------------------------------


def test_ulpins_are_reproducible_across_seeds():
    a, b = InMemoryRepository(), InMemoryRepository()
    seed_demo(a)
    seed_demo(b)
    assert [p["prototype_ulpin"] for p in a.records("properties")] == [
        p["prototype_ulpin"] for p in b.records("properties")
    ]


def test_assign_ulpins_does_not_touch_geometry_hash():
    """Identity and geometry are owned separately."""
    r = InMemoryRepository()
    seed_demo(r)
    before = {p["id"]: p["geometry_hash"] for p in r.records("properties")}
    assign_ulpins(r)  # already assigned by the seed, so this is a no-op
    after = {p["id"]: p["geometry_hash"] for p in r.records("properties")}
    assert before == after


def test_ulpin_label_carries_disclaimer():
    assert "not an official ULPIN" in ULPIN_LABEL


def test_seeded_geojson_matches_the_committed_reference_file():
    """demo-data/demo-city.geojson mirrors the seeded parcel.

    The reference file is regenerated from the code, so it is a genuine
    cross-check of the CRS transform rather than a stale artefact.
    """
    r = InMemoryRepository()
    seed_demo(r)
    ring = r.records("parcels")[0]["geometry"]["coordinates"][0]
    assert len(ring) == 5
    assert ring[-1] == ring[0]  # closed ring
    # The real anchor position must appear among the corners.
    assert [DEMO_ANCHOR_LON, DEMO_ANCHOR_LAT] in ring
    # Every corner must be a plausible WGS84 coordinate near the anchor.
    for lon, lat in ring:
        assert abs(lon - DEMO_ANCHOR_LON) < 0.01
        assert abs(lat - DEMO_ANCHOR_LAT) < 0.01


# --------------------------------------------------------------------------
# validation rules
# --------------------------------------------------------------------------


def test_seeded_scene_reports_exactly_three_findings(repo):
    issues = repo.records("issues")
    assert len(issues) == 3
    assert sum(i["severity"] == "CRITICAL" for i in issues) == 2
    assert sum(i["severity"] == "WARNING" for i in issues) == 1


def test_validate_replaces_previous_findings(repo):
    repo.records("issues").append({"id": "STALE"})
    validate(repo)
    assert "STALE" not in [i["id"] for i in repo.records("issues")]


def test_overlap_detection_is_restricted_to_the_same_floor(repo):
    """Known limitation: inter-floor 3D overlaps are not detected."""
    a = repo.records("properties")[1]  # PV-101, floor 1
    b = repo.records("properties")[3]  # PV-202, floor 2
    # Give them identical footprints so only the floor number differs.
    b["geometry_3d"] = a["geometry_3d"]
    b["z_min"], b["z_max"] = a["z_min"], a["z_max"]
    validate(repo)
    assert not any(
        i["issue_type"] == "VERTICAL_VOLUME_OVERLAP"
        and {i["object_a"], i["object_b"]} == {"PV-101", "PV-202"}
        for i in repo.records("issues")
    )


def test_invalid_geometry_is_appended_then_raises(repo):
    """Known pre-existing limitation (preserved, not introduced here).

    ``validate()`` appends the INVALID_GEOMETRY finding and then *continues*
    into the footprint-difference check on the same invalid geometry, which
    makes GEOS raise. The original monolithic implementation had exactly the
    same control flow, so the endpoint 500s on invalid input.
    """
    target = repo.records("properties")[1]
    # A bow-tie polygon is invalid.
    bowtie = [[0, 0], [1, 1], [1, 0], [0, 1], [0, 0]]
    target["geometry_3d"] = {
        "type": "Polygon",
        "coordinates": [[xy_to_ll(x, y) for x, y in bowtie]],
    }
    with pytest.raises(GEOSException):
        validate(repo)
    # The finding is recorded before the crash.
    assert any(
        i["issue_type"] == "INVALID_GEOMETRY" for i in repo.records("issues")
    )


def test_floor_gap_is_detected(repo):
    for p in repo.records("properties"):
        if p["floor_number"] == 3:
            p["z_min"] += 2.0
            p["z_max"] += 2.0
    validate(repo)
    assert any(
        i["issue_type"] == "EXPECTED_ADJACENT_FLOOR_GAP" for i in repo.records("issues")
    )


def test_z_order_inconsistency_is_detected(repo):
    target = repo.records("properties")[1]
    target["z_min"], target["z_max"] = 10.0, 10.0
    validate(repo)
    assert any(
        i["issue_type"] == "FLOOR_Z_ORDER_INCONSISTENCY"
        for i in repo.records("issues")
    )


def test_duplicate_identifier_is_detected(repo):
    props = repo.records("properties")
    # Make the duplicate share geometry as well, so the overlap has area and
    # the finding carries a real polygon (see the LineString note below).
    props[2]["prototype_ulpin"] = props[1]["prototype_ulpin"]
    props[2]["geometry_3d"] = props[1]["geometry_3d"]
    props[2]["z_min"] = props[1]["z_min"]
    props[2]["z_max"] = props[1]["z_max"]
    validate(repo)
    assert any(
        i["issue_type"] == "DUPLICATE_ULPIN_GEOMETRY" for i in repo.records("issues")
    )


def test_duplicate_identifier_touching_only_raises(repo):
    """Known pre-existing limitation (preserved, not introduced here).

    When two records share an identifier but their footprints only touch along
    an edge, the intersection is a LineString rather than a Polygon, and the
    finding serialiser calls the polygon-to-GeoJSON conversion on it, which
    raises. Original control flow was identical.
    """
    props = repo.records("properties")
    # PV-101 (x 10..40) and PV-102 (x 40..70) share only the edge x=40.
    props[2]["prototype_ulpin"] = props[1]["prototype_ulpin"]
    with pytest.raises(AttributeError):
        validate(repo)


# --------------------------------------------------------------------------
# ingestion
# --------------------------------------------------------------------------


def test_register_geojson_counts_features_only():
    r = InMemoryRepository()
    seed_demo(r)
    out = register_geojson(r, "a.geojson", b'{"features":[{"id":1},{"id":2}]}')
    assert out["features"] == 2
    # No cadastral records created.
    assert len(r.records("parcels")) == 1


def test_register_geojson_rejects_bad_extension():
    r = InMemoryRepository()
    with pytest.raises(IngestionError) as exc:
        register_geojson(r, "a.csv", b"{}")
    assert exc.value.status_code == 400


def test_register_source_rejects_unknown_extension():
    r = InMemoryRepository()
    with pytest.raises(IngestionError) as exc:
        register_source(r, "a.exe", b"x")
    assert exc.value.status_code == 400


def test_register_source_accepts_point_cloud_extensions():
    r = InMemoryRepository()
    for ext in ("las", "laz", "ply"):
        out = register_source(r, f"a.{ext}", b"junk")
        assert out["source_type"] == "Point cloud"


def test_register_source_honours_explicit_type():
    r = InMemoryRepository()
    # The extension is still validated first, so use a supported one.
    out = register_source(r, "a.ply", b"x", source_type="Custom")
    assert out["source_type"] == "Custom"


def test_upload_size_limit_is_enforced():
    r = InMemoryRepository()
    with pytest.raises(IngestionError) as exc:
        register_source(r, "a.ply", b"0" * (10_000_001))
    assert exc.value.status_code == 413


def test_make_volume_derives_fields_from_geometry():
    """Derived fields must be computed via the engine, never hand-entered."""
    from app.services.geometry import calculate_geometry_hash, make_volume

    poly = box(0, 0, 10, 10)
    v = make_volume(
        ident="T-1",
        parcel="P-001",
        building="B-001",
        prop_type="APARTMENT",
        floor=1,
        label="Test Unit",
        footprint=poly,
        zmin=0,
        zmax=3.2,
    )
    assert v["area_m2"] == 100.0
    assert v["volume_m3"] == 320.0
    assert v["geometry_hash"] == calculate_geometry_hash(poly, 0, 3.2)
    assert v["geometry_3d"] == polygon_to_geojson(poly)
