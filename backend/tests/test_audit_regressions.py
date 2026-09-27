"""Regressions for the defects the full-pipeline audit found.

Each test here corresponds to a bug that was live, silent, and uncovered:

1.  **Storey primary keys collided.** The storey id took the building's *last
    dash-separated segment* as its discriminator. An extracted building id ends in
    that segment (``XB-{job}-001``), so it was ``001`` for the first building of
    every job. One segmentation job handed buildings left by two extraction jobs
    therefore produced the same storey id twice: a silent duplicate row in memory
    and a ``UniqueViolation``/HTTP 500 under PostGIS.

2.  **The two backends disagreed on a duplicate key.** ``InMemoryRepository.add``
    appended unconditionally while the PostGIS adapter raised. One request, two
    rows on one backend and a 500 on the other.

3.  **A re-run re-issued a generated volume's ULPIN.** ``volume_key`` embedded
    the job-scoped ``building_id``, so re-processing one source produced a new
    identifier for an unchanged volume and defeated the replace-not-accumulate
    upsert -- the project's central identity claim, false.

4.  **The API served fabricated accuracies.** ``confidence: 0.974`` on the seeded
    building, ``0.918`` on all 17 property volumes, ``average_confidence: 91.8``
    from analytics, a ``model`` key naming a model that does not exist, and eight
    hardcoded storeys on ``GET /floors``.

5.  **``app.services.__all__`` was not importable.** Four names were declared and
    never defined, so ``from app.services import *`` raised ``AttributeError``.

The cross-stage behaviour lives in ``test_pipeline_integration.py``; this module
pins the units underneath it.
"""
from __future__ import annotations

import re
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from app.repositories.base import DuplicateRecord
from app.repositories.memory import InMemoryRepository
from app.services import property_volumes as pv
from app.services.demo import seed_demo
from app.services.property_volume_service import _extraction_ordinal


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


# ==========================================================================
# 1. storey primary keys must be unique
# ==========================================================================


def test_the_two_building_ids_that_caused_the_collision():
    """Pins the precondition, so the regression below keeps its meaning.

    If these ever stop sharing a trailing segment the collision becomes
    impossible, and a test that only asserted "no duplicates" would pass for the
    wrong reason.
    """
    a = "XB-JOB-aaa111-001"
    b = "XB-JOB-bbb222-001"
    assert a.split("-")[-1] == b.split("-")[-1] == "001"
    assert a != b, "but the full ids differ, so the collision was a derivation bug"


def test_the_storey_id_is_not_built_from_the_trailing_segment():
    """The fix, asserted against the source rather than one call site.

    A behavioural test would pass if someone reintroduced the collision for a
    building shape this module does not construct. Reading the id builder keeps
    the guarantee attached to the rule.
    """
    import inspect

    from app.services import floor_segmentation_service as fss

    source = inspect.getsource(fss)
    assert "building_id.split(\"-\")[-1]" not in source
    assert "building_id.split('-')[-1]" not in source, (
        "the storey id must not be keyed on a job-scoped id's trailing segment"
    )


# ==========================================================================
# 2. both backends must reject a duplicate primary key
# ==========================================================================


def test_in_memory_add_rejects_a_duplicate_primary_key():
    """It used to append, which is how the two backends came to disagree."""
    repo = InMemoryRepository()
    repo.add("parcels", {"id": "p1", "parcel_id": "P-1"})
    with pytest.raises(DuplicateRecord):
        repo.add("parcels", {"id": "p1", "parcel_id": "P-2"})
    assert len(repo.records("parcels")) == 1, "no second row may be appended"


def test_duplicate_record_names_the_collection_and_id():
    exc = DuplicateRecord("extracted_floors", "XF-1")
    assert exc.collection == "extracted_floors"
    assert exc.object_id == "XF-1"
    assert "XF-1" in str(exc)


def test_the_postgres_adapter_raises_the_same_error():
    """Checked without a database, by reading the guard.

    The behavioural half needs PostGIS and lives in
    ``test_postgres_integration``; this asserts the code path exists so a
    regression that removes the check is caught even with no database.
    """
    import inspect

    from app.repositories.postgres import PostgresCadastreRepository

    assert "DuplicateRecord" in inspect.getsource(PostgresCadastreRepository.add)


# ==========================================================================
# 3. a generated volume's identity must survive re-processing
# ==========================================================================


def test_stable_building_ref_does_not_depend_on_the_job():
    assert pv.stable_building_ref("DS-001", 1) == "DS-001-B01"
    assert pv.stable_building_ref("DS-001", 1) == pv.stable_building_ref("DS-001", 1)
    # A different source is a different observation and stays distinct.
    assert pv.stable_building_ref("DS-002", 1) != "DS-001-B01"
    # A different building in the same source is distinct.
    assert pv.stable_building_ref("DS-001", 2) != "DS-001-B01"
    # The job id must never leak into the identity anchor.
    assert "JOB" not in pv.stable_building_ref("DS-001", 1)


def test_extraction_ordinal_is_read_from_the_building_id_not_the_list():
    """The second version of this fix got this wrong and was caught by the
    end-to-end test. Pinned here so the mistake is cheap to see.

    Indexing the ordinal against the *accumulated* building list made the ref
    depend on how many times the pipeline had been run, so the same physical
    building was B01 then B03 and its ULPIN changed with it.
    """
    assert _extraction_ordinal("XB-JOB-abc-001", fallback=99) == 1
    assert _extraction_ordinal("XB-JOB-abc-007", fallback=99) == 7
    assert _extraction_ordinal("XB-odd", fallback=4) == 4


def test_volume_key_is_built_from_the_ref():
    key = pv.volume_key("DS-001-B01", 2, None)
    assert key == "DS-001-B01-F02"
    # The storey is in the key, or every floor's first unit would collide.
    assert pv.volume_key("DS-001-B01", 3, None) != key
    assert pv.volume_key("DS-001-B01", 2, "01") != key


def test_create_volume_from_footprint_falls_back_to_the_building_id():
    """A direct caller with no ref keeps working rather than getting a broken key."""
    from app.services.geometry import create_rectangle, polygon_to_geojson

    volume = pv.create_volume_from_footprint(
        create_rectangle(0, 0, 10, 10),
        z_min=0.0,
        z_max=3.0,
        source_crs="EPSG:32643",
        parent_parcel_id="P-001",
        building_id="B-001",
        floor_number=1,
    )
    assert volume is not None
    assert "B001" in volume.id


# ==========================================================================
# 4. no fabricated accuracy anywhere on the API
# ==========================================================================


def test_no_endpoint_reports_a_fabricated_confidence(client):
    """Every response is walked, because the fabrication was in five places."""
    paths = [
        "/buildings",
        "/properties",
        "/parcels",
        "/floors",
        "/infrastructure",
        "/analytics/summary",
        "/extracted-buildings",
        "/extracted-floors",
        "/generated-property-volumes",
        "/data-sources",
        "/validation/issues",
        "/reviews",
        "/changes",
        "/audit/events",
        "/processing-jobs",
    ]
    for path in paths:
        body = client.get(path).json()
        records = body if isinstance(body, list) else [body]
        for record in records:
            if not isinstance(record, dict):
                continue
            assert "confidence" not in record, f"{path} still reports a confidence"
            assert "average_confidence" not in record, path
            assert "accuracy" not in record, path
            assert "model" not in record, f"{path} names a model that is not used"

    for stage in (
        "building-extraction",
        "floor-segmentation",
        "vertical-delineation",
    ):
        body = client.post(f"/processing/{stage}").json()
        assert "confidence" not in body, stage
        assert "model" not in body, stage
        assert body["adapter"] is True


def test_no_seed_record_carries_a_confidence():
    """Guards the source, not just the response.

    A value can be absent from a response because a projection dropped it. This
    asserts the seed never writes one, so the field cannot reappear the moment
    anything reads the store directly.
    """
    repo = InMemoryRepository()
    seed_demo(repo)
    for kind in ("parcels", "buildings", "properties", "floors", "infrastructure"):
        for record in repo.records(kind):
            assert "confidence" not in record, f"{kind}/{record.get('id')}"


def test_the_published_openapi_declares_no_accuracy_field():
    """The contract itself, so a schema cannot reintroduce one."""
    from app.main import app

    blob = str(app.openapi())
    assert not re.search(r'"(confidence|accuracy|score|probability)"', blob), (
        "the OpenAPI document still declares an accuracy-shaped field"
    )
    assert "average_confidence" not in blob


def test_floors_are_read_from_the_store_not_hardcoded(client):
    """``GET /floors`` returned a fixed 8 x 3.2 m stack whatever the scene held."""
    body = client.get("/floors").json()
    assert len(body) == 8
    assert all(f.get("id") for f in body), (
        "rows without an id cannot have come from stored records"
    )
    assert {f["building_id"] for f in body} == {"B-001"}
    assert [f["floor_number"] for f in body] == list(range(1, 9))


def test_geometric_quality_survives_the_removal():
    """The honest measure must not have been collaterally removed.

    Removing the fabricated ``confidence`` must not have taken the real regularity
    score with it. ``measure_building_quality`` scores a *cluster* of points
    against its footprint, so this builds both -- an intact building's ring of
    points, and a scattered cloud.
    """
    import math

    import numpy as np

    from app.services.geometry import create_rectangle
    from app.services.point_cloud_extraction import (
        PointCluster,
        measure_building_quality,
    )

    def _cluster(points: list[tuple[float, float]]) -> PointCluster:
        """Wrap a point set the way the DBSCAN stage hands it over.

        ``PointCluster`` takes numpy arrays and a precomputed height, not a point
        list, so the shape of this constructor is part of what the test pins.
        """
        xy = [(x, y) for x, y in points]
        n = len(xy)
        return PointCluster(
            index=0,
            x=np.array([p[0] for p in xy], dtype=float),
            y=np.array([p[1] for p in xy], dtype=float),
            z=np.zeros(n, dtype=float),
            height_above_ground=np.ones(n, dtype=float),
        )

    def _ring(cx: float, cy: float, n: int = 48) -> PointCluster:
        points = [
            (
                cx + 5 * math.cos(2 * math.pi * i / n),
                cy + 5 * math.sin(2 * math.pi * i / n),
            )
            for i in range(n)
        ]
        points.append((cx, cy))
        return _cluster(points)

    regular = measure_building_quality(
        _ring(0.0, 0.0), create_rectangle(-5, -5, 5, 5), source_crs="EPSG:32643"
    )
    scattered = measure_building_quality(
        _cluster([(x * 37 % 100.0, y * 53 % 100.0) for x in range(25) for y in range(25)]),
        create_rectangle(0, 0, 100, 100),
        source_crs="EPSG:32643",
    )
    for score in (regular, scattered):
        assert 0.0 <= score["geometric_quality"] <= 1.0, score
    assert (
        regular["geometric_quality"] > scattered["geometric_quality"]
    ), "geometric_quality must still separate a regular shape from a scattered one"


def test_the_demo_scene_is_untouched_by_the_audit():
    """The audit must not have disturbed the deliberate conflicts."""
    repo = InMemoryRepository()
    seed_demo(repo)
    assert len(repo.records("parcels")) == 1
    assert len(repo.records("buildings")) == 1
    assert len(repo.records("properties")) == 17
    apartments = [p for p in repo.records("properties") if p["property_type"] == "APARTMENT"]
    assert len(apartments) == 16


# ==========================================================================
# 5. the package's declared surface is real
# ==========================================================================


def test_every_name_in_all_is_importable():
    import app.services as services

    missing = [name for name in services.__all__ if not hasattr(services, name)]
    assert not missing, f"declared in __all__ but not importable: {missing}"


def test_a_star_import_actually_works():
    """It raised ``AttributeError`` on the first phantom name."""
    result = subprocess.run(
        [sys.executable, "-c", "from app.services import *"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr[-400:]


def test_shape_to_geojson_is_re_exported():
    import app.services as services

    assert hasattr(services, "shape_to_geojson")
    assert "shape_to_geojson" in services.__all__


# ==========================================================================
# 6. contract honesty
# ==========================================================================


def test_ulpin_generate_honours_parent_parcel_id(client):
    """The body used to be accepted and discarded (the parameter was ``_``)."""
    whole = client.post("/ulpin/generate", json={}).json()
    assert whole["assigned"] >= 0

    scoped = client.post("/ulpin/generate", json={"parent_parcel_id": "P-001"}).json()
    assert "assigned" in scoped
    unknown = client.post("/ulpin/generate", json={"parent_parcel_id": "NOPE"}).json()
    assert unknown["assigned"] == 0, (
        "an unknown parcel must assign nothing rather than silently widening to the scene"
    )


def test_no_route_documents_an_unimplemented_stage(client):
    """Two docstrings still claimed floor segmentation did not exist."""
    blob = str(client.get("/openapi.json").json())
    assert "Floor segmentation is not implemented" not in blob


def test_extracted_buildings_have_one_declared_shape(client):
    """``GET`` returned the raw record and ``POST .../extract`` the filtered one,
    so a field's absence was ambiguous: missing, or simply not in the contract."""
    from app.models.schemas import ExtractedBuilding

    allowed = set(ExtractedBuilding.model_fields)
    records = client.get("/extracted-buildings").json()
    for record in records:
        assert set(record).issubset(allowed), (
            f"GET /extracted-buildings returned fields outside the declared schema: "
            f"{sorted(set(record) - allowed)}"
        )
