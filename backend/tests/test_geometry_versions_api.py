"""HTTP surface for geometry version history.

The service layer is already covered by ``test_identity.py``; this module covers
the part that did not exist: the routes.

The centre of gravity is the identity invariant, because it is the one property
the whole subsystem exists to guarantee and the one a reviewer would most want
to check over HTTP:

    stable ULPIN  ->  version 1  ->  version 2  ->  version N

A geometry change must move the geometry and the hash and the version number,
and must not touch the identifier.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.repositories.memory import InMemoryRepository
from app.repositories import set_repository
from app.services.demo import seed_demo
from app.services.geometry import create_rectangle, polygon_to_geojson
from app.services.geometry_versioning import create_geometry_version

PARCEL = "parcel-001"
PROPERTY = "PV-201"


@pytest.fixture
def client():
    set_repository(InMemoryRepository())
    with TestClient(app) as test_client:
        yield test_client
    set_repository(InMemoryRepository())


def _shrink(parcel_id: str = PARCEL, metres: float = 1.0) -> dict:
    """A different but valid polygon for the demo parcel."""
    return polygon_to_geojson(create_rectangle(0.0, 0.0, 80.0 - metres, 52.0))


# ==========================================================================
# 1. current version
# ==========================================================================


def test_current_version_reports_the_stored_row_and_its_identifier(client):
    body = client.get(f"/geometry-versions/{PARCEL}").json()
    assert body["object_id"] == PARCEL
    assert body["object_type"] == "PARCEL"
    assert body["prototype_ulpin"] == "VC-LP-P001"
    assert body["version"] == 1
    assert body["geometry_hash"]
    assert body["geometry"]["type"] == "Polygon"
    assert body["geometry"]["coordinates"]
    # A parcel is 2D land, so a zero Z extent is correct, not degenerate.
    assert body["z_min"] == 0.0 and body["z_max"] == 0.0
    assert body["change_reason"]
    assert body["created_at"]


def test_current_version_carries_z_range_for_a_volumetric_object(client):
    body = client.get(f"/geometry-versions/{PROPERTY}").json()
    assert body["object_type"] == "PROPERTY_VOLUME"
    assert body["prototype_ulpin"] == "VC-VP-P001-APARTMENT201"
    assert body["z_min"] == 3.2 and body["z_max"] == 6.4


def test_current_version_reports_no_identifier_as_none_not_invented(client):
    """Buildings carry no ULPIN, and absence must read as absence."""
    body = client.get("/geometry-versions/building-001").json()
    assert body["object_type"] == "BUILDING"
    assert body["prototype_ulpin"] is None


def test_current_version_resolves_a_business_id_too(client):
    """``/parcels/{ident}`` accepts business ids, so this must as well."""
    by_row = client.get(f"/geometry-versions/{PARCEL}").json()
    by_business = client.get("/geometry-versions/P-001").json()
    assert by_row == by_business


def test_current_version_resolves_a_prototype_ulpin(client):
    body = client.get("/geometry-versions/VC-LP-P001").json()
    assert body["object_id"] == PARCEL


# ==========================================================================
# 2. history
# ==========================================================================


def test_history_is_ordered_oldest_first_and_ends_at_the_current_version(client):
    for _ in range(3):
        create_geometry_version(
            _repo(), PARCEL, geometry=_shrink(), change_reason="re-survey"
        )
    history = client.get(f"/geometry-versions/{PARCEL}/history").json()
    assert [v["version"] for v in history] == [1, 2, 3, 4]
    current = client.get(f"/geometry-versions/{PARCEL}").json()
    assert history[-1]["version"] == current["version"]
    assert history[-1]["geometry_hash"] == current["geometry_hash"]


def test_history_exposes_stored_metadata_and_invents_nothing(client):
    version = client.get(f"/geometry-versions/{PARCEL}/history").json()[0]
    # Exactly the stored columns. In particular no identifier: a version row has
    # none by design, and back-filling one would imply it was ever recorded.
    assert set(version) == {
        "object_id",
        "object_type",
        "version",
        "geometry",
        "z_min",
        "z_max",
        "geometry_hash",
        "source_id",
        "processing_job_id",
        "created_at",
        "created_by",
        "change_reason",
    }
    assert "prototype_ulpin" not in version


def test_history_records_provenance_when_supplied(client):
    create_geometry_version(
        _repo(),
        PARCEL,
        geometry=_shrink(),
        source_id="DS-RS-1",
        processing_job_id="JOB-RS-1",
        created_by="surveyor-1",
        change_reason="re-survey",
    )
    latest = client.get(f"/geometry-versions/{PARCEL}/history").json()[-1]
    assert latest["source_id"] == "DS-RS-1"
    assert latest["processing_job_id"] == "JOB-RS-1"
    assert latest["created_by"] == "surveyor-1"
    assert latest["change_reason"] == "re-survey"


def test_history_is_deterministic_across_repeated_reads(client):
    first = client.get(f"/geometry-versions/{PARCEL}/history").json()
    for _ in range(3):
        again = client.get(f"/geometry-versions/{PARCEL}/history").json()
        assert again == first


def test_history_is_empty_for_an_object_with_no_versions(client):
    """An object that exists but was never versioned is an empty list, not 404.

    The object is real; the absence is about its history, and conflating the two
    would make "never surveyed" indistinguishable from "no such object".
    """
    repo = _repo()
    repo.add(
        "parcels",
        {
            "id": "parcel-unversioned",
            "parcel_id": "P-999",
            "prototype_ulpin": "VC-LP-P999",
            "geometry": polygon_to_geojson(create_rectangle(0, 0, 10, 10)),
            "area": 100.0,
            "land_use": "Test",
            "survey_reference": "Test",
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "version": 1,
        },
    )
    assert client.get("/geometry-versions/parcel-unversioned/history").json() == []
    # ... but asking for its *current* version is a 404, because there is none.
    r = client.get("/geometry-versions/parcel-unversioned")
    assert r.status_code == 404
    assert "no geometry version" in r.json()["detail"].lower()


# ==========================================================================
# 3. a specific version
# ==========================================================================


def test_a_specific_version_returns_the_geometry_as_it_was(client):
    original = client.get(f"/geometry-versions/{PARCEL}/versions/1").json()
    original_hash = original["geometry_hash"]

    create_geometry_version(_repo(), PARCEL, geometry=_shrink(), change_reason="re-survey")

    # Version 1 must still be the geometry from before the change, not today's.
    replayed = client.get(f"/geometry-versions/{PARCEL}/versions/1").json()
    assert replayed["geometry_hash"] == original_hash
    assert replayed["geometry"] == original["geometry"]
    assert client.get(f"/geometry-versions/{PARCEL}").json()["geometry_hash"] != original_hash


def test_a_specific_version_matches_the_history_entry(client):
    history = client.get(f"/geometry-versions/{PARCEL}/history").json()
    for entry in history:
        fetched = client.get(
            f"/geometry-versions/{PARCEL}/versions/{entry['version']}"
        ).json()
        assert fetched == entry


# ==========================================================================
# 4. comparison
# ==========================================================================


def test_comparison_reports_the_moved_geometry_and_nothing_more(client):
    create_geometry_version(_repo(), PARCEL, geometry=_shrink(), change_reason="re-survey")
    body = client.get(
        f"/geometry-versions/{PARCEL}/compare?from_version=1&to_version=2"
    ).json()

    assert body["object_id"] == PARCEL
    assert body["from_version"] == 1
    assert body["to_version"] == 2
    assert body["geometry_changed"] is True
    assert body["geometry_hash_changed"] is True
    assert body["from_geometry_hash"] != body["to_geometry_hash"]
    # The parcel shrank, so the area delta is negative and non-zero.
    assert body["delta_area_m2"] < 0
    assert body["to_area_m2"] == body["from_area_m2"] + body["delta_area_m2"]
    # A parcel has no vertical extent to move.
    assert body["z_range_changed"] is False
    assert body["delta_z_min"] == 0.0 and body["delta_z_max"] == 0.0


def test_comparison_reports_z_deltas_for_a_volumetric_object(client):
    create_geometry_version(
        _repo(), PROPERTY, z_min=3.2, z_max=7.4, change_reason="re-survey"
    )
    body = client.get(
        f"/geometry-versions/{PROPERTY}/compare?from_version=1&to_version=2"
    ).json()
    assert body["z_range_changed"] is True
    assert body["delta_z_min"] == 0.0
    assert body["delta_z_max"] == pytest.approx(1.0, abs=1e-6)
    assert body["delta_volume_m3"] > 0
    assert body["to_volume_m3"] == body["from_volume_m3"] + body["delta_volume_m3"]


def test_comparison_of_a_version_with_itself_reports_no_change(client):
    body = client.get(
        f"/geometry-versions/{PARCEL}/compare?from_version=1&to_version=1"
    ).json()
    assert body["geometry_changed"] is False
    assert body["delta_area_m2"] == 0.0
    assert body["delta_volume_m3"] == 0.0


def test_comparison_exposes_the_identifier_that_did_not_change(client):
    create_geometry_version(_repo(), PARCEL, geometry=_shrink())
    body = client.get(
        f"/geometry-versions/{PARCEL}/compare?from_version=1&to_version=2"
    ).json()
    assert body["prototype_ulpin"] == "VC-LP-P001"
    assert body["ulpin_changed"] is False


# ==========================================================================
# 5-7. failure modes
# ==========================================================================


def test_a_nonexistent_object_is_a_404(client):
    r = client.get("/geometry-versions/parcel-does-not-exist")
    assert r.status_code == 404
    assert "no cadastral object" in r.json()["detail"].lower()
    assert "internal server" not in r.text.lower()


def test_a_nonexistent_version_is_a_404_naming_what_exists(client):
    r = client.get(f"/geometry-versions/{PARCEL}/versions/99")
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert "no geometry version 99" in detail.lower()
    assert "1" in detail, "the error must name the versions that do exist"


def test_an_invalid_comparison_is_a_404_not_a_500(client):
    r = client.get(
        f"/geometry-versions/{PARCEL}/compare?from_version=1&to_version=42"
    )
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")


def test_a_non_integer_version_is_a_422(client):
    r = client.get(f"/geometry-versions/{PARCEL}/versions/not-a-number")
    assert r.status_code == 422


def test_missing_comparison_parameters_are_a_422(client):
    assert client.get(f"/geometry-versions/{PARCEL}/compare").status_code == 422
    assert (
        client.get(f"/geometry-versions/{PARCEL}/compare?from_version=1").status_code
        == 422
    )


def test_a_non_cadastral_id_is_a_404_not_a_wrong_answer(client):
    """A data source is not a cadastral object and must not resolve as one."""
    r = client.get("/geometry-versions/DS-001")
    assert r.status_code == 404


# ==========================================================================
# 8-10. the identity invariant
# ==========================================================================


def test_ulpin_survives_a_geometry_change_over_http(client):
    """The invariant, end to end through HTTP.

    Before: version 1, identifier U, hash H1.
    After a geometry change: version 2, the same identifier U, a different hash.
    """
    before = client.get(f"/geometry-versions/{PARCEL}").json()
    u_before, h_before, v_before = (
        before["prototype_ulpin"],
        before["geometry_hash"],
        before["version"],
    )
    assert u_before and h_before and v_before == 1

    create_geometry_version(
        _repo(), PARCEL, geometry=_shrink(), change_reason="re-survey"
    )

    after = client.get(f"/geometry-versions/{PARCEL}").json()
    assert after["prototype_ulpin"] == u_before, "a geometry change reissued the ULPIN"
    assert after["geometry_hash"] != h_before, "the hash did not change"
    assert after["version"] == v_before + 1, "the version did not increment"

    # And the record itself, read through the ordinary cadastral route, still
    # carries the same identifier.
    assert client.get("/parcels/P-001").json()["prototype_ulpin"] == u_before


def test_the_ulpin_is_the_same_on_every_version_of_a_long_history(client):
    repo = _repo()
    create_geometry_version(repo, PARCEL, geometry=_shrink(metres=1.0))
    create_geometry_version(repo, PARCEL, geometry=_shrink(metres=2.0))
    create_geometry_version(repo, PARCEL, geometry=_shrink(metres=3.0))

    history = client.get(f"/geometry-versions/{PARCEL}/history").json()
    assert [v["version"] for v in history] == [1, 2, 3, 4]
    # Every version has a distinct hash...
    hashes = [v["geometry_hash"] for v in history]
    assert len(set(hashes)) == len(hashes)
    # ...and one identifier, which none of them carries.
    assert client.get(f"/geometry-versions/{PARCEL}").json()["prototype_ulpin"] == (
        "VC-LP-P001"
    )
    for a, b in ((1, 2), (2, 3), (1, 4)):
        body = client.get(
            f"/geometry-versions/{PARCEL}/compare?from_version={a}&to_version={b}"
        ).json()
        assert body["ulpin_changed"] is False
        assert body["prototype_ulpin"] == "VC-LP-P001"


def test_repeated_geometry_processing_does_not_reissue_an_identifier(client):
    """Re-running a stage must not mint a new ULPIN.

    Every ``create_geometry_version`` call is a fresh write, including one that
    records the *same* geometry. Each may add a version; none may touch identity.
    """
    repo = _repo()
    before = client.get(f"/geometry-versions/{PROPERTY}").json()["prototype_ulpin"]
    record = repo.records("properties")[0]
    next((r for r in repo.records("properties") if r["id"] == PROPERTY), record)

    for _ in range(5):
        create_geometry_version(
            repo, PROPERTY, source_id="DS-REPEAT", change_reason="reprocess"
        )
        assert (
            client.get(f"/geometry-versions/{PROPERTY}").json()["prototype_ulpin"]
            == before
        )
        # The ordinary property route agrees.
        properties = {p["id"]: p["prototype_ulpin"] for p in client.get("/properties").json()}
        assert properties[PROPERTY] == before

    # Versions did advance; identifiers did not.
    history = client.get(f"/geometry-versions/{PROPERTY}/history").json()
    assert [v["version"] for v in history] == [1, 2, 3, 4, 5, 6]
    assert all(v["prototype_ulpin"] if "prototype_ulpin" in v else None is None
               for v in history)


def test_a_z_only_revision_keeps_the_identifier_but_moves_the_hash(client):
    create_geometry_version(repo=_repo(), object_id=PROPERTY, z_min=3.4, z_max=6.4)
    before = client.get(f"/geometry-versions/{PROPERTY}").json()
    assert before["version"] == 2
    assert before["prototype_ulpin"] == "VC-VP-P001-APARTMENT201"
    body = client.get(
        f"/geometry-versions/{PROPERTY}/compare?from_version=1&to_version=2"
    ).json()
    assert body["geometry_hash_changed"] is True
    assert body["z_range_changed"] is True
    assert body["ulpin_changed"] is False


# ==========================================================================
# 11. discovery and read-only guarantees
# ==========================================================================


def test_discovery_names_the_versioned_collections(client):
    body = client.get("/geometry-versions").json()
    assert set(body["versioned_collections"]) == {
        "parcels",
        "buildings",
        "properties",
        "infrastructure",
    }
    assert body["object_types"]["parcels"] == "PARCEL"
    assert body["total_objects"] == 1 + 1 + 17 + 1


def test_there_is_no_route_that_writes_a_geometry_version(client):
    """Read-only is the design, not an accident.

    Applying a geometry change means deciding who may move a cadastral boundary.
    That is a policy question this layer must not answer, so no POST, PUT, PATCH
    or DELETE exists against the geometry-version surface.
    """
    spec = client.get("/openapi.json").json()
    for path, ops in spec["paths"].items():
        if "geometry-version" not in path:
            continue
        assert set(ops) <= {"get"}, f"{path} exposes a write method: {set(ops)}"


def test_versions_are_not_rewritten_by_a_read(client):
    history_before = client.get(f"/geometry-versions/{PARCEL}/history").json()
    for _ in range(3):
        client.get(f"/geometry-versions/{PARCEL}")
        client.get(f"/geometry-versions/{PARCEL}/history")
    assert client.get(f"/geometry-versions/{PARCEL}/history").json() == history_before


# ==========================================================================
# helper
# ==========================================================================


def _repo() -> InMemoryRepository:
    """The repository the running app is bound to, for direct service writes.

    The routes are read-only, so a test that needs history of more than one
    version writes through the service -- the same path the pipeline uses.
    """
    from app.repositories import get_repository

    repo = get_repository()
    if not repo.records("geometry_versions"):
        seed_demo(repo)
    return repo
