"""The full pipeline, exercised in one test, on both storage backends.

The audit found that every stage passed its own tests and the *sequence* still
broke: re-processing one source produced duplicate derived records, collided on a
storey primary key (a 500 under PostGIS, silent duplication in memory), and
re-issued the ULPIN of an unchanged volume. None of that is visible from a
per-stage test, which is exactly why it survived.

This module drives the stages the way a user does -- upload, extract, segment,
generate -- and asserts the properties that only hold across the whole run.
"""
from __future__ import annotations

import re

import pytest

from tests.fixtures import buildings as fx


@pytest.fixture
def store(tmp_path, monkeypatch):
    from app.services import point_cloud_store as pc_store

    root = tmp_path / "store"
    root.mkdir()
    monkeypatch.setattr(pc_store, "DEFAULT_STORE_DIR", root)
    return root


@pytest.fixture
def upload(tmp_path_factory):
    def _read(spec, *, name: str = "scan.las") -> bytes:
        directory = tmp_path_factory.mktemp("pipeline")
        path = fx.write_las_storey_scene(directory / name, spec)
        return path.read_bytes()

    return _read


def _ingest(client, payload: bytes, name: str = "scan.las") -> str:
    r = client.post(
        "/import/source",
        files={"file": (name, payload, "application/octet-stream")},
    )
    assert r.status_code == 200, r.text
    return r.json()["source_id"]


def _run_pipeline(client, source_id: str) -> tuple[int, int, int]:
    """Extract, segment, generate. Returns each stage's HTTP status."""
    stages = []
    for path in ("extract", "segment-floors", "property-volumes"):
        r = client.post(f"/point-clouds/{source_id}/{path}")
        stages.append(r.status_code)
        assert r.status_code == 200, f"{path} -> {r.status_code}: {r.text[:400]}"
    return tuple(stages)  # type: ignore[return-value]


# ==========================================================================
# the pipeline runs
# ==========================================================================


def test_the_whole_pipeline_runs_from_upload_to_volumes(client, store, upload):
    source_id = _ingest(client, upload(fx.three_storey_building()))
    assert _run_pipeline(client, source_id) == (200, 200, 200)

    buildings = client.get(f"/extracted-buildings?source_id={source_id}").json()
    storeys = client.get(f"/extracted-floors?source_id={source_id}").json()
    volumes = client.get(f"/generated-property-volumes?source_id={source_id}").json()
    assert len(buildings) >= 1
    assert len(storeys) >= 1
    assert len(volumes) >= 1
    # Every volume is identified, and the identifier is the prototype format.
    for volume in volumes:
        assert volume["prototype_ulpin"].startswith("VC-VP-")


def test_the_pipeline_reaches_validation_review_and_change_detection(
    client, store, upload
):
    """The tail of the pipeline, driven from a pipeline-produced volume.

    Change detection reads the *cadastral* scene rather than the derived volumes,
    so the comparison is against the seeded parcel and its 17 volumes -- which is
    the intended relationship: derived geometry is compared with the approved
    cadastre, never written into it.
    """
    source_id = _ingest(client, upload(fx.three_storey_building()))
    _run_pipeline(client, source_id)

    validated = client.post("/validation/run").json()
    assert validated["issues"] >= 1, "the pipeline produced no findings to review"
    issues = client.get("/validation/issues").json()
    assert len(issues) == validated["issues"]

    case = client.post(f"/reviews?issue_id={issues[0]['id']}").json()
    assert case["state"] == "PENDING"
    approved = client.post(
        f"/reviews/{case['id']}/approve",
        json={"reason": "pipeline audit", "reviewer": "audit-test"},
    ).json()
    assert approved["state"] == "APPROVED"

    properties = client.get("/properties").json()
    target = next(p for p in properties if p["geometry_3d"])
    ring = target["geometry_3d"]["coordinates"][0]
    moved = [
        [lon + 0.00002, lat + 0.00002] if i in (1, 2) else [lon, lat]
        for i, (lon, lat) in enumerate(ring)
    ]
    compared = client.post(
        "/changes/compare",
        json={
            "source_id": "DS-PIPELINE",
            "survey": [
                {
                    "id": target["id"],
                    "geometry_3d": {"type": "Polygon", "coordinates": [moved]},
                    "z_min": target["z_min"],
                    "z_max": target["z_max"],
                    "floor_number": target["floor_number"],
                    "building_id": target["building_id"],
                }
            ],
        },
    )
    assert compared.status_code == 200, compared.text
    assert compared.json()["total_changes"] >= 1

    changes = client.get("/changes?source_id=DS-PIPELINE").json()
    assert changes
    footprint = next((c for c in changes if c["change_type"] == "FOOTPRINT"), None)
    assert footprint is not None, "a moved boundary must be reported as a change"
    assert footprint["difference_geometry"], (
        "the changed region is what a reviewer highlights, so it must be stored"
    )
    # And every change is reviewable: a change with no finding cannot be verified.
    assert all(c["finding_id"] for c in changes)


# ==========================================================================
# the properties that only hold across the whole run
# ==========================================================================


def test_running_the_pipeline_twice_does_not_crash(client, store, upload):
    """The 500 this audit found.

    The storey id used the building's last dash-separated segment as its
    discriminator, which is ``001`` for the first building of every extraction
    job. The second segmentation pass therefore produced a storey id that already
    existed, and PostGIS raised ``UniqueViolation`` -- an unhandled 500 -- while
    the in-memory store quietly wrote a duplicate row.
    """
    source_id = _ingest(client, upload(fx.three_storey_building()))
    assert _run_pipeline(client, source_id) == (200, 200, 200)
    assert _run_pipeline(client, source_id) == (200, 200, 200)
    assert _run_pipeline(client, source_id) == (200, 200, 200)


def test_no_derived_record_ever_shares_a_primary_key(client, store, upload):
    """The silent half of the same bug: two rows, one id, no error in memory."""
    source_id = _ingest(client, upload(fx.three_storey_building()))
    for _ in range(3):
        _run_pipeline(client, source_id)

    for path in (
        "/extracted-buildings",
        "/extracted-floors",
        "/generated-property-volumes",
        "/processing-jobs",
    ):
        records = client.get(f"{path}?source_id={source_id}"
                             if "jobs" not in path else path).json()
        if not isinstance(records, list):
            continue
        ids = [r["id"] for r in records]
        duplicates = {i for i in ids if ids.count(i) > 1}
        assert not duplicates, f"{path} holds duplicate primary keys: {duplicates}"


def test_reprocessing_a_source_does_not_reissue_identifiers(client, store, upload):
    """The project's central identity claim, which was false for derived volumes.

    ``volume_key`` embedded the job-scoped ``building_id``, so the same physical
    volume was ``VC-VP-P001-XBJOBA005...`` on one run and
    ``VC-VP-P001-XBJOB51FE0F...`` on the next, and the upsert accumulated a
    duplicate volume for each. A cadastral identifier that changes when nothing
    physical changed is not an identifier.
    """
    source_id = _ingest(client, upload(fx.three_storey_building()))

    def snapshot() -> tuple[set[str], int]:
        volumes = client.get(
            f"/generated-property-volumes?source_id={source_id}"
        ).json()
        return {v["prototype_ulpin"] for v in volumes}, len(volumes)

    _run_pipeline(client, source_id)
    first_ids, first_count = snapshot()
    assert first_ids, "the run produced no volumes to compare"

    for _ in range(2):
        _run_pipeline(client, source_id)

    later_ids, later_count = snapshot()
    assert first_ids == later_ids, (
        f"re-processing one source re-issued identifiers:\n"
        f"  first: {sorted(first_ids)}\n"
        f"  later: {sorted(later_ids)}"
    )
    assert first_count == later_count, (
        f"re-processing one source accumulated volumes: "
        f"{first_count} -> {later_count}"
    )


def test_the_identifier_carries_no_processing_job_id(client, store, upload):
    """A job id in an identifier means the identifier changes per run.

    Stated as a property of the value rather than of one call, so a future
    derivation that reintroduces a job id fails here immediately.
    """
    source_id = _ingest(client, upload(fx.three_storey_building()))
    _run_pipeline(client, source_id)
    volumes = client.get(f"/generated-property-volumes?source_id={source_id}").json()
    assert volumes
    for volume in volumes:
        ulpin = volume["prototype_ulpin"]
        assert "JOB" not in ulpin, f"{ulpin} embeds a processing job id"
        # The identifier slugifies its components, so ``DS-002`` appears as
        # ``DS002``. Comparing against the raw id would fail for a reason that
        # has nothing to do with identity stability.
        slug = re.sub(r"[^A-Z0-9]", "", source_id.upper())
        assert slug in ulpin, (
            f"{ulpin} does not name its source ({slug}), so two sources could collide"
        )


def test_an_approval_survives_a_re_run_of_the_pipeline(client, store, upload):
    """A human decision is evidence and must not be undone by reprocessing."""
    validated = client.post("/validation/run").json()
    issues = client.get("/validation/issues").json()
    case = client.post(f"/reviews?issue_id={issues[0]['id']}").json()
    client.post(
        f"/reviews/{case['id']}/approve",
        json={"reason": "pipeline audit", "reviewer": "audit-test"},
    )

    source_id = _ingest(client, upload(fx.three_storey_building()))
    _run_pipeline(client, source_id)
    client.post("/validation/run")

    # Read the case directly. ``GET /reviews`` is the *pending* queue and
    # deliberately excludes decided cases, so an approved case is correctly
    # absent from it -- reading that absence as a lost approval would report a
    # serious bug that does not exist.
    survived = client.get(f"/reviews/{issues[0]['id']}")
    assert survived.status_code == 200, survived.text
    assert survived.json()["state"] == "APPROVED", (
        f"re-running the pipeline discarded the approval: {survived.json()}"
    )
    # And the decision row itself is still readable.
    decisions = client.get(f"/reviews/{survived.json()['id']}/decisions").json()
    assert [d["decision"] for d in decisions] == ["APPROVED"], decisions


def test_the_cadastre_is_never_written_by_the_pipeline(client, store, upload):
    """Derived geometry must not touch the cadastral tables.

    The pipeline writes ``extracted_*`` and ``generated_property_volumes``, which
    are deliberately separate tables. A generated volume landing in
    ``property_volumes`` would claim legal standing it does not have.
    """
    before = {
        name: client.get(path).json()
        for name, path in (
            ("parcels", "/parcels"),
            ("buildings", "/buildings"),
            ("properties", "/properties"),
        )
    }
    source_id = _ingest(client, upload(fx.three_storey_building()))
    _run_pipeline(client, source_id)
    _run_pipeline(client, source_id)

    for name, path in (("parcels", "/parcels"), ("buildings", "/buildings"), ("properties", "/properties")):
        after = client.get(path).json()
        assert len(after) == len(before[name]), (
            f"{name} changed count: {len(before[name])} -> {len(after)}"
        )
        assert after == before[name], f"{name} was modified by the pipeline"


def test_provenance_reaches_back_to_the_source_after_the_pipeline(
    client, store, upload
):
    """Audit item 14, end to end: every derived object traces to a data source."""
    source_id = _ingest(client, upload(fx.three_storey_building()))
    _run_pipeline(client, source_id)

    stages = client.get("/provenance/stages").json()["stages"]
    for path in ("/extracted-buildings", "/extracted-floors", "/generated-property-volumes"):
        records = client.get(path).json()
        assert records, f"{path} is empty"
        for record in records[:2]:
            lineage = client.get(
                f"/provenance/{record['id']}/lineage"
            ).json()
            assert lineage["object_id"] == record["id"]
            ranks = [
                stages.index(node["stage"])
                for node in lineage["lineage"]
                if node["stage"] in stages
            ]
            assert ranks == sorted(ranks), "the chain is not in stage order"
            # And no stage names a model, because none is used.
            assert all(
                not node.get("model_name") for node in lineage["lineage"]
            )
