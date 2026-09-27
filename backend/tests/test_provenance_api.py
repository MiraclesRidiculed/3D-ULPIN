"""API contract for provenance and lineage retrieval.

Read-only, and asserted as such from the OpenAPI document: provenance is a record
of what already happened, so there must be no route that writes, edits or
deletes a link.
"""
from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from app.models.enums import STAGE_ORDER, ProvenanceStage

from tests.fixtures import buildings as fx

CRS = "EPSG:32643"


@pytest.fixture
def store(tmp_path, monkeypatch):
    from app.services import point_cloud_store as pc_store

    root = tmp_path / "prov-store"
    root.mkdir()
    monkeypatch.setattr(pc_store, "DEFAULT_STORE_DIR", root)
    return root


@pytest.fixture
def scene(tmp_path_factory, store):
    directory = tmp_path_factory.mktemp("prov-api")
    return fx.write_las_storey_scene(
        directory / "scan.las", fx.three_storey_building()
    ).read_bytes()


@pytest.fixture
def pipeline_client(client, scene, store):
    """A client whose store has a fully-derived pipeline to trace."""
    response = client.post(
        f"/import/source?crs={CRS}",
        files={"file": ("scan.las", io.BytesIO(scene), "application/octet-stream")},
    )
    assert response.status_code == 200, response.text
    source_id = response.json()["source_id"]
    assert client.post(f"/point-clouds/{source_id}/extract").status_code == 200
    assert client.post(f"/point-clouds/{source_id}/segment-floors").status_code == 200
    assert client.post(f"/point-clouds/{source_id}/property-volumes").status_code == 200
    return source_id


# ==========================================================================
# The vocabulary
# ==========================================================================


def test_stages_endpoint_lists_the_chain_in_order(client):
    body = client.get("/provenance/stages").json()
    assert body["stages"] == list(STAGE_ORDER)
    assert len(body["stages"]) == 8
    assert body["stage_collections"]["DATA_SOURCE"] == "sources"
    assert "rather than an inferred origin" in body["note"]


# ==========================================================================
# Retrieval
# ==========================================================================


def test_lineage_of_a_derived_volume(pipeline_client, client):
    source_id = pipeline_client
    volumes = client.get("/generated-property-volumes").json()
    assert volumes
    body = client.get(f"/provenance/{volumes[0]['id']}/lineage").json()
    assert body["complete"] is True
    assert body["source_id"] == source_id
    stages = [n["stage"] for n in body["lineage"]]
    assert stages[0] == ProvenanceStage.DATA_SOURCE.value
    assert ProvenanceStage.PROPERTY_VOLUME.value in stages


def test_lineage_stages_endpoint_reports_what_is_missing(pipeline_client, client):
    volumes = client.get("/generated-property-volumes").json()
    body = client.get(f"/provenance/{volumes[0]['id']}/lineage/stages").json()
    assert ProvenanceStage.DATA_SOURCE.value in body["stages"]
    assert body["stages"] == [s for s in STAGE_ORDER if s in body["stages"]]
    assert body["complete"] is True
    # This pipeline raises no finding, so the later stages are genuinely absent
    # and are reported as such rather than assumed.
    assert ProvenanceStage.REVIEW.value in body["missing_stages"]


def test_provenance_records_carry_the_required_fields(pipeline_client, client):
    volumes = client.get("/generated-property-volumes").json()
    links = client.get(f"/provenance/{volumes[0]['id']}").json()
    assert links
    for link in links:
        for field in (
            "source_id",
            "processing_job_id",
            "algorithm",
            "model_name",
            "model_version",
            "created_at",
            "parameters",
        ):
            assert field in link, field
        assert link["algorithm"]
        assert link["created_at"]


def test_no_model_information_is_returned(pipeline_client, client):
    """Nothing in the system names a model, so nothing returned may either."""
    volumes = client.get("/generated-property-volumes").json()
    for link in client.get(f"/provenance/{volumes[0]['id']}").json():
        assert link["model_name"] is None
        assert link["model_version"] is None


def test_source_objects_endpoint(pipeline_client, client):
    source_id = pipeline_client
    volumes = client.get("/generated-property-volumes").json()
    body = client.get(f"/provenance/{volumes[0]['id']}/sources").json()
    assert body["source_ids"] == [source_id]


def test_processing_history_endpoint(pipeline_client, client):
    volumes = client.get("/generated-property-volumes").json()
    history = client.get(f"/provenance/{volumes[0]['id']}/processing").json()
    assert history
    assert all(h["processing_job_id"] for h in history)
    assert all(h["status"] for h in history)


def test_derived_objects_endpoint(pipeline_client, client):
    source_id = pipeline_client
    body = client.get(f"/provenance/source/{source_id}/derived").json()
    assert body["source_id"] == source_id
    assert body["total_objects"] >= 3
    stages = {g["stage"] for g in body["stages"]}
    assert ProvenanceStage.BUILDING.value in stages
    assert ProvenanceStage.FLOOR.value in stages
    assert ProvenanceStage.PROPERTY_VOLUME.value in stages


def test_derived_objects_can_be_scoped_to_a_stage(pipeline_client, client):
    source_id = pipeline_client
    body = client.get(
        f"/provenance/source/{source_id}/derived?stage={ProvenanceStage.BUILDING.value}"
    ).json()
    assert len(body["stages"]) == 1
    assert body["stages"][0]["stage"] == ProvenanceStage.BUILDING.value


def test_an_unknown_stage_filter_is_422(client):
    assert client.get("/provenance/source/DS-1/derived?stage=NOPE").status_code == 422


# ==========================================================================
# Gaps are reported
# ==========================================================================


def test_an_object_with_no_provenance_reports_none(client):
    body = client.get("/provenance/P-404/lineage").json()
    assert body["lineage"] == []
    assert body["complete"] is False
    assert body["source_id"] is None
    assert "rather than inferred" in body["note"]


def test_a_seeded_record_has_no_invented_lineage(client):
    for object_id in ("P-001", "B-001", "PV-101"):
        body = client.get(f"/provenance/{object_id}/lineage").json()
        assert body["complete"] is False
        assert body["source_id"] is None


def test_an_unknown_source_has_no_derived_objects(client):
    body = client.get("/provenance/source/DS-NOPE/derived").json()
    assert body["total_objects"] == 0
    assert body["stages"] == []


# ==========================================================================
# Read-only, and the demo scene is untouched
# ==========================================================================


def test_there_is_no_route_that_writes_provenance(client):
    """Asserted from the OpenAPI document, so one cannot be added quietly."""
    spec = client.get("/openapi.json").json()
    for path, operations in spec["paths"].items():
        if not path.startswith("/provenance"):
            continue
        assert set(operations) == {"get"}, f"{path} exposes {sorted(operations)}"


def test_the_demo_scene_reports_no_fabricated_lineage(client):
    """A demo load must not invent a source for seeded records."""
    client.get("/demo/load")
    for object_id in ("P-001", "B-001"):
        body = client.get(f"/provenance/{object_id}/lineage").json()
        assert body["source_id"] is None
        assert body["complete"] is False
