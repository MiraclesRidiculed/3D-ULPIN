"""API-level tests for floor segmentation.

Exercises the whole path: upload a synthetic multi-storey point cloud, extract
buildings, segment storeys, and check the stored result, the job trail, and the
failures.

The point-cloud store is redirected to a temporary directory so these tests leave
no files behind.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.fixtures import buildings as fx

CRS = "EPSG:32643"


@pytest.fixture
def store(tmp_path, monkeypatch):
    from app.services import point_cloud_store as pc_store

    root = tmp_path / "store"
    root.mkdir()
    monkeypatch.setattr(pc_store, "DEFAULT_STORE_DIR", root)
    return root


@pytest.fixture
def upload(tmp_path_factory):
    """Upload bytes for a multi-storey scene, written to a real LAS file."""

    def _read(spec: fx.StoreyBuilding, *, name: str = "scan.las") -> bytes:
        directory = tmp_path_factory.mktemp("floors")
        path = fx.write_las_storey_scene(directory / name, spec)
        return path.read_bytes()

    return _read


def ingest_and_extract(client, payload: bytes, name: str = "scan.las") -> str:
    r = client.post(
        "/import/source",
        files={"file": (name, payload, "application/octet-stream")},
    )
    assert r.status_code == 200, r.text
    source_id = r.json()["source_id"]
    r = client.post(f"/point-clouds/{source_id}/extract")
    assert r.status_code == 200, r.text
    return source_id


# ==========================================================================
# The happy path
# ==========================================================================


def test_segmentation_finds_the_true_storey_count(client, store, upload):
    source_id = ingest_and_extract(client, upload(fx.three_storey_building()))
    r = client.post(f"/point-clouds/{source_id}/segment-floors")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["storeys_found"] == 3
    assert body["buildings_segmented"] == 1
    assert [s["floor_number"] for s in body["storeys"]] == [1, 2, 3]


def test_storey_heights_match_the_synthetic_truth(client, store, upload):
    spec = fx.three_storey_building()
    source_id = ingest_and_extract(client, upload(spec))
    body = client.post(f"/point-clouds/{source_id}/segment-floors").json()
    heights = [s["z_max"] - s["z_min"] for s in body["storeys"]]
    for found in heights:
        assert found == pytest.approx(3.5, abs=0.2)


def test_a_five_storey_building_is_segmented_as_five(client, store, upload):
    source_id = ingest_and_extract(client, upload(fx.five_storey_building()))
    body = client.post(f"/point-clouds/{source_id}/segment-floors").json()
    assert body["storeys_found"] == 5
    heights = [s["z_max"] - s["z_min"] for s in body["storeys"]]
    for found in heights:
        assert found == pytest.approx(3.2, abs=0.2)


def test_a_single_storey_building_is_one_storey(client, store, upload):
    source_id = ingest_and_extract(client, upload(fx.single_storey_building()))
    body = client.post(f"/point-clouds/{source_id}/segment-floors").json()
    assert body["storeys_found"] == 1
    # Its height cannot be established from spacing, so it must be flagged.
    assert body["requires_human_review"] is True


def test_irregular_storey_is_detected_and_flagged(client, store, upload):
    spec = fx.double_height_building()
    source_id = ingest_and_extract(client, upload(spec))
    body = client.post(f"/point-clouds/{source_id}/segment-floors").json()

    assert body["storeys_found"] == 4
    double = next(s for s in body["storeys"] if s["floor_number"] == 2)
    assert (double["z_max"] - double["z_min"]) == pytest.approx(6.9, abs=0.4)
    assert double["requires_human_review"] is True
    assert any("IRREGULAR_SPACING" in r for r in double["review_reasons"])
    assert body["requires_human_review"] is True


# ==========================================================================
# Required output fields
# ==========================================================================


def test_storey_carries_every_required_field(client, store, upload):
    source_id = ingest_and_extract(client, upload(fx.three_storey_building()))
    body = client.post(f"/point-clouds/{source_id}/segment-floors").json()

    for storey in body["storeys"]:
        assert storey["floor_number"] >= 1
        assert storey["z_min"] < storey["z_max"]
        # ...plus the heights above the building base, which is what a surveyor reads
        assert storey["z_max_above_ground"] > storey["z_min_above_ground"]
        # floor geometry
        assert storey["footprint"]["type"] == "Polygon"
        ring = storey["footprint"]["coordinates"][0]
        assert len({(round(a, 6), round(b, 6)) for a, b in ring}) >= 4
        assert all(77.0 < lon < 77.5 for lon, _ in ring)
        assert storey["geometry_3d"]["z_min"] == storey["z_min"]
        # quality metric
        assert 0.0 <= storey["geometric_quality"] <= 1.0
        assert storey["quality_metrics"]["components"]["point_count"] > 0
        # provenance
        assert storey["source_id"] == source_id
        assert storey["processing_job_id"] == body["processing_job_id"]
        assert storey["building_id"]
        assert storey["crs"] == CRS
        assert storey["method"] == "algorithmic_geometric"
        assert len(storey["geometry_hash"]) == 12
        assert isinstance(storey["requires_human_review"], bool)


def test_stored_storey_area_is_measured_correctly(client, store, upload):
    """Re-measure the persisted WGS84 footprint with pyproj, independently."""
    from pyproj import Transformer
    from shapely.geometry import shape
    from shapely.ops import transform as shapely_transform

    spec = fx.three_storey_building()
    source_id = ingest_and_extract(client, upload(spec))
    body = client.post(f"/point-clouds/{source_id}/segment-floors").json()

    to_utm = Transformer.from_crs("EPSG:4326", "EPSG:32643", always_xy=True)
    for storey in body["storeys"]:
        area = shapely_transform(to_utm.transform, shape(storey["footprint"])).area
        assert area == pytest.approx(storey["area_m2"], rel=0.02)
    # The building is 30 x 24 m.
    assert body["storeys"][0]["area_m2"] == pytest.approx(30 * 24, rel=0.05)


def test_storeys_are_listable_and_filterable(client, store, upload):
    source_id = ingest_and_extract(client, upload(fx.five_storey_building()))
    body = client.post(f"/point-clouds/{source_id}/segment-floors").json()
    assert len(body["storeys"]) == 5

    listed = client.get("/extracted-floors").json()
    assert len(listed) == 5
    assert all(s["source_id"] == source_id for s in listed)

    building_id = body["storeys"][0]["building_id"]
    per_building = client.get(f"/extracted-floors?building_id={building_id}").json()
    assert len(per_building) == 5

    assert client.get("/extracted-floors?source_id=DS-999").json() == []


def test_geometry_hashes_are_unique_per_storey(client, store, upload):
    source_id = ingest_and_extract(client, upload(fx.three_storey_building()))
    body = client.post(f"/point-clouds/{source_id}/segment-floors").json()
    hashes = [s["geometry_hash"] for s in body["storeys"]]
    assert len(set(hashes)) == 3


def test_segmentation_is_reproducible(client, store, upload):
    payload = upload(fx.three_storey_building())
    results = []
    for index in range(2):
        source_id = ingest_and_extract(client, payload, name=f"scan{index}.las")
        body = client.post(f"/point-clouds/{source_id}/segment-floors").json()
        results.append(
            (body["storeys_found"], sorted(s["geometry_hash"] for s in body["storeys"]))
        )
    assert results[0] == results[1]


# ==========================================================================
# The job trail and status
# ==========================================================================


def test_segmentation_records_a_completed_job(client, store, upload):
    source_id = ingest_and_extract(client, upload(fx.three_storey_building()))
    body = client.post(f"/point-clouds/{source_id}/segment-floors").json()

    jobs = client.get(f"/processing-jobs?source_id={source_id}").json()
    segmentation = [j for j in jobs if j["job_type"] == "FLOOR_SEGMENTATION"]
    completed = next(
        j for j in segmentation if j["id"] == body["processing_job_id"]
    )
    assert completed["status"] == "COMPLETED"
    assert completed["metadata"]["storeys_found"] == 3
    assert completed["metadata"]["method"] == "algorithmic_geometric"
    # No stale PENDING row left claiming the work is outstanding.
    assert not any(j["status"] == "PENDING" for j in segmentation)


def test_source_status_reaches_segmented(client, store, upload):
    source_id = ingest_and_extract(client, upload(fx.three_storey_building()))
    assert client.get(f"/point-clouds/{source_id}").json()["status"] == "EXTRACTED"
    client.post(f"/point-clouds/{source_id}/segment-floors")
    assert client.get(f"/point-clouds/{source_id}").json()["status"] == "SEGMENTED"


def test_both_stages_are_pending_after_ingestion(client, store, upload):
    """Neither stage is missing any more, so neither is NOT_IMPLEMENTED."""
    r = client.post(
        "/import/source",
        files={"file": ("scan.las", upload(fx.three_storey_building()),
                        "application/octet-stream")},
    )
    statuses = {j["job_type"]: j["status"] for j in r.json()["jobs"]}
    assert statuses["METADATA_EXTRACTION"] == "COMPLETED"
    assert statuses["BUILDING_EXTRACTION"] == "PENDING"
    assert statuses["FLOOR_SEGMENTATION"] == "PENDING"
    assert "NOT_IMPLEMENTED" not in statuses.values()


# ==========================================================================
# A caller-supplied storey height
# ==========================================================================


def test_supplied_floor_height_is_recorded_as_supplied(client, store, upload):
    source_id = ingest_and_extract(client, upload(fx.three_storey_building()))
    body = client.post(
        f"/point-clouds/{source_id}/segment-floors?floor_height=3.5"
    ).json()
    assert body["buildings"][0]["levels_from"] == "supplied_by_caller"
    # A supplied height is not a measurement, so it must be flagged.
    assert any("LEVELS_SUPPLIED" in r for r in body["review_reasons"])
    assert body["requires_human_review"] is True


def test_estimated_levels_are_recorded_as_detected(client, store, upload):
    source_id = ingest_and_extract(client, upload(fx.three_storey_building()))
    body = client.post(f"/point-clouds/{source_id}/segment-floors").json()
    assert body["buildings"][0]["levels_from"] == "detected_from_points"
    assert body["buildings"][0]["floor_height_m"] == pytest.approx(3.5, abs=0.2)
    assert body["buildings"][0]["height_estimate"]["reliable"] is True


# ==========================================================================
# Failures
# ==========================================================================


def test_segmenting_an_unknown_source_is_404(client, store):
    assert client.post("/point-clouds/DS-999/segment-floors").status_code == 404


def test_segmentation_before_extraction_is_refused(client, store, upload):
    r = client.post(
        "/import/source",
        files={"file": ("scan.las", upload(fx.three_storey_building()),
                        "application/octet-stream")},
    )
    source_id = r.json()["source_id"]
    r = client.post(f"/point-clouds/{source_id}/segment-floors")
    assert r.status_code == 409
    assert "extract" in r.json()["detail"].lower()


def test_a_crs_less_cloud_is_refused_and_the_job_is_failed(client, store, tmp_path_factory):
    directory = tmp_path_factory.mktemp("nocrs")
    path = fx.write_las_scene(directory / "nocrs.las", fx.two_buildings_scene(), crs_epsg=None)
    r = client.post(
        "/import/source",
        files={"file": ("nocrs.las", path.read_bytes(), "application/octet-stream")},
    )
    source_id = r.json()["source_id"]
    r = client.post(f"/point-clouds/{source_id}/segment-floors")
    assert r.status_code == 422
    assert "no CRS" in r.json()["detail"]

    jobs = client.get(f"/processing-jobs?source_id={source_id}").json()
    segmentation = [j for j in jobs if j["job_type"] == "FLOOR_SEGMENTATION"]
    assert any(j["status"] == "FAILED" for j in segmentation)
    assert client.get("/extracted-floors").json() == []


def test_a_deleted_retained_file_is_reported(client, store, upload):
    source_id = ingest_and_extract(client, upload(fx.three_storey_building()))
    for path in store.rglob("*.las"):
        path.unlink()
    r = client.post(f"/point-clouds/{source_id}/segment-floors")
    assert r.status_code == 409
    assert "retained file" in r.json()["detail"].lower()


# ==========================================================================
# Honesty
# ==========================================================================


def test_no_confidence_or_accuracy_figure_is_returned(client, store, upload):
    source_id = ingest_and_extract(client, upload(fx.three_storey_building()))
    body = client.post(f"/point-clouds/{source_id}/segment-floors").json()
    for storey in body["storeys"]:
        assert "confidence" not in storey
        assert "accuracy" not in storey
        assert "probability" not in storey
    assert "confidence" not in body
    assert "no accuracy figure is claimed" in body["note"].lower()
    assert "not machine learning" in body["method_description"].lower()


def test_segmentation_creates_no_property_or_ownership_record(client, store, upload):
    """The cadastre must be untouched by storey detection."""
    source_id = ingest_and_extract(client, upload(fx.three_storey_building()))
    client.post(f"/point-clouds/{source_id}/segment-floors")

    assert len(client.get("/properties").json()) == 17
    assert len(client.get("/buildings").json()) == 1
    assert len(client.get("/parcels").json()) == 1
    # The demo's own floor stack is unchanged.
    demo_floors = client.get("/floors").json()
    assert len(demo_floors) == 8
    assert [f["z_min"] for f in demo_floors] == pytest.approx(
        [round(i * 3.2, 1) for i in range(8)], abs=0.01
    )
    assert "no property volumes or ownership records" in (
        client.post(f"/point-clouds/{source_id}/segment-floors").json()["note"].lower()
    )


def test_demo_floor_endpoint_keeps_its_deterministic_values(client, store):
    """The demo's 8 floors at 3.2 m is untouched by real-survey segmentation."""
    floors = client.get("/floors").json()
    assert len(floors) == 8
    for index, floor in enumerate(floors):
        assert floor["floor_number"] == index + 1
        assert floor["z_min"] == pytest.approx(index * 3.2, abs=0.01)
        assert floor["z_max"] == pytest.approx((index + 1) * 3.2, abs=0.01)
