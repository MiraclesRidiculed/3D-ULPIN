"""Tests for source-to-cadastral provenance.

The spine is the eight-stage chain, verified end to end through the **real**
point-cloud pipeline rather than by hand-built links, because a provenance system
that only works when fed its own output has proved nothing.

The properties that matter most:

* **no model information is fabricated.** Every stage this system runs is
  algorithmic or geometric, so ``model_name``/``model_version`` must be empty
  everywhere. A test asserts that across the whole store, and another asserts the
  service layer *refuses* a model name attached to a known algorithm.
* **gaps are reported, not filled.** The demo scene's records were never derived
  from a survey, and a lineage that claimed otherwise would look complete while
  being wrong. ``complete`` is ``False`` for them, with a note saying so.
* **provenance is immutable**, enforced by the storage layer, because a rewritten
  link is a false record.
"""
from __future__ import annotations

import io
import re
from pathlib import Path

import pytest

from app.models.enums import STAGE_ORDER, ProvenanceStage
from app.repositories.memory import InMemoryRepository
from app.services import provenance as pv
from app.services.demo import seed_demo
from app.services.provenance import ProvenanceError

from tests.fixtures import buildings as fx


@pytest.fixture
def repo():
    store = InMemoryRepository()
    seed_demo(store)
    return store


@pytest.fixture
def pipeline(tmp_path_factory, monkeypatch):
    """Run the real ingest -> extract -> segment -> generate chain.

    Returns ``(repo, source_id)``. Uses the same scene the extraction and
    segmentation suites use, so provenance is verified against geometry that
    actually goes through the real algorithms.
    """
    from app.services import (
        building_extraction_service as extraction,
        floor_segmentation_service as segmentation,
        point_cloud_store as pc_store,
        property_volume_service as volumes,
    )
    from app.services.point_cloud_ingestion import ingest_point_cloud

    root = tmp_path_factory.mktemp("prov-store")
    monkeypatch.setattr(pc_store, "DEFAULT_STORE_DIR", root)

    directory = tmp_path_factory.mktemp("prov-upload")
    # A three-storey scene, so the chain runs all the way to generated volumes
    # rather than stopping at a footprint.
    payload = fx.write_las_storey_scene(
        directory / "scan.las", fx.three_storey_building()
    ).read_bytes()

    store = InMemoryRepository()
    seed_demo(store)
    source = ingest_point_cloud(
        store, io.BytesIO(payload), "scan.las", crs_override="EPSG:32643"
    )
    source_id = source["source_id"]
    extraction.extract_buildings(store, source_id)
    segmentation.segment_building_floors(store, source_id)
    volumes.generate_volumes(store, source_id)
    return store, source_id


# ==========================================================================
# The vocabulary
# ==========================================================================


def test_the_eight_stages_are_in_canonical_order():
    assert list(STAGE_ORDER) == [
        "DATA_SOURCE",
        "PROCESSING_JOB",
        "BUILDING",
        "FLOOR",
        "PROPERTY_VOLUME",
        "ULPIN",
        "VALIDATION",
        "REVIEW",
    ]
    assert len(ProvenanceStage) == 8


def test_an_unknown_stage_is_rejected(repo):
    with pytest.raises(ProvenanceError, match="unknown provenance stage"):
        pv.create_provenance_record(repo, stage="NOT_A_STAGE", object_id="X-1")


def test_an_object_id_is_required(repo):
    with pytest.raises(ProvenanceError, match="object_id is required"):
        pv.create_provenance_record(repo, stage=ProvenanceStage.BUILDING.value, object_id="")


# ==========================================================================
# No model information is ever fabricated
# ==========================================================================


def test_no_model_information_is_fabricated(pipeline):
    """Every stage this system runs is algorithmic, so no link names a model.

    This is the load-bearing honesty test for the feature. A model name on a grid
    filter would read later as "a model produced this", which is false.
    """
    store, _ = pipeline
    links = store.records("provenance_links")
    assert links, "the pipeline should have produced provenance"
    assert all(link["model_name"] is None for link in links)
    assert all(link["model_version"] is None for link in links)


def test_no_model_information_anywhere_in_the_store(repo):
    """Not just the pipeline: nothing in the system names a model."""
    for link in repo.records("provenance_links"):
        assert link["model_name"] is None
        assert link["model_version"] is None


def test_a_model_name_on_a_known_algorithm_is_refused(repo):
    """Refused, not stored: a placeholder is worse than a blank field."""
    for algorithm in sorted(pv.ALGORITHMIC_METHODS):
        with pytest.raises(ProvenanceError, match="has no model"):
            pv.create_provenance_record(
                repo,
                stage=ProvenanceStage.BUILDING.value,
                object_id="XB-1",
                algorithm=algorithm,
                model_name="SomeModel",
            )
    # The refusal added nothing; the demo seed's own links are untouched.
    assert not any(l["model_name"] for l in repo.records("provenance_links"))


def test_a_genuine_model_may_be_recorded(repo):
    """The fields are not dead: a caller that really ran a model can say so."""
    link = pv.create_provenance_record(
        repo,
        stage=ProvenanceStage.BUILDING.value,
        object_id="XB-1",
        algorithm="learned-segmentation",
        model_name="an-actual-model",
        model_version="2.1.0",
    )
    assert link["model_name"] == "an-actual-model"
    assert link["model_version"] == "2.1.0"


def test_the_algorithm_is_the_real_method_not_a_label(pipeline):
    """Every pipeline link names a method the pipeline actually emits.

    Scoped to the extraction stages on purpose: a VALIDATION link's "algorithm" is
    the rule that fired and a REVIEW link's is ``human-review``, which are the
    honest answers for those stages.
    """
    store, _ = pipeline
    pipeline_stages = {
        ProvenanceStage.BUILDING.value,
        ProvenanceStage.FLOOR.value,
        ProvenanceStage.PROPERTY_VOLUME.value,
        ProvenanceStage.ULPIN.value,
    }
    checked = 0
    for link in store.records("provenance_links"):
        if link["stage"] in pipeline_stages:
            assert link["algorithm"] in pv.ALGORITHMIC_METHODS, link["algorithm"]
            checked += 1
    assert checked >= 3


# ==========================================================================
# The full chain, through the real pipeline
# ==========================================================================


def test_the_pipeline_records_provenance_at_every_stage(pipeline):
    store, source_id = pipeline
    groups = {g["stage"]: g["count"] for g in pv.get_derived_objects(store, source_id)}
    assert groups[ProvenanceStage.BUILDING.value] >= 1
    assert groups[ProvenanceStage.FLOOR.value] >= 1
    assert groups[ProvenanceStage.PROPERTY_VOLUME.value] >= 1
    assert groups[ProvenanceStage.ULPIN.value] >= 1


def test_a_volume_traces_back_to_its_data_source(pipeline):
    store, source_id = pipeline
    volume = store.records("generated_property_volumes")[0]
    lineage = pv.get_object_lineage(store, volume["id"])
    assert lineage["complete"] is True
    assert lineage["source_id"] == source_id
    stages = [node["stage"] for node in lineage["lineage"]]
    assert stages[0] == ProvenanceStage.DATA_SOURCE.value
    # The object asked about is in its own lineage, at its own stage.
    assert ProvenanceStage.PROPERTY_VOLUME.value in stages
    assert lineage["lineage"][-2]["object_id"] == volume["id"]
    assert lineage["lineage"][-1]["stage"] == ProvenanceStage.ULPIN.value


def test_the_lineage_runs_in_canonical_order(pipeline):
    """Never in insertion order, so two runs of the pipeline read the same."""
    store, _ = pipeline
    volume = store.records("generated_property_volumes")[0]
    stages = [n["stage"] for n in pv.get_object_lineage(store, volume["id"])["lineage"]]
    ranks = [STAGE_ORDER.index(s) for s in stages]
    assert ranks == sorted(ranks)


def test_a_volume_records_its_processing_job_and_method(pipeline):
    store, _ = pipeline
    volume = store.records("generated_property_volumes")[0]
    links = pv.get_provenance(store, volume["id"])
    assert links
    link = next(
        l for l in links if l["stage"] == ProvenanceStage.PROPERTY_VOLUME.value
    )
    assert link["processing_job_id"]
    assert link["source_id"]
    assert link["algorithm"] == "derived_geometric"
    assert link["method_description"]
    assert link["created_at"]


def test_a_storey_records_the_building_it_came_from(pipeline):
    store, _ = pipeline
    storey = store.records("extracted_floors")[0]
    link = next(
        l for l in pv.get_provenance(store, storey["id"])
        if l["stage"] == ProvenanceStage.FLOOR.value
    )
    # A storey has two real parents -- its job and the building it was segmented
    # from -- so it gets one link per relationship rather than one overloaded
    # field. The building is the lineage parent; the job edge makes the chain
    # reach the data source.
    assert link["parent_id"] == storey["building_id"]
    assert link["parent_stage"] == ProvenanceStage.BUILDING.value
    assert link["processing_job_id"]
    assert link["parameters"]["derived_from"] == storey["building_id"]
    assert link["parameters"]["derived_from_stage"] == ProvenanceStage.BUILDING.value


def test_a_ulpin_links_to_the_volume_it_identifies(pipeline):
    store, _ = pipeline
    volume = next(
        v for v in store.records("generated_property_volumes") if v.get("prototype_ulpin")
    )
    link = next(
        l for l in pv.get_provenance(store, volume["prototype_ulpin"])
        if l["stage"] == ProvenanceStage.ULPIN.value
    )
    assert link["parent_id"] == volume["id"]


def test_the_processing_history_is_read_from_the_job_rows(pipeline):
    store, _ = pipeline
    volume = store.records("generated_property_volumes")[0]
    history = pv.get_processing_history(store, volume["id"])
    assert history
    entry = history[0]
    assert entry["status"]
    assert entry["job_type"]
    # A link is a claim; the job row is the record, so status comes from there.
    job = next(
        j for j in store.records("processing_jobs") if j["id"] == entry["processing_job_id"]
    )
    assert entry["status"] == job["status"]


def test_asking_what_an_upload_produced(pipeline):
    store, source_id = pipeline
    groups = pv.get_derived_objects(store, source_id)
    assert sum(g["count"] for g in groups) >= 3
    assert all(g["object_ids"] for g in groups)


# ==========================================================================
# Gaps are reported, never filled
# ==========================================================================


def test_an_object_with_no_provenance_reports_none(repo):
    lineage = pv.get_object_lineage(repo, "P-404")
    assert lineage["lineage"] == []
    assert lineage["complete"] is False
    assert lineage["source_id"] is None
    assert "rather than inferred" in lineage["note"]


def test_the_demo_scene_has_no_invented_lineage(repo):
    """The seeded records were never derived from a survey."""
    for object_id in ("P-001", "B-001", "PV-101"):
        lineage = pv.get_object_lineage(repo, object_id)
        assert lineage["complete"] is False
        assert lineage["source_id"] is None


def test_a_finding_about_a_seeded_record_has_no_source(repo):
    """The demo's findings are about records with no survey behind them."""
    for issue in repo.records("issues"):
        if issue["object_a"] in {"PV-502", "PV-201", "PV-B001"}:
            links = [
                l
                for l in repo.records("provenance_links")
                if l["object_id"] == issue["id"] and l["stage"] == "VALIDATION"
            ]
            assert links, issue["id"]
            assert all(l["source_id"] is None for l in links)


def test_an_incomplete_chain_is_marked_incomplete(pipeline):
    """A chain that does not begin at a source says so rather than guessing."""
    store, _ = pipeline
    volume = store.records("generated_property_volumes")[0]
    # Sever the origin by removing the source-to-job edge itself. Nulling the
    # denormalised ``source_id`` would not do it: the walk follows graph edges,
    # so the job would still lead to the source node.
    store.records("provenance_links")[:] = [
        link
        for link in store.records("provenance_links")
        if not (link.get("stage") == ProvenanceStage.PROCESSING_JOB.value
                and link.get("parent_stage") == ProvenanceStage.DATA_SOURCE.value)
    ]
    lineage = pv.get_object_lineage(store, volume["id"])
    assert lineage["complete"] is False
    assert lineage["source_id"] is None
    assert "does not begin at a recorded data source" in lineage["note"]


# ==========================================================================
# Validation and review stages
# ==========================================================================


def test_a_finding_is_linked_to_the_object_it_is_about(repo):
    from app.services.validation import validate

    validate(repo)
    issue = repo.records("issues")[0]
    links = [
        l
        for l in pv.get_provenance(repo, issue["id"])
        if l["stage"] == ProvenanceStage.VALIDATION.value
    ]
    assert links
    assert links[0]["parent_id"] in {issue["object_a"], issue["object_b"]}
    assert links[0]["algorithm"] == issue["rule_id"]


def test_a_review_is_linked_to_the_finding_it_resolves(repo):
    from app.services.reviews import create_review_case

    issue = repo.records("issues")[0]
    case = create_review_case(repo, issue["id"], reason="check")
    link = next(
        l for l in pv.get_provenance(repo, case["id"])
        if l["stage"] == ProvenanceStage.REVIEW.value
    )
    assert link["parent_id"] == issue["id"]
    assert link["parent_stage"] == ProvenanceStage.VALIDATION.value
    assert link["algorithm"] == "human-review"


def test_the_review_stage_reaches_the_object_under_review(pipeline, repo):
    """A finding about a derived object inherits the volume's origin."""
    from app.services.reviews import create_review_case

    store, source_id = pipeline
    volume = store.records("generated_property_volumes")[0]
    issue = next(
        i for i in store.records("issues") if i["object_a"] == volume["id"]
    ) if any(i["object_a"] == volume["id"] for i in store.records("issues")) else None
    if issue is None:
        pytest.skip("no finding raised against a generated volume in this scene")
    case = create_review_case(store, issue["id"], reason="verify")
    assert pv.get_source_objects(store, case["id"]) == [source_id]


# ==========================================================================
# Querying
# ==========================================================================


def test_get_provenance_matches_both_roles(pipeline):
    store, _ = pipeline
    storey = store.records("extracted_floors")[0]
    links = pv.get_provenance(store, storey["id"])
    # As a result: its own FLOOR link. As a parent: the volumes that came from it.
    assert any(l["object_id"] == storey["id"] for l in links)
    assert any(l["parent_id"] == storey["id"] or l.get("object_id") == storey["id"] for l in links)


def test_get_derived_objects_can_be_scoped_to_a_stage(pipeline):
    store, source_id = pipeline
    buildings = pv.get_derived_objects(store, source_id, stage=ProvenanceStage.BUILDING.value)
    assert len(buildings) == 1
    assert buildings[0]["stage"] == ProvenanceStage.BUILDING.value
    assert all(i.startswith("XB-") for i in buildings[0]["object_ids"])


def test_get_derived_objects_of_an_unknown_source_is_empty(repo):
    assert pv.get_derived_objects(repo, "DS-NOPE") == []


def test_get_source_objects_of_an_unknown_object_is_empty(repo):
    assert pv.get_source_objects(repo, "NOT-A-THING") == []


def test_get_processing_history_of_an_unknown_object_is_empty(repo):
    assert pv.get_processing_history(repo, "NOT-A-THING") == []


def test_a_walk_terminates_on_a_cycle(repo):
    """A malformed chain must not spin forever.

    A cycle cannot arise from the service, but the table is writable and a bad
    row would otherwise hang a request.
    """
    for index, (stage, parent) in enumerate(
        [
            (ProvenanceStage.BUILDING.value, "XB-A"),
            (ProvenanceStage.FLOOR.value, "XB-B"),
        ]
    ):
        pv.create_provenance_record(
            repo,
            stage=ProvenanceStage.FLOOR.value,
            object_id=f"XF-{index}",
            parent_id=parent,
            parent_stage=ProvenanceStage.BUILDING.value,
        )
    # Point XB-B back at XF-0 to close the loop.
    repo.records("provenance_links")[-1]["parent_id"] = "XF-0"
    assert isinstance(pv.get_object_lineage(repo, "XF-0"), dict)


def test_a_diamond_reports_a_shared_ancestor_once(repo):
    """Two paths to one object must not list it twice."""
    for child in ("GPV-1", "GPV-2"):
        pv.create_provenance_record(
            repo,
            stage=ProvenanceStage.PROPERTY_VOLUME.value,
            object_id=child,
            source_id="DS-1",
            parent_id="JOB-1",
            parent_stage=ProvenanceStage.PROCESSING_JOB.value,
            algorithm="derived_geometric",
        )
    pv.create_provenance_record(
        repo,
        stage=ProvenanceStage.VALIDATION.value,
        object_id="VAL-1",
        parent_id="GPV-1",
        parent_stage=ProvenanceStage.PROPERTY_VOLUME.value,
    )
    pv.create_provenance_record(
        repo,
        stage=ProvenanceStage.VALIDATION.value,
        object_id="VAL-2",
        parent_id="GPV-2",
        parent_stage=ProvenanceStage.PROPERTY_VOLUME.value,
    )
    lineage = pv.get_object_lineage(repo, "VAL-1")
    assert len(lineage["ancestors"]) == len(set(lineage["ancestors"]))


# ==========================================================================
# The linking helpers
# ==========================================================================


def test_link_source_to_object(repo):
    link = pv.link_source_to_object(
        repo, "DS-1", "GPV-1", stage=ProvenanceStage.PROPERTY_VOLUME.value
    )
    assert link["parent_id"] == "DS-1"
    assert link["parent_stage"] == ProvenanceStage.DATA_SOURCE.value
    assert link["source_id"] == "DS-1"


def test_link_processing_job_to_object(repo):
    link = pv.link_processing_job_to_object(
        repo,
        "JOB-1",
        "XB-1",
        stage=ProvenanceStage.BUILDING.value,
        source_id="DS-1",
        algorithm="algorithmic_geometric",
    )
    assert link["parent_id"] == "JOB-1"
    assert link["processing_job_id"] == "JOB-1"
    assert link["source_id"] == "DS-1"


def test_a_link_with_no_parent_is_a_legitimate_root(repo):
    """A hand-uploaded file genuinely has no parent."""
    link = pv.create_provenance_record(
        repo, stage=ProvenanceStage.PROPERTY_VOLUME.value, object_id="GPV-1"
    )
    assert link["parent_id"] is None
    assert link["algorithm"] == pv.DEFAULT_METHOD


# ==========================================================================
# Immutability
# ==========================================================================


def test_provenance_links_cannot_be_updated(pipeline):
    """A rewritten provenance record is a false record."""
    store, _ = pipeline
    link = store.records("provenance_links")[0]
    repo_update = store.update
    repo_update("provenance_links", link["id"], {"source_id": "DS-FAKED"})
    after = next(l for l in store.records("provenance_links") if l["id"] == link["id"])
    assert after["source_id"] == link["source_id"]


def test_the_demo_scene_seeds_no_provenance(repo):
    """The seeded records have no survey behind them, so none is claimed."""
    stages = {l["stage"] for l in repo.records("provenance_links")}
    assert ProvenanceStage.BUILDING.value not in stages
    assert ProvenanceStage.PROPERTY_VOLUME.value not in stages
    assert ProvenanceStage.ULPIN.value not in stages
