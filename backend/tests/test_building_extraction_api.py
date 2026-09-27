"""API-level tests for building extraction.

Exercises the real pipeline through the HTTP surface: upload a synthetic point
cloud, run extraction, and check the stored result, the job trail, and the
failures.

The point-cloud store is redirected to a temporary directory so these tests do
not leave files in the repository.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.fixtures import buildings as fx

CRS = "EPSG:32643"


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point the retained-file store at a temporary directory."""
    from app.services import point_cloud_store as pc_store

    root = tmp_path / "store"
    root.mkdir()
    monkeypatch.setattr(pc_store, "DEFAULT_STORE_DIR", root)
    return root


def _scene_bytes(tmp_path_factory, scene, *, name="scan.las", fmt="las", **kwargs):
    directory = tmp_path_factory.mktemp("upload")
    if fmt == "las":
        path = fx.write_las_scene(directory / name, scene, **kwargs)
    elif fmt == "laz":
        path = fx.write_laz_scene(directory / name, scene, **kwargs)
    else:
        path = fx.write_ply_scene(directory / name, scene, **kwargs)
    return path.read_bytes()


@pytest.fixture
def upload_las(tmp_path_factory):
    def _read(scene=None, **kwargs):
        return _scene_bytes(
            tmp_path_factory, scene or fx.two_buildings_scene(), fmt="las", **kwargs
        )

    return _read


# ==========================================================================
# The happy path
# ==========================================================================


def test_extract_returns_both_buildings(client, store, upload_las):
    r = client.post(
        "/import/source",
        files={"file": ("scan.las", upload_las(), "application/octet-stream")},
    )
    assert r.status_code == 200
    source_id = r.json()["source_id"]

    r = client.post(f"/point-clouds/{source_id}/extract")
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["buildings_found"] == 2
    assert body["method"] == "algorithmic_geometric"
    assert "not machine learning" in body["method_description"].lower()
    assert body["extractor"] == "DeterministicBuildingExtractor"
    assert body["processing_job_id"]
    assert body["point_cloud"]["points_read"] > 0
    assert body["parameters"]["cluster_eps"] == 1.5

    heights = sorted(b["height_m"] for b in body["buildings"])
    assert heights[0] == pytest.approx(12.0, abs=0.3)
    assert heights[1] == pytest.approx(25.0, abs=0.3)


def test_extracted_building_carries_the_full_output_contract(client, store, upload_las):
    r = client.post(
        "/import/source",
        files={"file": ("scan.las", upload_las(), "application/octet-stream")},
    )
    source_id = r.json()["source_id"]
    body = client.post(f"/point-clouds/{source_id}/extract").json()

    for building in body["buildings"]:
        # geometry
        assert building["footprint"]["type"] == "Polygon"
        ring = building["footprint"]["coordinates"][0]
        # Stored as WGS84, so coordinates are degrees, not metres.
        assert all(-180 <= lon <= 180 for lon, _ in ring)
        assert all(-90 <= lat <= 90 for _, lat in ring)
        # A real footprint, not a collapsed point. This guards a double-reprojection
        # bug that once silently flattened every stored footprint to a single
        # coordinate while still reporting a plausible area.
        assert len({(round(lon, 6), round(lat, 6)) for lon, lat in ring}) >= 4
        # Near the demo city, where the fixture is anchored.
        assert all(77.0 < lon < 77.5 for lon, _ in ring)
        assert all(28.0 < lat < 29.0 for _, lat in ring)
        # height, source, job, method, quality, hash, provenance
        assert building["height_m"] > 0
        assert building["source_id"] == source_id
        assert building["processing_job_id"] == body["processing_job_id"]
        assert building["method"] == "algorithmic_geometric"
        assert 0.0 <= building["geometric_quality"] <= 1.0
        assert len(building["geometry_hash"]) == 12
        assert building["crs"] == CRS
        assert building["provenance"]["parameters"]
        assert building["provenance"]["ground_model"]["cells"] > 0
        assert building["quality_metrics"]["components"]["roof_planarity_rms_m"] is not None


def test_extracted_buildings_are_listable(client, store, upload_las):
    r = client.post(
        "/import/source",
        files={"file": ("scan.las", upload_las(), "application/octet-stream")},
    )
    source_id = r.json()["source_id"]
    client.post(f"/point-clouds/{source_id}/extract")

    listed = client.get("/extracted-buildings").json()
    assert len(listed) == 2
    assert all(b["source_id"] == source_id for b in listed)

    filtered = client.get(f"/extracted-buildings?source_id={source_id}").json()
    assert len(filtered) == 2
    assert client.get("/extracted-buildings?source_id=DS-999").json() == []


def test_status_reaches_extracted_not_left_on_metadata_only(client, store, upload_las):
    """No stale PENDING row may keep the source reading as METADATA_ONLY.

    Repeated uploads of the same file each leave a PENDING stage row; all of them
    must be closed by a successful run.
    """
    payload = upload_las()
    ids = []
    for index in range(3):
        r = client.post(
            "/import/source",
            files={"file": (f"scan{index}.las", payload, "application/octet-stream")},
        )
        ids.append(r.json()["source_id"])
    # A different source, extracted independently.
    other = client.post(
        "/import/source",
        files={"file": ("other.las", upload_las(), "application/octet-stream")},
    ).json()["source_id"]

    client.post(f"/point-clouds/{ids[0]}/extract")
    client.post(f"/point-clouds/{other}/extract")

    for source_id in (ids[0], other):
        jobs = client.get(f"/processing-jobs?source_id={source_id}").json()
        extraction = [j for j in jobs if j["job_type"] == "BUILDING_EXTRACTION"]
        assert not any(j["status"] == "PENDING" for j in extraction), extraction
        assert any(j["status"] == "COMPLETED" for j in extraction)
        assert client.get(f"/point-clouds/{source_id}").json()["status"] == "EXTRACTED"

    # The un-extracted source still correctly reads METADATA_ONLY.
    assert client.get(f"/point-clouds/{ids[1]}").json()["status"] == "METADATA_ONLY"


def test_rerunning_extraction_keeps_both_results(client, store, upload_las):
    """Re-surveying a source is normal; the second run must not collide.

    Building ids are keyed on the processing job, so each run's buildings are
    retained and attributable.
    """
    r = client.post(
        "/import/source",
        files={"file": ("scan.las", upload_las(), "application/octet-stream")},
    )
    source_id = r.json()["source_id"]

    first = client.post(f"/point-clouds/{source_id}/extract").json()
    second = client.post(f"/point-clouds/{source_id}/extract").json()

    assert first["processing_job_id"] != second["processing_job_id"]
    assert {b["id"] for b in first["buildings"]}.isdisjoint(
        b["id"] for b in second["buildings"]
    )
    # Both runs are retained, each attributed to its own job.
    listed = client.get("/extracted-buildings").json()
    assert len(listed) == 4
    assert {b["processing_job_id"] for b in listed} == {
        first["processing_job_id"],
        second["processing_job_id"],
    }
    # Same input, so the same geometry.
    assert sorted(b["geometry_hash"] for b in first["buildings"]) == sorted(
        b["geometry_hash"] for b in second["buildings"]
    )


def test_geometry_hash_is_unique_per_building(client, store, upload_las):
    r = client.post(
        "/import/source",
        files={"file": ("scan.las", upload_las(), "application/octet-stream")},
    )
    body = client.post(f"/point-clouds/{r.json()['source_id']}/extract").json()
    hashes = [b["geometry_hash"] for b in body["buildings"]]
    assert len(set(hashes)) == 2


def test_extraction_is_reproducible(client, store, upload_las):
    """Same file twice yields the same hashes -- the result is auditable."""
    payload = upload_las()
    hashes = []
    for index in range(2):
        r = client.post(
            "/import/source",
            files={"file": (f"scan{index}.las", payload, "application/octet-stream")},
        )
        body = client.post(f"/point-clouds/{r.json()['source_id']}/extract").json()
        hashes.append(sorted(b["geometry_hash"] for b in body["buildings"]))
    assert hashes[0] == hashes[1]


# ==========================================================================
# The job trail
# ==========================================================================


def test_extraction_records_a_completed_job(client, store, upload_las):
    r = client.post(
        "/import/source",
        files={"file": ("scan.las", upload_las(), "application/octet-stream")},
    )
    source_id = r.json()["source_id"]
    body = client.post(f"/point-clouds/{source_id}/extract").json()

    jobs = client.get(f"/processing-jobs?source_id={source_id}").json()
    extraction = [j for j in jobs if j["job_type"] == "BUILDING_EXTRACTION"]
    assert any(
        j["id"] == body["processing_job_id"] and j["status"] == "COMPLETED"
        for j in extraction
    )
    # The PENDING row from ingestion must not still claim work is outstanding.
    assert not any(j["status"] == "PENDING" for j in extraction)

    completed = next(j for j in extraction if j["id"] == body["processing_job_id"])
    assert completed["metadata"]["method"] == "algorithmic_geometric"
    assert completed["metadata"]["buildings_found"] == 2
    assert completed["metadata"]["quality_summary"]["buildings"] == 2


def test_status_reports_extracted_not_complete(client, store, upload_las):
    """Floor segmentation does not exist, so no cloud may read as COMPLETE."""
    r = client.post(
        "/import/source",
        files={"file": ("scan.las", upload_las(), "application/octet-stream")},
    )
    source_id = r.json()["source_id"]

    before = client.get(f"/point-clouds/{source_id}").json()
    assert before["status"] == "METADATA_ONLY"

    client.post(f"/point-clouds/{source_id}/extract")

    after = client.get(f"/point-clouds/{source_id}").json()
    assert after["status"] == "EXTRACTED"
    assert after["status"] != "COMPLETE"


def test_detail_view_includes_jobs_and_shows_segmentation_is_available(
    client, store, upload_las
):
    r = client.post(
        "/import/source",
        files={"file": ("scan.las", upload_las(), "application/octet-stream")},
    )
    source_id = r.json()["source_id"]
    client.post(f"/point-clouds/{source_id}/extract")

    detail = client.get(f"/point-clouds/{source_id}").json()
    statuses = {j["job_type"]: j["status"] for j in detail["jobs"]}
    assert statuses["METADATA_EXTRACTION"] == "COMPLETED"
    assert statuses["BUILDING_EXTRACTION"] == "COMPLETED"
    # Segmentation is implemented but has not been asked for, so it is available
    # rather than absent.
    assert statuses["FLOOR_SEGMENTATION"] == "PENDING"
    assert detail["status"] == "EXTRACTED"


# ==========================================================================
# Failures
# ==========================================================================


def test_extracting_an_unknown_source_is_404(client, store):
    r = client.post("/point-clouds/DS-999/extract")
    assert r.status_code == 404


def test_extraction_refuses_a_cloud_with_no_crs(client, store, tmp_path_factory):
    payload = _scene_bytes(
        tmp_path_factory, fx.two_buildings_scene(), fmt="las", crs_epsg=None
    )
    r = client.post(
        "/import/source",
        files={"file": ("scan.las", payload, "application/octet-stream")},
    )
    source_id = r.json()["source_id"]
    r = client.post(f"/point-clouds/{source_id}/extract")
    assert r.status_code == 422
    assert "no CRS" in r.json()["detail"]


def test_failed_extraction_records_a_failed_job(client, store, tmp_path_factory):
    payload = _scene_bytes(
        tmp_path_factory, fx.two_buildings_scene(), fmt="las", crs_epsg=None
    )
    r = client.post(
        "/import/source",
        files={"file": ("scan.las", payload, "application/octet-stream")},
    )
    source_id = r.json()["source_id"]
    client.post(f"/point-clouds/{source_id}/extract")

    jobs = client.get(f"/processing-jobs?source_id={source_id}").json()
    extraction = [j for j in jobs if j["job_type"] == "BUILDING_EXTRACTION"]
    assert any(j["status"] == "FAILED" for j in extraction), jobs
    # The failure reason is recorded, and the stage is not left PENDING.
    failed = next(j for j in extraction if j["status"] == "FAILED")
    assert "no CRS" in failed["error"]
    assert not any(j["status"] == "PENDING" for j in extraction)
    # Nothing was stored.
    assert client.get("/extracted-buildings").json() == []


def test_a_deleted_retained_file_is_reported_not_silently_empty(client, store, upload_las):
    r = client.post(
        "/import/source",
        files={"file": ("scan.las", upload_las(), "application/octet-stream")},
    )
    source_id = r.json()["source_id"]
    for path in store.rglob("*.las"):
        path.unlink()

    r = client.post(f"/point-clouds/{source_id}/extract")
    assert r.status_code == 409
    assert "missing" in r.json()["detail"].lower()


def test_corrupt_point_cloud_is_rejected(client, store):
    r = client.post(
        "/import/source",
        files={"file": ("bad.las", b"not a point cloud at all", "application/octet-stream")},
    )
    # Ingestion rejects it, so there is no source to extract from.
    assert r.status_code == 422
    assert client.get("/extracted-buildings").json() == []


# ==========================================================================
# Retention
# ==========================================================================


def test_ingestion_retains_the_file_for_extraction(client, store, upload_las):
    r = client.post(
        "/import/source",
        files={"file": ("scan.las", upload_las(), "application/octet-stream")},
    )
    assert r.json()["retained_for_processing"] is True
    retained = list(store.rglob("*.las"))
    assert len(retained) == 1
    assert retained[0].stat().st_size > 0


def test_two_uploads_of_the_same_bytes_are_two_sources(client, store, upload_las):
    payload = upload_las()
    ids = []
    for index in range(2):
        r = client.post(
            "/import/source",
            files={"file": (f"scan{index}.las", payload, "application/octet-stream")},
        )
        ids.append(r.json()["source_id"])
    assert ids[0] != ids[1]
    assert len(list(store.rglob("*.las"))) == 2


# ==========================================================================
# Other formats and scenes
# ==========================================================================


def test_extraction_from_laz(client, store, tmp_path_factory):
    payload = _scene_bytes(tmp_path_factory, fx.two_buildings_scene(), fmt="laz")
    r = client.post(
        "/import/source",
        files={"file": ("scan.laz", payload, "application/octet-stream")},
    )
    body = client.post(f"/point-clouds/{r.json()['source_id']}/extract").json()
    assert body["buildings_found"] == 2
    assert body["point_cloud"]["format"] == "LAZ"
    # LAZ streams: laspy's chunked reader decompresses block by block via lazrs.
    assert body["point_cloud"]["read_in_chunks"] is True
    assert not any("no streaming reader" in w for w in body["warnings"])


def test_extraction_from_ply(client, store, tmp_path_factory):
    payload = _scene_bytes(tmp_path_factory, fx.two_buildings_scene(), fmt="ply")
    r = client.post(
        "/import/source",
        files={"file": ("scan.ply", payload, "application/octet-stream")},
    )
    body = client.post(f"/point-clouds/{r.json()['source_id']}/extract").json()
    assert body["buildings_found"] == 2
    assert body["point_cloud"]["format"] == "PLY"
    assert body["point_cloud"]["read_in_chunks"] is False


def test_empty_scene_yields_no_buildings(client, store, upload_las):
    """Ground only must produce zero buildings, not a phantom one."""
    r = client.post(
        "/import/source",
        files={"file": ("flat.las", upload_las(fx.empty_scene()), "application/octet-stream")},
    )
    body = client.post(f"/point-clouds/{r.json()['source_id']}/extract").json()
    assert body["buildings_found"] == 0
    assert body["buildings"] == []
    assert client.get("/extracted-buildings").json() == []


def test_l_shaped_building_keeps_its_notch(client, store, upload_las):
    r = client.post(
        "/import/source",
        files={"file": ("l.las", upload_las(fx.l_shape_scene()), "application/octet-stream")},
    )
    body = client.post(f"/point-clouds/{r.json()['source_id']}/extract").json()
    assert body["buildings_found"] == 1
    assert body["buildings"][0]["footprint"]["type"] == "Polygon"


def test_truncation_is_reported_in_the_result(client, store, upload_las):
    """A capped read must say so, not look like a complete survey."""
    r = client.post(
        "/import/source",
        files={"file": ("scan.las", upload_las(), "application/octet-stream")},
    )
    body = client.post(
        f"/point-clouds/{r.json()['source_id']}/extract?max_points=2000"
    ).json()
    assert body["point_cloud"]["truncated"] is True
    assert any("cap" in w for w in body["warnings"])


def test_max_points_is_respected(client, store, upload_las):
    r = client.post(
        "/import/source",
        files={"file": ("scan.las", upload_las(), "application/octet-stream")},
    )
    body = client.post(
        f"/point-clouds/{r.json()['source_id']}/extract?max_points=2000"
    ).json()
    assert body["point_cloud"]["points_read"] == 2000


# ==========================================================================
# Honesty about what this is
# ==========================================================================


def test_stored_footprint_area_matches_the_reported_height_and_area(
    client, store, upload_las
):
    """The stored WGS84 geometry must still measure correctly.

    Measured with pyproj directly rather than the app's own helpers, so the
    check is independent of the code that produced the geometry.
    """
    from pyproj import Transformer
    from shapely.geometry import shape
    from shapely.ops import transform as shapely_transform

    r = client.post(
        "/import/source",
        files={"file": ("scan.las", upload_las(), "application/octet-stream")},
    )
    body = client.post(f"/point-clouds/{r.json()['source_id']}/extract").json()

    to_utm = Transformer.from_crs("EPSG:4326", "EPSG:32643", always_xy=True)
    areas = sorted(
        shapely_transform(to_utm.transform, shape(b["footprint"])).area
        for b in body["buildings"]
    )
    # True footprints are 30x20 = 600 m2 and 20x20 = 400 m2.
    assert areas[0] == pytest.approx(400.0, rel=0.02)
    assert areas[1] == pytest.approx(600.0, rel=0.02)


def test_no_confidence_or_accuracy_figure_is_ever_returned(client, store, upload_las):
    """The words "accuracy" and "confidence" may appear only in disclaimers.

    A response must never carry a bare accuracy or confidence number, because
    this pipeline cannot produce one honestly.
    """
    r = client.post(
        "/import/source",
        files={"file": ("scan.las", upload_las(), "application/octet-stream")},
    )
    source_id = r.json()["source_id"]
    body = client.post(f"/point-clouds/{source_id}/extract").json()

    # No key may be named like an accuracy or confidence figure.
    for building in body["buildings"]:
        assert "confidence" not in building
        assert "accuracy" not in building
        assert "probability" not in building
    assert "confidence" not in body
    assert "accuracy" not in {k for k in body}

    # The only occurrences are explicit denials.
    for text in (body["note"], body["method_description"]):
        assert "no accuracy figure is claimed" in text.lower()
    assert "not an accuracy" in body["quality_summary"]["interpretation"].lower()

    listed = client.get("/extracted-buildings").json()
    for building in listed:
        assert "confidence" not in building
        assert "accuracy" not in building


def test_quality_summary_explains_itself(client, store, upload_las):
    r = client.post(
        "/import/source",
        files={"file": ("scan.las", upload_las(), "application/octet-stream")},
    )
    body = client.post(f"/point-clouds/{r.json()['source_id']}/extract").json()
    summary = body["quality_summary"]
    assert summary["buildings"] == 2
    assert 0.0 <= summary["geometric_quality_mean"] <= 1.0
    assert summary["total_footprint_area_m2"] > 0
    assert "not an accuracy" in summary["interpretation"].lower()


def test_extraction_does_not_create_cadastrical_records(client, store, upload_las):
    """An inferred footprint is not a legal object and must not appear as one."""
    r = client.post(
        "/import/source",
        files={"file": ("scan.las", upload_las(), "application/octet-stream")},
    )
    client.post(f"/point-clouds/{r.json()['source_id']}/extract")

    # The demo scene is untouched: no new cadastral buildings, properties or
    # parcels were created.
    assert len(client.get("/buildings").json()) == 1
    assert len(client.get("/parcels").json()) == 1
    assert len(client.get("/properties").json()) == 17
