"""PostGIS integration tests.

Skipped automatically when no database is reachable, so the unit suite still runs
anywhere. Point at a database with::

    ALEMBIC_DATABASE_URL / DATABASE_URL, or
    VCAD_TEST_DATABASE_URL (takes precedence)

The database must already be migrated (``alembic upgrade head``); these tests
assume a schema and never create one.

Run just these with::

    python -m pytest tests/test_postgres_integration.py -v
"""
from __future__ import annotations

import os
import uuid

import pytest
import sqlalchemy as sa
from geoalchemy2.shape import to_shape
from shapely.geometry import box, shape

from app.db.models import Base, PropertyVolume
from app.db.session import (
    check_database_health,
    get_database_url,
    get_engine,
    reset_engine,
    session_scope,
    set_database_url,
    uses_postgres,
)
from app.db.types import GEOGRAPHIC_SRID, METRIC_SRID
from app.repositories.postgres import PostgresCadastreRepository
from app.services.demo import seed_demo
from app.services.geometry import (
    DEMO_ANCHOR_LAT,
    DEMO_ANCHOR_LON,
    GEOGRAPHIC_CRS,
    PROCESSING_CRS,
    geojson_to_polygon,
    polygon_to_geojson,
    xy_to_ll,
)
from app.services.geometry_versioning import (
    create_geometry_version,
    get_current_geometry_version,
    get_geometry_history,
)
from app.services.validation import validate

#: Footprint bounds in local-plane metres, converted to WGS84 the way the app does.
BOUNDS = (10, 8, 70, 44)


def test_url() -> str | None:
    return (
        os.getenv("VCAD_TEST_DATABASE_URL")
        or os.getenv("ALEMBIC_DATABASE_URL")
        or os.getenv("DATABASE_URL")
    )


TEST_URL = test_url()

pytestmark = pytest.mark.skipif(
    not TEST_URL,
    reason="No database configured; set VCAD_TEST_DATABASE_URL to run these",
)


@pytest.fixture(scope="module", autouse=True)
def _database():
    """Point the app at the test database and verify PostGIS is usable."""
    set_database_url(TEST_URL)
    reset_engine()
    health = check_database_health()
    if health["status"] != "ok":
        pytest.skip(f"database not usable: {health.get('error')}")
    # Confirm the schema is actually present, not just the server.
    engine = get_engine()
    with engine.connect() as conn:
        present = sa.inspect(conn).get_table_names()
    missing = set(Base.metadata.tables) - set(present)
    if missing:
        pytest.skip(f"schema not migrated; run `alembic upgrade head` (missing {sorted(missing)})")
    yield
    reset_engine()
    set_database_url(None)


@pytest.fixture
def repo():
    """A repository on a clean database, committed on success."""
    with session_scope() as session:
        repository = PostgresCadastreRepository(session)
        _truncate(session)
        yield repository


def _truncate(session) -> None:
    """Empty every application table, children before parents.

    Explicit list, and it must name **every** collection: a table missing here
    survives between tests, so one test's review decisions or audit events leak
    into the next and the two backends stop behaving identically.
    """
    from app.db.models import COLLECTION_MODELS

    for model in (
        COLLECTION_MODELS["provenance_links"],
        COLLECTION_MODELS["cadastral_changes"],

        COLLECTION_MODELS["review_decisions"],
        COLLECTION_MODELS["review_cases"],
        COLLECTION_MODELS["audit_events"],
        COLLECTION_MODELS["generated_property_volumes"],
        COLLECTION_MODELS["extracted_floors"],
        COLLECTION_MODELS["extracted_buildings"],
        COLLECTION_MODELS["processing_jobs"],
        COLLECTION_MODELS["geometry_versions"],
        COLLECTION_MODELS["issues"],
        COLLECTION_MODELS["sources"],
        COLLECTION_MODELS["properties"],
        COLLECTION_MODELS["floors"],
        COLLECTION_MODELS["infrastructure"],
        COLLECTION_MODELS["buildings"],
        COLLECTION_MODELS["parcels"],
    ):
        session.execute(sa.delete(model))
    session.flush()


def _parcel_record(**overrides) -> dict:
    record = {
        "id": f"parcel-{uuid.uuid4().hex[:8]}",
        "parcel_id": "P-900",
        "prototype_ulpin": None,
        "geometry": polygon_to_geojson(box(0, 0, 80, 52)),
        "area": 4160.0,
        "land_use": "Test",
        "survey_reference": "Test sheet",
        "version": 1,
    }
    record.update(overrides)
    return record


# --------------------------------------------------------------------------
# health / connectivity
# --------------------------------------------------------------------------


def test_health_reports_postgis(repo):
    health = repo.health_check()
    assert health["status"] == "ok"
    assert health["postgis"] is True
    assert health["dialect"] == "postgresql"
    assert health["error"] is None
    assert health["collections"]["parcels"] == 0


def test_uses_postgres_is_true_under_test():
    assert uses_postgres() is True


# --------------------------------------------------------------------------
# create: parcels, buildings, floors, property volumes
# --------------------------------------------------------------------------


def test_create_parcel(repo):
    created = repo.add("parcels", _parcel_record())
    assert created["id"]
    stored = repo.records("parcels")
    assert len(stored) == 1
    assert stored[0]["parcel_id"] == "P-900"
    assert stored[0]["geometry"]["type"] == "Polygon"


def test_create_building(repo):
    repo.add("parcels", _parcel_record())
    repo.add(
        "buildings",
        {
            "id": "building-900",
            "building_id": "B-900",
            "parcel_id": "P-900",
            "footprint": polygon_to_geojson(box(10, 8, 70, 44)),
            "height": 25.6,
            "floor_count": 8,
            "confidence": 0.974,
            "version": 1,
        },
    )
    stored = repo.records("buildings")
    assert len(stored) == 1
    assert stored[0]["building_id"] == "B-900"
    assert stored[0]["parcel_id"] == "P-900"


def test_create_floor(repo):
    repo.add("parcels", _parcel_record())
    repo.add(
        "buildings",
        {
            "id": "building-900",
            "building_id": "B-900",
            "parcel_id": "P-900",
            "height": 25.6,
            "floor_count": 1,
            "version": 1,
        },
    )
    repo.add(
        "floors",
        {
            "id": "floor-900",
            "building_id": "B-900",
            "floor_number": 1,
            "z_min": 0.0,
            "z_max": 3.2,
            "confidence": 0.918,
            "version": 1,
        },
    )
    stored = repo.records("floors")
    assert len(stored) == 1
    assert stored[0]["floor_number"] == 1
    assert stored[0]["z_max"] == 3.2


def test_create_property_volume(repo):
    repo.add("parcels", _parcel_record())
    created = repo.add(
        "properties",
        {
            "id": "PV-900",
            "parent_parcel_id": "P-900",
            "building_id": "B-900",
            "property_type": "APARTMENT",
            "floor_number": 1,
            "unit_label": "Apartment 901",
            "z_min": 0.0,
            "z_max": 3.2,
            "geometry_3d": polygon_to_geojson(box(*BOUNDS)),
            "volume_m3": 1080.0,
            "area_m2": 1080.0,
            "geometry_hash": "ABCDEF012345",
            "status": "MAPPED",
            "confidence": 0.918,
            "version": 1,
        },
    )
    assert created["id"] == "PV-900"
    stored = repo.records("properties")
    assert len(stored) == 1
    assert stored[0]["unit_label"] == "Apartment 901"


def test_create_infrastructure(repo):
    repo.add(
        "infrastructure",
        {
            "id": "INF-900",
            "type": "UNDERGROUND_UTILITY_CORRIDOR",
            "geometry_3d": polygon_to_geojson(box(18, 21, 62, 25)),
            "z_min": -2.2,
            "z_max": 0.0,
            "owner": "Test Utility",
            "reference": "UG-TEST",
            "version": 1,
        },
    )
    stored = repo.records("infrastructure")
    assert stored[0]["owner"] == "Test Utility"
    assert stored[0]["reference"] == "UG-TEST"


# --------------------------------------------------------------------------
# SRID preservation and metric correctness
# --------------------------------------------------------------------------


def test_geometry_srid_is_preserved(repo):
    """Stored footprints must come back as SRID 4326, not an assumed CRS."""
    repo.add("parcels", _parcel_record())
    srid = repo.session.execute(
        sa.text("SELECT ST_SRID(geometry) FROM parcels LIMIT 1")
    ).scalar()
    assert srid == GEOGRAPHIC_SRID


def test_metric_column_is_projected_and_indexed(repo):
    repo.add("parcels", _parcel_record())
    row_srid = repo.session.execute(
        sa.text("SELECT ST_SRID(geometry_metric) FROM parcels LIMIT 1")
    ).scalar()
    assert row_srid == METRIC_SRID


def test_area_is_square_metres_not_square_degrees(repo):
    """The whole point of the metric companion column.

    ``ST_Area`` on the 4326 column returns square degrees; on the projected
    column it returns square metres. The two differ by ~10 orders of magnitude,
    so this asserts the metric column is the one that looks like an area.
    """
    repo.add("parcels", _parcel_record())
    square_degrees = repo.session.execute(
        sa.text("SELECT ST_Area(geometry) FROM parcels LIMIT 1")
    ).scalar()
    square_metres = repo.session.execute(
        sa.text("SELECT ST_Area(geometry_metric) FROM parcels LIMIT 1")
    ).scalar()
    # A square_degree value is ~1e-05; a 4160 m^2 parcel is thousands.
    assert square_degrees < 1e-3
    assert 3000 < square_metres < 6000
    # And the repository helper agrees with the database.
    assert repo.metric_area_m2("parcels", repo.records("parcels")[0]["id"]) == pytest.approx(
        square_metres, rel=1e-6
    )


def test_postgis_metric_area_agrees_with_the_engine(repo):
    """The database and the geometry engine must measure the same site the same way.

    The engine works in a local plane that is the processing CRS translated to
    the demo anchor; PostGIS stores WGS84 and reprojects to SRID 32643 on the
    fly. Agreement proves the two metric paths are consistent.
    """
    seed_demo(repo)
    from app.services.geometry import calculate_area, record_to_polygon

    parcel = repo.records("parcels")[0]
    engine_area = calculate_area(record_to_polygon(parcel))
    database_area = repo.metric_area_m2("parcels", parcel["id"])
    assert engine_area == pytest.approx(4160.0, rel=1e-3)
    assert database_area == pytest.approx(engine_area, rel=2e-3)


def test_stored_srid_is_the_geographic_crs_not_the_processing_one(repo):
    """Footprints are stored in degrees; the metre CRS is derived, not stored."""
    seed_demo(repo)
    stored = repo.session.execute(
        sa.text(
            "SELECT DISTINCT srid FROM geometry_columns "
            "WHERE f_table_schema='public' AND f_table_name='parcels' "
            "AND f_geometry_column='geometry'"
        )
    ).scalar()
    assert stored == GEOGRAPHIC_SRID


def test_data_source_crs_is_persisted(repo):
    """CRS provenance must survive a round-trip through the database."""
    seed_demo(repo)
    stored = repo.records("sources")[0]
    assert stored["crs"] == GEOGRAPHIC_CRS
    assert stored["metadata"]["source_crs"] == GEOGRAPHIC_CRS
    # The processing CRS is recorded too, so the choice is auditable.
    assert stored["metadata"]["processing_crs"] == PROCESSING_CRS
    row = repo.session.execute(
        sa.text("SELECT crs, metadata->>'source_crs' FROM data_sources WHERE id='DS-001'")
    ).one()
    assert row[0] == GEOGRAPHIC_CRS
    assert row[1] == GEOGRAPHIC_CRS


def test_geographic_predicate_is_srided(repo):
    """A 4326 predicate must not be compared against a metric geometry."""
    repo.add("parcels", _parcel_record())
    hits = repo.session.execute(
        sa.text(
            "SELECT count(*) FROM parcels "
            "WHERE ST_Intersects(geometry, ST_GeomFromText("
            "'POLYGON((77.20 28.61,77.22 28.61,77.22 28.63,77.20 28.63,77.20 28.61))',4326))"
        )
    ).scalar()
    assert hits == 1


# --------------------------------------------------------------------------
# spatial query
# --------------------------------------------------------------------------


def test_spatial_query_finds_intersecting_volumes(repo):
    repo.add("parcels", _parcel_record())
    for ident, cells in (
        ("PV-901", (10, 8, 40, 44)),
        ("PV-902", (50, 8, 70, 44)),
    ):
        repo.add(
            "properties",
            {
                "id": ident,
                "parent_parcel_id": "P-900",
                "property_type": "APARTMENT",
                "unit_label": ident,
                "z_min": 0.0,
                "z_max": 3.2,
                "geometry_3d": polygon_to_geojson(box(*cells)),
                "geometry_hash": "HASH" + ident[-3:],
                "volume_m3": 1.0,
                "area_m2": 1.0,
                "version": 1,
            },
        )
    # Probe over the western unit only. The probe is given in WGS84, so build it
    # by converting from the local plane rather than hand-writing degrees.
    west = geojson_to_polygon(polygon_to_geojson(box(12, 10, 38, 40)))
    hits = repo.spatial_query("properties", polygon_to_geojson(west))
    assert [h["id"] for h in hits] == ["PV-901"]


def test_spatial_query_misses_disjoint_geometry(repo):
    repo.add("parcels", _parcel_record())
    repo.add(
        "properties",
        {
            "id": "PV-903",
            "parent_parcel_id": "P-900",
            "property_type": "APARTMENT",
            "z_min": 0.0,
            "z_max": 3.2,
            "geometry_3d": polygon_to_geojson(box(10, 8, 40, 44)),
            "geometry_hash": "HASH903",
            "volume_m3": 1.0,
            "area_m2": 1.0,
            "version": 1,
        },
    )
    far_away = {"type": "Polygon", "coordinates": [[[78.0, 28.0], [78.1, 28.0],
                                                     [78.1, 28.1], [78.0, 28.1], [78.0, 28.0]]]}
    assert repo.spatial_query("properties", far_away) == []


def test_spatial_query_rejects_unknown_relationship(repo):
    with pytest.raises(ValueError):
        repo.spatial_query("parcels", {"type": "Polygon", "coordinates": []},
                           relationship="teleports_to")


# --------------------------------------------------------------------------
# validation issues
# --------------------------------------------------------------------------


def test_validation_issues_persist(repo):
    seed_demo(repo)
    issues = repo.records("issues")
    assert len(issues) == 3
    ids = {i["id"] for i in issues}
    assert ids == {"VAL-OUT-PV-502", "VAL-OVR-PV-201-PV-202", "VAL-INF-PV-B001"}
    overlap = next(i for i in issues if i["id"] == "VAL-OVR-PV-201-PV-202")
    assert overlap["severity"] == "CRITICAL"
    # 180 m^2 of plan overlap x 3.2 m of shared height, within the tolerance
    # implied by measuring after a 7-dp WGS84 round-trip.
    assert overlap["overlap_volume"] == pytest.approx(576.0, rel=5e-3)
    assert overlap["geometry"] is not None


def test_validation_issues_geometry_srid_and_severity_survive(repo):
    seed_demo(repo)
    row = repo.session.execute(
        sa.text(
            "SELECT ST_SRID(geometry), severity, gap_m FROM validation_issues "
            "WHERE id = 'VAL-OVR-PV-201-PV-202'"
        )
    ).one()
    assert row[0] == GEOGRAPHIC_SRID
    assert row[1] == "CRITICAL"
    assert row[2] is None


def test_validation_rule_attribution_and_evidence_survive(repo):
    """The rule engine's columns round-trip through JSONB and TEXT.

    A finding that loses its evidence on the way to disk is an assertion rather
    than a measurement, so this is asserted rather than assumed.
    """
    seed_demo(repo)
    by_id = {i["id"]: i for i in repo.records("issues")}
    assert by_id["VAL-OUT-PV-502"]["rule_id"] == "PARCEL-PROPERTY-OUTSIDE"
    assert by_id["VAL-OUT-PV-502"]["category"] == "PARCEL"
    assert by_id["VAL-OUT-PV-502"]["evidence"]["outside_area_m2"] == pytest.approx(
        216.25, rel=5e-3
    )
    assert by_id["VAL-OVR-PV-201-PV-202"]["category"] == "VERTICAL"
    assert by_id["VAL-INF-PV-B001"]["category"] == "INFRASTRUCTURE"
    assert (
        by_id["VAL-INF-PV-B001"]["evidence"]["infrastructure_type"]
        == "UNDERGROUND_UTILITY_CORRIDOR"
    )
    # Stored as real JSONB, not as text.
    row = repo.session.execute(
        sa.text(
            "SELECT jsonb_typeof(evidence) FROM validation_issues "
            "WHERE id = 'VAL-OVR-PV-201-PV-202'"
        )
    ).one()
    assert row[0] == "object"


def test_rerunning_validation_replaces_rows(repo):
    seed_demo(repo)
    first = repo.records("issues")
    validate(repo)
    second = repo.records("issues")
    assert len(first) == len(second) == 3
    assert {i["id"] for i in first} == {i["id"] for i in second}


# --------------------------------------------------------------------------
# geometry versions
# --------------------------------------------------------------------------


def test_geometry_versions_persist_for_the_whole_seed(repo):
    seed_demo(repo)
    versions = repo.records("geometry_versions")
    # 1 parcel + 1 building + 17 volumes + 1 infrastructure
    assert len(versions) == 20
    assert {v["version"] for v in versions} == {1}
    for record_type in ("PARCEL", "BUILDING", "PROPERTY_VOLUME", "INFRASTRUCTURE"):
        assert any(v["object_type"] == record_type for v in versions)


def test_geometry_version_history_persists_across_sessions(repo):
    seed_demo(repo)
    before = get_geometry_history(repo, "PV-201")
    assert len(before) == 1
    create_geometry_version(
        repo,
        "PV-201",
        geometry=polygon_to_geojson(box(*BOUNDS)),
        change_reason="re-survey",
    )
    # Commit, then open a *brand-new* session: if history is visible here it is
    # genuinely in the database rather than in an uncommitted transaction.
    repo.session.commit()
    with session_scope() as fresh_session:
        fresh = PostgresCadastreRepository(fresh_session)
        history = get_geometry_history(fresh, "PV-201")
        assert [v.version for v in history] == [1, 2]
        assert history[-1].change_reason == "re-survey"
        assert history[-1].source_id is None


def test_geometry_version_increments_and_keeps_identity(repo):
    seed_demo(repo)
    original = next(p for p in repo.records("properties") if p["id"] == "PV-201")
    original_ulpin = original["prototype_ulpin"]
    original_hash = original["geometry_hash"]

    create_geometry_version(
        repo,
        "PV-201",
        geometry={
            "type": "Polygon",
            "coordinates": [
                [xy_to_ll(12, 9), xy_to_ll(38, 9), xy_to_ll(38, 42),
                 xy_to_ll(12, 42), xy_to_ll(12, 9)]
            ],
        },
        change_reason="boundary change",
    )

    updated = next(p for p in repo.records("properties") if p["id"] == "PV-201")
    assert updated["prototype_ulpin"] == original_ulpin  # identity is stable
    assert updated["geometry_hash"] != original_hash  # geometry hash moves
    assert updated["geometry_version"] == 2
    assert get_current_geometry_version(repo, "PV-201").version == 2


def test_geometry_version_derived_area_refreshes(repo):
    seed_demo(repo)
    create_geometry_version(
        repo,
        "PV-201",
        geometry={
            "type": "Polygon",
            "coordinates": [
                [xy_to_ll(10, 8), xy_to_ll(30, 8), xy_to_ll(30, 28),
                 xy_to_ll(10, 28), xy_to_ll(10, 8)]
            ],
        },
    )
    updated = next(p for p in repo.records("properties") if p["id"] == "PV-201")
    assert updated["area_m2"] == pytest.approx(400, rel=1e-2)


# --------------------------------------------------------------------------
# stable ULPIN persistence
# --------------------------------------------------------------------------


def test_stable_ulpin_persists_and_is_not_geometry_derived(repo):
    seed_demo(repo)
    parcel = repo.records("parcels")[0]
    assert parcel["prototype_ulpin"] == "VC-LP-P001"
    volume = next(p for p in repo.records("properties") if p["id"] == "PV-201")
    assert volume["prototype_ulpin"] == "VC-VP-P001-APARTMENT201"
    # The stored identifier embeds neither a hash nor a grid index.
    assert volume["geometry_hash"][:6] not in volume["prototype_ulpin"]


def test_ulpin_survives_re_issuing(repo):
    from app.services.ulpin import assign_ulpins

    seed_demo(repo)
    before = {p["id"]: p["prototype_ulpin"] for p in repo.records("properties")}
    result = assign_ulpins(repo, overwrite=True)
    after = {p["id"]: p["prototype_ulpin"] for p in repo.records("properties")}
    assert before == after
    assert result["assigned"] == 18


def test_ulpin_lookup_by_stable_identifier(repo):
    seed_demo(repo)
    found = repo.search("VC-VP-P001-APARTMENT201")
    assert found.kind == "property"
    assert found.record["id"] == "PV-201"
    # Label search still works too.
    assert repo.search("Apartment 201").record["id"] == "PV-201"


# --------------------------------------------------------------------------
# full demo seed against PostGIS
# --------------------------------------------------------------------------


def test_demo_seed_produces_the_same_counts_as_the_in_memory_store(repo):
    result = seed_demo(repo)
    assert result == {"message": "Demo City loaded", "properties": 17, "issues": 3}
    assert len(repo.records("parcels")) == 1
    assert len(repo.records("buildings")) == 1
    assert len(repo.records("floors")) == 8
    assert len(repo.records("properties")) == 17
    assert len(repo.records("infrastructure")) == 1
    assert len(repo.records("issues")) == 3


def test_demo_seed_is_idempotent(repo):
    first = seed_demo(repo)
    second = seed_demo(repo)
    assert first == second
    assert len(repo.records("properties")) == 17
    assert len(repo.records("geometry_versions")) == 20


def test_demo_geometry_roundtrips_without_drift(repo):
    """A footprint stored and reloaded keeps its coordinates and SRID."""
    seed_demo(repo)
    stored = repo.records("parcels")[0]["geometry"]
    ring = stored["coordinates"][0]
    assert ring[0] == ring[-1]  # closed
    # The real anchor position must be present among the corners, and every
    # coordinate must sit in the demo's neighbourhood.
    assert [DEMO_ANCHOR_LON, DEMO_ANCHOR_LAT] in ring
    for lon, lat in ring:
        assert abs(lon - DEMO_ANCHOR_LON) < 0.01
        assert abs(lat - DEMO_ANCHOR_LAT) < 0.01
    assert len(ring) == 5


def test_transaction_rollback_leaves_no_partial_writes():
    """A failed unit of work must not leave half a seed behind."""
    # Clear in its own committed transaction first, so the assertion below is
    # about the *failing* transaction rather than leftovers from another test.
    with session_scope() as session:
        _truncate(session)
    with pytest.raises(RuntimeError):
        with session_scope() as session:
            repository = PostgresCadastreRepository(session)
            repository.add("parcels", _parcel_record())
            raise RuntimeError("simulated failure mid-transaction")
    with session_scope() as session:
        repository = PostgresCadastreRepository(session)
        assert repository.records("parcels") == []


def test_search_miss_raises_record_not_found(repo):
    from app.repositories.base import RecordNotFound

    seed_demo(repo)
    with pytest.raises(RecordNotFound):
        repo.search("no-such-object")


# --------------------------------------------------------------------------
# human verification and audit history
# --------------------------------------------------------------------------


def test_review_workflow_persists(repo):
    from app.services import reviews

    seed_demo(repo)
    case = reviews.create_review_case(
        repo, "VAL-OUT-PV-502", reason="apartment overhangs the boundary"
    )
    reviews.assign_review_case(repo, case["id"], "surveyor-1")
    reviews.approve_review(
        repo, "VAL-OUT-PV-502", reason="confirmed on site", reviewer="surveyor-1"
    )

    stored = reviews.get_review_case(repo, "VAL-OUT-PV-502")
    assert stored["state"] == "APPROVED"
    assert stored["assigned_to"] == "surveyor-1"
    assert stored["review_count"] == 1

    decision = reviews.get_review_decisions(repo, "VAL-OUT-PV-502")[0]
    assert decision["reviewer"] == "surveyor-1"
    assert decision["reason"] == "confirmed on site"
    assert decision["object_id"] == "PV-502"
    assert decision["related_object_id"] == "P-001"
    assert decision["previous_state"] == "IN_REVIEW"
    assert decision["new_state"] == "APPROVED"
    # Timestamps are normalised to ISO strings so this backend returns the same
    # record shape as the in-memory store; a raw datetime here would be a
    # backend-dependent difference in an audit record.
    assert isinstance(decision["decided_at"], str)
    assert isinstance(stored["created_at"], str)


def test_one_review_case_per_issue_is_enforced_by_the_database():
    """A unique constraint, not just service code.

    Two open cases on one finding would let two people decide it and the audit
    trail would record a contradiction, so the database refuses the second.

    Deliberately does not use the ``repo`` fixture: that fixture holds an open
    transaction, and nesting a second session here would block on its own locks.
    Each step commits in its own transaction, as in
    ``test_transaction_rollback_leaves_no_partial_writes``.
    """
    with session_scope() as session:
        repository = PostgresCadastreRepository(session)
        _truncate(session)
        repository.add(
            "review_cases",
            {"id": "RC-A", "issue_id": "VAL-OUT-PV-502", "state": "PENDING"},
        )
    with pytest.raises(sa.exc.IntegrityError):
        with session_scope() as session:
            repository = PostgresCadastreRepository(session)
            repository.add(
                "review_cases",
                {"id": "RC-B", "issue_id": "VAL-OUT-PV-502", "state": "PENDING"},
            )


def test_audit_events_persist_with_jsonb_context(repo):
    from app.services.audit import get_audit_history, get_recent_audit_events

    seed_demo(repo)
    assert repo.records("audit_events")
    event = next(
        e for e in get_recent_audit_events(repo, limit=500)
        if e["action"] == "VALIDATED"
    )
    # Stored as real JSONB, not as text.
    row = repo.session.execute(
        sa.text(
            "SELECT jsonb_typeof(context), context->>'findings' FROM audit_events "
            "WHERE id = :id"
        ),
        {"id": event["id"]},
    ).one()
    assert row[0] == "object"
    assert row[1] == "3"
    assert isinstance(event["occurred_at"], str)


def test_audit_history_is_readable_per_object(repo):
    from app.services.audit import get_audit_history

    seed_demo(repo)
    history = get_audit_history(repo, "PV-502")
    assert history
    assert history[0]["action"] == "CREATED"
    assert history[0]["object_type"] == "PROPERTY_VOLUME"
    stamps = [e["occurred_at"] for e in history]
    assert stamps == sorted(stamps)


def test_audit_events_really_cannot_be_updated_in_postgres(repo):
    """The append-only guarantee, asserted against the database itself.

    Asserted against the store rather than only the service because on this
    backend the field filter is the *only* thing preventing a rewrite.
    """
    from app.services.audit import create_audit_event, get_recent_audit_events

    seed_demo(repo)
    event = create_audit_event(
        repo, action="APPROVED", object_id="P-001", detail="original"
    )
    repo.update("audit_events", event["id"], {"detail": "tampered"})
    repo.session.commit()
    stored = next(
        e for e in get_recent_audit_events(repo, limit=500) if e["id"] == event["id"]
    )
    assert stored["detail"] == "original"


def test_review_decisions_really_cannot_be_updated_in_postgres(repo):
    from app.services import reviews

    seed_demo(repo)
    reviews.create_review_case(repo, "VAL-OUT-PV-502", reason="check")
    reviews.approve_review(
        repo, "VAL-OUT-PV-502", reason="confirmed", reviewer="surveyor-1"
    )
    decision = reviews.get_review_decisions(repo, "VAL-OUT-PV-502")[0]
    repo.update("review_decisions", decision["id"], {"reason": "tampered"})
    repo.session.commit()
    stored = next(
        d for d in repo.records("review_decisions") if d["id"] == decision["id"]
    )
    assert stored["reason"] == "confirmed"


def test_reopening_clears_the_decision_on_postgres_too(repo):
    """The delete-then-insert path, which an update cannot express.

    A change set drops ``None`` values, so an update would leave the stale
    ``APPROVED`` sitting on a case claiming to be pending.
    """
    from app.services import reviews

    seed_demo(repo)
    reviews.create_review_case(repo, "VAL-OUT-PV-502", reason="check")
    reviews.approve_review(
        repo, "VAL-OUT-PV-502", reason="confirmed", reviewer="surveyor-1"
    )
    reviews.reopen_validation_issue(
        repo, "VAL-OUT-PV-502", reason="new evidence", reviewer="surveyor-1"
    )
    case = reviews.get_review_case(repo, "VAL-OUT-PV-502")
    assert case["state"] == "PENDING"
    assert case["decision"] is None
    assert case["decided_at"] is None
    assert case["decided_by"] is None
    # The decision history survives the reopen.
    assert len(reviews.get_review_decisions(repo, "VAL-OUT-PV-502")) == 1


def test_review_outcome_survives_a_validation_re_run_on_postgres(repo):
    from app.services import reviews
    from app.services.validation import validate

    seed_demo(repo)
    reviews.create_review_case(repo, "VAL-OUT-PV-502", reason="check")
    reviews.approve_review(
        repo, "VAL-OUT-PV-502", reason="confirmed", reviewer="surveyor-1"
    )
    assert validate(repo)["issues"] == 3
    issues = {i["id"]: i for i in repo.records("issues")}
    assert issues["VAL-OUT-PV-502"]["status"] == "APPROVED"
    assert issues["VAL-OVR-PV-201-PV-202"]["status"] == "OPEN"


# --------------------------------------------------------------------------
# cadastral change detection
# --------------------------------------------------------------------------


def _survey_object(
    object_id="PV-101", box=(10.0, 8.0, 40.0, 44.0), z_min=0.0, z_max=3.2, floor=1
):
    from app.services.geometry import create_rectangle, polygon_to_geojson

    return {
        "id": object_id,
        "geometry_3d": polygon_to_geojson(create_rectangle(*box)),
        "z_min": z_min,
        "z_max": z_max,
        "floor_number": floor,
        "building_id": "B-001",
    }


def test_detected_changes_persist_with_both_geometries(repo):
    from app.services.change_detection import compare_approved_vs_survey

    seed_demo(repo)
    compare_approved_vs_survey(
        [_survey_object()],
        [_survey_object(box=(10.0, 8.0, 50.0, 44.0))],
        source_id="DS-CHANGE",
        repository=repo,
    )
    changes = repo.records("cadastral_changes")
    assert changes
    stored = next(c for c in changes if c["change_type"] == "FOOTPRINT")
    # Both outlines survive the round-trip, as JSONB rather than geometry
    # columns: they are evidence of a comparison, not a queryable location.
    assert stored["previous_geometry"]["type"] == "Polygon"
    assert stored["new_geometry"]["type"] == "Polygon"
    assert stored["previous_z_min"] == pytest.approx(0.0)
    assert stored["new_z_max"] == pytest.approx(3.2)
    assert stored["status"] == "REQUIRES_VERIFICATION"
    assert stored["source_id"] == "DS-CHANGE"
    row = repo.session.execute(
        sa.text(
            "SELECT jsonb_typeof(previous_geometry), jsonb_typeof(new_geometry) "
            "FROM cadastral_changes WHERE id = :id"
        ),
        {"id": stored["id"]},
    ).one()
    assert row[0] == "object"
    assert row[1] == "object"


def test_a_change_keeps_its_status_and_deltas_on_postgres(repo):
    from app.services.change_detection import compare_approved_vs_survey

    seed_demo(repo)
    compare_approved_vs_survey(
        [_survey_object(z_max=3.2)],
        [_survey_object(z_max=9.6)],
        source_id="DS-CHANGE",
        repository=repo,
    )
    stored = next(
        c for c in repo.records("cadastral_changes") if c["change_type"] == "HEIGHT"
    )
    assert stored["height_delta"] == pytest.approx(6.4, rel=5e-3)
    assert stored["previous_height_m"] == pytest.approx(3.2, rel=5e-3)
    assert stored["new_height_m"] == pytest.approx(9.6, rel=5e-3)
    assert 0.0 <= stored["geometric_quality"] <= 1.0
    assert isinstance(stored["detected_at"], str)


def test_an_unregistered_floor_finding_persists(repo):
    from app.services.change_detection import compare_approved_vs_survey

    seed_demo(repo)
    compare_approved_vs_survey(
        [_survey_object(floor=1)],
        [_survey_object(floor=1), _survey_object("PV-900", floor=42)],
        source_id="DS-CHANGE",
        repository=repo,
    )
    findings = [
        i
        for i in repo.records("issues")
        if i["issue_type"] == "UNREGISTERED_FLOOR"
    ]
    assert len(findings) == 1
    assert findings[0]["evidence"]["floor_number"] == 42
    assert findings[0]["evidence"]["source_id"] == "DS-CHANGE"
    assert findings[0]["severity"] == "WARNING"
    assert "requires verification" in findings[0]["description"]


def test_a_change_opens_a_review_case_on_postgres(repo):
    from app.services.change_detection import compare_approved_vs_survey

    seed_demo(repo)
    report = compare_approved_vs_survey(
        [_survey_object()],
        [_survey_object(box=(10.0, 8.0, 50.0, 44.0))],
        source_id="DS-CHANGE",
        repository=repo,
    )
    assert report.review_cases_created == len(report.changes)
    assert repo.records("review_cases")
    assert all(
        c["state"] == "PENDING" for c in repo.records("review_cases")
    )


def test_change_records_cannot_be_updated_in_postgres(repo):
    """A change is a record of a comparison that already happened.

    Asserted against the database, where the empty field set is the only thing
    preventing a rewrite.
    """
    from app.services.change_detection import compare_approved_vs_survey

    seed_demo(repo)
    compare_approved_vs_survey(
        [_survey_object()],
        [_survey_object(box=(10.0, 8.0, 50.0, 44.0))],
        source_id="DS-CHANGE",
        repository=repo,
    )
    stored = repo.records("cadastral_changes")[0]
    repo.update("cadastral_changes", stored["id"], {"status": "VERIFIED", "area_delta": 0.0})
    repo.session.commit()
    after = next(
        c for c in repo.records("cadastral_changes") if c["id"] == stored["id"]
    )
    assert after["status"] == "REQUIRES_VERIFICATION"
    assert after["area_delta"] == stored["area_delta"]


def test_change_report_reads_back_from_postgres(repo):
    from app.services.change_detection import (
        compare_approved_vs_survey,
        generate_change_report,
    )

    seed_demo(repo)
    compare_approved_vs_survey(
        [_survey_object()],
        [_survey_object(box=(10.0, 8.0, 50.0, 44.0))],
        source_id="DS-CHANGE",
        repository=repo,
    )
    report = generate_change_report(repo, source_id="DS-CHANGE")
    assert report["total_changes"] >= 1
    assert report["by_type"]["FOOTPRINT"] == 1
    assert report["all_require_verification"] is True
    assert generate_change_report(repo)["total_changes"] == report["total_changes"]


def test_a_comparison_does_not_touch_cadastral_geometry_on_postgres(repo):
    """A re-survey supersedes; the approved geometry stays as it was."""
    from app.services.change_detection import compare_approved_vs_survey

    seed_demo(repo)
    before = next(p for p in repo.records("properties") if p["id"] == "PV-101")
    original = before["geometry_3d"]
    compare_approved_vs_survey(
        [_survey_object()],
        [_survey_object(box=(0.0, 0.0, 900.0, 900.0))],
        source_id="DS-CHANGE",
        repository=repo,
    )
    repo.session.commit()
    after = next(p for p in repo.records("properties") if p["id"] == "PV-101")
    assert after["geometry_3d"] == original


# --------------------------------------------------------------------------
# provenance
# --------------------------------------------------------------------------


def test_provenance_links_persist_and_round_trip(repo):
    from app.services.provenance import create_provenance_record

    seed_demo(repo)
    link = create_provenance_record(
        repo,
        stage="PROPERTY_VOLUME",
        object_id="GPV-1",
        source_id="DS-1",
        processing_job_id="JOB-1",
        parent_id="XF-1",
        parent_stage="FLOOR",
        algorithm="derived_geometric",
        method_description="Planar subdivision of a surveyed footprint",
        parameters={"floor_number": 2, "units_inferred": False},
    )
    stored = next(
        l for l in repo.records("provenance_links") if l["id"] == link["id"]
    )
    assert stored["stage"] == "PROPERTY_VOLUME"
    assert stored["source_id"] == "DS-1"
    assert stored["processing_job_id"] == "JOB-1"
    assert stored["parent_stage"] == "FLOOR"
    assert stored["algorithm"] == "derived_geometric"
    assert stored["method_description"]
    # Stored as real JSONB.
    row = repo.session.execute(
        sa.text(
            "SELECT jsonb_typeof(parameters), parameters->>'units_inferred' "
            "FROM provenance_links WHERE id = :id"
        ),
        {"id": link["id"]},
    ).one()
    assert row[0] == "object"
    assert row[1] == "false"
    assert isinstance(stored["created_at"], str)


def test_no_provenance_link_names_a_model_on_postgres(repo):
    """The honesty guarantee, asserted against the stored rows."""
    from app.services import provenance as pv

    seed_demo(repo)
    pv.record_job(repo, "JOB-1", "DS-1", job_type="BUILDING_EXTRACTION")
    pv.link_pipeline_output(
        repo,
        stage="BUILDING",
        object_id="XB-1",
        source_id="DS-1",
        processing_job_id="JOB-1",
        parent_id="JOB-1",
        parent_stage="PROCESSING_JOB",
        algorithm="algorithmic_geometric",
    )
    links = repo.records("provenance_links")
    assert links
    assert all(l["model_name"] is None for l in links)
    assert all(l["model_version"] is None for l in links)


def test_record_job_is_idempotent_on_postgres(repo):
    from app.services.provenance import record_job

    seed_demo(repo)
    first = record_job(repo, "JOB-1", "DS-1", job_type="BUILDING_EXTRACTION")
    second = record_job(repo, "JOB-1", "DS-1", job_type="BUILDING_EXTRACTION")
    assert first["id"] == second["id"]
    assert len([l for l in repo.records("provenance_links") if l["object_id"] == "JOB-1"]) == 1


def test_provenance_links_cannot_be_updated_in_postgres(repo):
    from app.services.provenance import create_provenance_record

    seed_demo(repo)
    link = create_provenance_record(
        repo, stage="BUILDING", object_id="XB-1", source_id="DS-1"
    )
    repo.update("provenance_links", link["id"], {"source_id": "DS-FAKED"})
    repo.session.commit()
    after = next(
        l for l in repo.records("provenance_links") if l["id"] == link["id"]
    )
    assert after["source_id"] == "DS-1"


def test_lineage_resolves_on_postgres(repo):
    from app.services.provenance import (
        get_object_lineage,
        link_pipeline_output,
        record_job,
    )

    seed_demo(repo)
    record_job(repo, "JOB-A", "DS-1", job_type="BUILDING_EXTRACTION")
    link_pipeline_output(
        repo,
        stage="BUILDING",
        object_id="XB-1",
        source_id="DS-1",
        processing_job_id="JOB-A",
        parent_id="JOB-A",
        parent_stage="PROCESSING_JOB",
        algorithm="algorithmic_geometric",
    )
    record_job(repo, "JOB-B", "DS-1", job_type="PROPERTY_VOLUME_GENERATION")
    link_pipeline_output(
        repo,
        stage="PROPERTY_VOLUME",
        object_id="GPV-1",
        source_id="DS-1",
        processing_job_id="JOB-B",
        parent_id="XB-1",
        parent_stage="BUILDING",
        algorithm="derived_geometric",
    )
    lineage = get_object_lineage(repo, "GPV-1")
    assert lineage["complete"] is True
    assert lineage["source_id"] == "DS-1"
    assert [n["stage"] for n in lineage["lineage"]][:4] == [
        "DATA_SOURCE",
        "PROCESSING_JOB",
        "BUILDING",
        "PROPERTY_VOLUME",
    ]


def test_an_object_with_no_provenance_reports_none_on_postgres(repo):
    from app.services.provenance import get_object_lineage

    seed_demo(repo)
    # P-001 is the ``object_b`` of the demo's floor-5 finding, so it legitimately
    # has a link; P-404 is touched by nothing.
    lineage = get_object_lineage(repo, "P-404")
    assert lineage["lineage"] == []
    assert lineage["complete"] is False
    assert "rather than inferred" in lineage["note"]


def test_a_seeded_record_is_never_given_a_fabricated_origin(repo):
    """A seeded record was never derived from a survey, so it has no source."""
    from app.services.provenance import get_object_lineage

    seed_demo(repo)
    for object_id in ("P-001", "B-001"):
        lineage = get_object_lineage(repo, object_id)
        assert lineage["complete"] is False
        assert lineage["source_id"] is None
        assert all(node["object_id"] != "DS-001" for node in lineage["lineage"])
