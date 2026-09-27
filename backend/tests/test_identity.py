"""Stable cadastral identity and geometry versioning.

The invariant under test: **a geometry change must never change identity.**
Each of the five required properties gets an explicit test:

1. same property -> same ULPIN
2. geometry changes -> same ULPIN
3. geometry changes -> new geometry hash
4. geometry changes -> incremented geometry version
5. geometry history remains accessible
"""
from __future__ import annotations

import pytest

from app.repositories.memory import InMemoryRepository
from app.services.demo import seed_demo
from app.services.geometry import polygon_to_geojson, xy_to_ll
from app.services.geometry_versioning import (
    UnknownObject,
    compare_geometry_versions,
    create_geometry_version,
    get_current_geometry_version,
    get_geometry_history,
    get_previous_geometry_version,
    snapshot_geometry,
)
from app.services.ulpin import (
    assign_ulpins,
    generate_stable_ulpin,
    ulpin_for_record,
)


def geo(ring):
    """Local-plane ring -> GeoJSON mapping."""
    closed = list(ring) + [ring[0]]
    return {"type": "Polygon", "coordinates": [[xy_to_ll(x, y) for x, y in closed]]}


@pytest.fixture
def repo():
    r = InMemoryRepository()
    seed_demo(r)
    return r


def prop(repo, object_id):
    return next(p for p in repo.records("properties") if p["id"] == object_id)


# --------------------------------------------------------------------------
# generate_stable_ulpin
# --------------------------------------------------------------------------


def test_generate_stable_ulpin_format():
    assert generate_stable_ulpin(prefix="VP", parent="P-001", unit="Apartment 201") == (
        "VC-VP-P001-APARTMENT201"
    )


def test_generate_stable_ulpin_ignores_punctuation():
    assert generate_stable_ulpin(
        prefix="LP", parent="p-001", unit="p-001"
    ) == generate_stable_ulpin(prefix="LP", parent="P001", unit="P001")


def test_generate_stable_ulpin_falls_back_when_unit_missing():
    assert generate_stable_ulpin(prefix="VP", parent="ROOT", unit="").endswith("LAND")


def test_ulpin_contains_no_geometry_derived_component():
    """The format must not embed a hash or a centroid-derived grid index."""
    ulpin = generate_stable_ulpin(prefix="VP", parent="P-001", unit="Apartment 201")
    # No 6-char hex digest segment, and no 10-digit spatial grid segment.
    assert not any(
        len(part) == 6 and all(c in "0123456789ABCDEF" for c in part)
        for part in ulpin.split("-")
    )
    assert not any(part.isdigit() and len(part) >= 8 for part in ulpin.split("-"))


def test_ulpin_for_record_classifies_parcel_and_volume():
    parcel = {"id": "parcel-001", "parcel_id": "P-001"}
    volume = {"id": "PV-201", "parent_parcel_id": "P-001", "unit_label": "Apartment 201"}
    assert ulpin_for_record(parcel) == "VC-LP-P001"
    assert ulpin_for_record(volume) == "VC-VP-P001-APARTMENT201"

def test_building_is_not_mistaken_for_a_parcel():
    """Only land parcels and volumetric rights are issued identifiers.

    A building carries ``parcel_id`` but has no ``parent_parcel_id``, so it is
    never given an identifier — it is a structure on a parcel, not a parcel.
    """
    from app.repositories import get_repository

    r = get_repository()
    assign_ulpins(r, overwrite=True)
    assert all("prototype_ulpin" not in b for b in r.records("buildings"))
    assert all("prototype_ulpin" not in i for i in r.records("infrastructure"))
    assert all(p.get("prototype_ulpin") for p in r.records("properties"))
    assert all(p.get("prototype_ulpin") for p in r.records("parcels"))


# --------------------------------------------------------------------------
# PROOF 1: same property -> same ULPIN
# --------------------------------------------------------------------------


def test_proof_1_same_property_keeps_same_ulpin(repo):
    a = prop(repo, "PV-201")["prototype_ulpin"]
    b = prop(repo, "PV-201")["prototype_ulpin"]
    assert a == b


def test_proof_1_ulpin_is_stable_across_repeated_issuance(repo):
    before = prop(repo, "PV-201")["prototype_ulpin"]
    assign_ulpins(repo)
    assign_ulpins(repo, overwrite=True)  # even a deliberate re-issue
    assert prop(repo, "PV-201")["prototype_ulpin"] == before


def test_proof_1_ulpin_is_reproducible_from_a_fresh_seed():
    a, b = InMemoryRepository(), InMemoryRepository()
    seed_demo(a)
    seed_demo(b)
    assert prop(a, "PV-201")["prototype_ulpin"] == prop(b, "PV-201")["prototype_ulpin"]


def test_ulpins_are_unique_within_the_demo_scene(repo):
    seen = [p["prototype_ulpin"] for p in repo.records("properties")]
    assert len(seen) == len(set(seen))


# --------------------------------------------------------------------------
# geometry mutation helper
# --------------------------------------------------------------------------


def move_footprint(repo, object_id, ring, **kwargs):
    """Re-survey an object: new footprint, new version, same identity."""
    return create_geometry_version(
        repo,
        object_id,
        geometry=geo(ring),
        change_reason="re-survey",
        **kwargs,
    )


# --------------------------------------------------------------------------
# PROOF 2: geometry changes -> same ULPIN
# --------------------------------------------------------------------------


def test_proof_2_geometry_change_keeps_ulpin(repo):
    original_ulpin = prop(repo, "PV-201")["prototype_ulpin"]
    move_footprint(repo, "PV-201", [(12, 9), (38, 9), (38, 42), (12, 42)])
    assert prop(repo, "PV-201")["prototype_ulpin"] == original_ulpin


def test_proof_2_ulpin_survives_many_geometry_changes(repo):
    original_ulpin = prop(repo, "PV-201")["prototype_ulpin"]
    for i in range(5):
        move_footprint(repo, "PV-201", [(10 + i, 8), (40 - i, 8), (40 - i, 44), (10 + i, 44)])
    record = prop(repo, "PV-201")
    assert record["prototype_ulpin"] == original_ulpin
    assert record["geometry_version"] == 6


def test_proof_2_ulpin_survives_a_z_range_change(repo):
    original_ulpin = prop(repo, "PV-201")["prototype_ulpin"]
    create_geometry_version(repo, "PV-201", z_min=0.4, z_max=4.0, change_reason="levelling")
    assert prop(repo, "PV-201")["prototype_ulpin"] == original_ulpin


def test_proof_2_parcel_ulpin_survives_geometry_change(repo):
    parcel = repo.records("parcels")[0]
    original_ulpin = parcel["prototype_ulpin"]
    create_geometry_version(
        repo, parcel["id"], geometry=geo([(0, 0), (82, 0), (82, 52), (0, 52)])
    )
    assert parcel["prototype_ulpin"] == original_ulpin


def test_proof_2_infrastructure_ulpin_is_not_reissued_either(repo):
    infra = repo.records("infrastructure")[0]
    before = infra.get("prototype_ulpin")
    create_geometry_version(repo, infra["id"], z_max=-1.0, change_reason="depth survey")
    assert infra.get("prototype_ulpin") == before


# --------------------------------------------------------------------------
# PROOF 3: geometry changes -> new geometry hash
# --------------------------------------------------------------------------


def test_proof_3_geometry_change_produces_a_new_hash(repo):
    before = prop(repo, "PV-201")["geometry_hash"]
    move_footprint(repo, "PV-201", [(12, 9), (38, 9), (38, 42), (12, 42)])
    after = prop(repo, "PV-201")["geometry_hash"]
    assert after != before
    assert len(after) == 12 and after.isupper()


def test_proof_3_z_only_change_also_changes_the_hash(repo):
    before = prop(repo, "PV-201")["geometry_hash"]
    create_geometry_version(repo, "PV-201", z_max=9.9, change_reason="floor height fix")
    assert prop(repo, "PV-201")["geometry_hash"] != before


def test_proof_3_identical_geometry_keeps_the_same_hash(repo):
    """Re-recording the same shape must not churn the hash."""
    before = prop(repo, "PV-201")["geometry_hash"]
    original = prop(repo, "PV-201")["geometry_3d"]
    create_geometry_version(repo, "PV-201", geometry=original, change_reason="no-op resurvey")
    assert prop(repo, "PV-201")["geometry_hash"] == before


def test_proof_3_derived_area_and_volume_are_refreshed(repo):
    # Shrink the footprint from 30 x 36 = 1080 m^2 to 20 x 20 = 400 m^2.
    create_geometry_version(
        repo, "PV-201", geometry=geo([(10, 8), (30, 8), (30, 28), (10, 28)])
    )
    record = prop(repo, "PV-201")
    assert record["area_m2"] == pytest.approx(400, rel=1e-2)
    assert record["volume_m3"] == pytest.approx(400 * 3.2, rel=1e-2)


# --------------------------------------------------------------------------
# PROOF 4: geometry changes -> incremented geometry version
# --------------------------------------------------------------------------


def test_proof_4_version_increments_on_each_change(repo):
    assert prop(repo, "PV-201")["geometry_version"] == 1
    move_footprint(repo, "PV-201", [(12, 9), (38, 9), (38, 42), (12, 42)])
    assert prop(repo, "PV-201")["geometry_version"] == 2
    move_footprint(repo, "PV-201", [(13, 10), (37, 10), (37, 41), (13, 41)])
    assert prop(repo, "PV-201")["geometry_version"] == 3


def test_proof_4_record_version_is_independent_of_geometry_version(repo):
    """`version` is the record row version; `geometry_version` is the geometry."""
    record = prop(repo, "PV-201")
    assert record["version"] == 1
    move_footprint(repo, "PV-201", [(12, 9), (38, 9), (38, 42), (12, 42)])
    assert record["version"] == 1
    assert record["geometry_version"] == 2


def test_proof_4_versions_are_per_object(repo):
    move_footprint(repo, "PV-201", [(12, 9), (38, 9), (38, 42), (12, 42)])
    assert prop(repo, "PV-201")["geometry_version"] == 2
    assert prop(repo, "PV-101")["geometry_version"] == 1


def test_proof_4_parcel_area_is_refreshed_too(repo):
    parcel = repo.records("parcels")[0]
    create_geometry_version(
        repo, parcel["id"], geometry=geo([(0, 0), (100, 0), (100, 52), (0, 52)])
    )
    assert parcel["area"] == pytest.approx(5200, rel=1e-2)


def test_proof_4_invalid_z_range_is_rejected_without_versioning(repo):
    before_version = prop(repo, "PV-201")["geometry_version"]
    before_hash = prop(repo, "PV-201")["geometry_hash"]
    with pytest.raises(ValueError):
        create_geometry_version(repo, "PV-201", z_min=10, z_max=0)
    record = prop(repo, "PV-201")
    assert record["geometry_version"] == before_version
    assert record["geometry_hash"] == before_hash


# --------------------------------------------------------------------------
# PROOF 5: geometry history remains accessible
# --------------------------------------------------------------------------


def test_proof_5_history_has_one_entry_after_seed(repo):
    history = get_geometry_history(repo, "PV-201")
    assert len(history) == 1
    assert history[0].version == 1
    assert history[0].change_reason == "initial"
    assert history[0].source_id == "DS-001"


def test_proof_5_history_grows_with_each_change(repo):
    for i in range(3):
        move_footprint(repo, "PV-201", [(10 + i, 8), (40 - i, 8), (40 - i, 44), (10 + i, 44)])
    history = get_geometry_history(repo, "PV-201")
    assert [v.version for v in history] == [1, 2, 3, 4]
    assert all(v.object_id == "PV-201" for v in history)
    assert all(v.object_type == "PROPERTY_VOLUME" for v in history)


def test_proof_5_history_entries_are_immutable_snapshots(repo):
    """Later edits must not mutate the recorded past geometry."""
    original = prop(repo, "PV-201")["geometry_3d"]
    first = get_current_geometry_version(repo, "PV-201")
    move_footprint(repo, "PV-201", [(12, 9), (38, 9), (38, 42), (12, 42)])
    # The current version has advanced, but version 1 still holds the old shape.
    assert get_current_geometry_version(repo, "PV-201").version == 2
    stored = [v for v in get_geometry_history(repo, "PV-201") if v.version == 1][0]
    assert stored.geometry.model_dump() == original
    assert first.geometry.model_dump() == original


def test_proof_5_current_and_previous_accessors(repo):
    move_footprint(repo, "PV-201", [(12, 9), (38, 9), (38, 42), (12, 42)])
    current = get_current_geometry_version(repo, "PV-201")
    previous = get_previous_geometry_version(repo, "PV-201")
    assert current.version == 2
    assert previous.version == 1
    assert current.geometry_hash != previous.geometry_hash


def test_proof_5_history_is_empty_for_an_unknown_object(repo):
    assert get_geometry_history(repo, "NOPE-1") == []
    assert get_current_geometry_version(repo, "NOPE-1") is None


def test_proof_5_every_seeded_object_has_history(repo):
    for collection in ("parcels", "buildings", "properties", "infrastructure"):
        for record in repo.records(collection):
            history = get_geometry_history(repo, record["id"])
            assert len(history) == 1, record["id"]


def test_history_records_provenance_fields(repo):
    create_geometry_version(
        repo,
        "PV-201",
        geometry=geo([(12, 9), (38, 9), (38, 42), (12, 42)]),
        source_id="DS-002",
        processing_job_id="JOB-77",
        created_by="surveyor-1",
        change_reason="boundary re-survey",
    )
    latest = get_current_geometry_version(repo, "PV-201")
    assert latest.source_id == "DS-002"
    assert latest.processing_job_id == "JOB-77"
    assert latest.created_by == "surveyor-1"
    assert latest.change_reason == "boundary re-survey"
    assert latest.created_at


def test_snapshot_geometry_does_not_mutate_the_record(repo):
    before = prop(repo, "PV-201")["geometry_version"]
    snapshot_geometry(repo, "PV-201", version=99, change_reason="audit only")
    assert prop(repo, "PV-201")["geometry_version"] == before
    assert get_current_geometry_version(repo, "PV-201").version == 99


def test_versioning_unknown_object_raises(repo):
    with pytest.raises(UnknownObject):
        create_geometry_version(repo, "NOPE-1", z_max=1.0)


# --------------------------------------------------------------------------
# compare_geometry_versions
# --------------------------------------------------------------------------


def test_compare_reports_geometry_change(repo):
    move_footprint(repo, "PV-201", [(10, 8), (30, 8), (30, 28), (10, 28)])
    before, after = get_geometry_history(repo, "PV-201")
    diff = compare_geometry_versions(before, after)
    assert diff.from_version == 1 and diff.to_version == 2
    assert diff.geometry_changed is True
    assert diff.geometry_hash_changed is True
    assert diff.from_geometry_hash != diff.to_geometry_hash
    assert diff.delta_area_m2 < 0
    assert diff.delta_volume_m3 < 0


def test_compare_reports_z_range_change(repo):
    # PV-201 sits on floor 2: z 3.2 -> 6.4. Raising the ceiling to 9.9 is +3.5.
    create_geometry_version(repo, "PV-201", z_max=9.9)
    before, after = get_geometry_history(repo, "PV-201")
    diff = compare_geometry_versions(before, after)
    assert diff.z_range_changed is True
    assert diff.delta_z_max == pytest.approx(3.5, abs=0.01)


def test_compare_always_reports_identity_unchanged(repo):
    """The machine-checkable form of the stability invariant."""
    move_footprint(repo, "PV-201", [(12, 9), (38, 9), (38, 42), (12, 42)])
    before, after = get_geometry_history(repo, "PV-201")
    assert compare_geometry_versions(before, after).ulpin_changed is False


def test_compare_identical_versions_shows_no_change(repo):
    version = get_current_geometry_version(repo, "PV-201")
    diff = compare_geometry_versions(version, version)
    assert diff.geometry_changed is False
    assert diff.z_range_changed is False
    assert diff.delta_area_m2 == 0
    assert diff.delta_volume_m3 == 0


def test_compare_rejects_different_objects(repo):
    a = get_current_geometry_version(repo, "PV-201")
    b = get_current_geometry_version(repo, "PV-101")
    with pytest.raises(ValueError):
        compare_geometry_versions(a, b)


# --------------------------------------------------------------------------
# separation of concerns
# --------------------------------------------------------------------------


def test_identity_hash_and_version_are_three_separate_concepts(repo):
    record = prop(repo, "PV-201")
    ulpin, geom_hash, geom_version = (
        record["prototype_ulpin"],
        record["geometry_hash"],
        record["geometry_version"],
    )
    # Distinct values, each with its own job.
    assert ulpin != geom_hash
    assert isinstance(geom_version, int)
    # The ULPIN embeds neither the hash nor the version.
    assert geom_hash[:6] not in ulpin
    assert str(geom_version) not in ulpin.split("-")
