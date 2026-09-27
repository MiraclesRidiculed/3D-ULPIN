"""Geometry-version API against PostGIS, for parity with the in-memory store.

``test_postgres_integration.py`` is deliberately repository- and service-level;
it contains no HTTP client. This module covers the missing half: do the new
geometry-version routes answer **the same** over PostGIS as they do in memory?

The service layer already has PostGIS coverage
(``test_geometry_version_history_persists_across_sessions`` and friends). What
this adds is the transport: an ORM round trip, ISO-string timestamps, and a
``geometry`` column coming back as GeoJSON are three places where the two
backends could quietly disagree, and only an HTTP-level test would see it.

Skipped unless a database is configured, and skipped again if the schema is not
migrated. The database must already be at ``alembic upgrade head``; this module
never creates a schema.
"""
from __future__ import annotations

import os

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.db.base import Base
from app.db.session import (
    check_database_health,
    get_engine,
    reset_engine,
    set_database_url,
)
from app.main import app
from app.repositories.postgres import PostgresCadastreRepository
from app.services.demo import seed_demo
from app.services.geometry import create_rectangle, polygon_to_geojson
from app.services.geometry_versioning import create_geometry_version

TEST_URL = (
    os.getenv("VCAD_TEST_DATABASE_URL")
    or os.getenv("ALEMBIC_DATABASE_URL")
    or os.getenv("DATABASE_URL")
)

pytestmark = pytest.mark.skipif(
    not TEST_URL,
    reason="No database configured; set VCAD_TEST_DATABASE_URL to run these",
)

PARCEL = "parcel-001"


@pytest.fixture(scope="module", autouse=True)
def _database():
    set_database_url(TEST_URL)
    reset_engine()
    health = check_database_health()
    if health["status"] != "ok":
        pytest.skip(f"database not usable: {health.get('error')}")
    with get_engine().connect() as conn:
        present = sa.inspect(conn).get_table_names()
    missing = set(Base.metadata.tables) - set(present)
    if missing:
        pytest.skip(f"schema not migrated; run `alembic upgrade head` ({sorted(missing)})")
    yield
    reset_engine()
    set_database_url(None)


@pytest.fixture
def client():
    """A client on a freshly truncated, freshly seeded PostGIS database."""
    from app.db.session import session_scope
    from app.db.models import COLLECTION_MODELS

    with session_scope() as session:
        for kind in (
            "provenance_links",
            "cadastral_changes",
            "review_decisions",
            "review_cases",
            "audit_events",
            "generated_property_volumes",
            "extracted_floors",
            "extracted_buildings",
            "processing_jobs",
            "geometry_versions",
            "issues",
            "sources",
            "properties",
            "floors",
            "infrastructure",
            "buildings",
            "parcels",
        ):
            session.execute(sa.delete(COLLECTION_MODELS[kind]))
        session.flush()
        seed_demo(PostgresCadastreRepository(session))

    with TestClient(app) as test_client:
        yield test_client


def _write_version() -> None:
    """Apply a geometry change through the service, on the request's own store."""
    from app.db.session import session_scope
    from app.repositories.postgres import PostgresCadastreRepository

    with session_scope() as session:
        create_geometry_version(
            PostgresCadastreRepository(session),
            PARCEL,
            geometry=polygon_to_geojson(create_rectangle(0.0, 0.0, 79.0, 52.0)),
            change_reason="re-survey",
        )


# ==========================================================================
# the same four capabilities, over PostGIS
# ==========================================================================


def test_current_version_over_postgres(client):
    body = client.get(f"/geometry-versions/{PARCEL}").json()
    assert body["object_id"] == PARCEL
    assert body["object_type"] == "PARCEL"
    assert body["prototype_ulpin"] == "VC-LP-P001"
    assert body["version"] == 1
    assert body["geometry_hash"]
    assert body["geometry"]["type"] == "Polygon"
    assert body["geometry"]["coordinates"]
    assert body["z_min"] == 0.0 and body["z_max"] == 0.0
    assert body["change_reason"]


def test_history_over_postgres_is_ordered_and_carries_stored_fields(client):
    history = client.get(f"/geometry-versions/{PARCEL}/history").json()
    assert [v["version"] for v in history] == [1]
    assert set(history[0]) == {
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


def test_specific_version_over_postgis(client):
    body = client.get(f"/geometry-versions/{PARCEL}/versions/1").json()
    assert body["version"] == 1
    assert body["geometry"]["coordinates"]


def test_comparison_over_postgres(client):
    _write_version()
    body = client.get(
        f"/geometry-versions/{PARCEL}/compare?from_version=1&to_version=2"
    ).json()
    assert body["from_version"] == 1 and body["to_version"] == 2
    assert body["geometry_changed"] is True
    assert body["delta_area_m2"] < 0
    assert body["prototype_ulpin"] == "VC-LP-P001"
    assert body["ulpin_changed"] is False


# ==========================================================================
# the identity invariant, over PostGIS
# ==========================================================================


def test_ulpin_survives_a_geometry_change_on_postgres(client):
    """The invariant, on the backend where geometry is an ORM round trip."""
    before = client.get(f"/geometry-versions/{PARCEL}").json()
    _write_version()
    after = client.get(f"/geometry-versions/{PARCEL}").json()

    assert after["prototype_ulpin"] == before["prototype_ulpin"] == "VC-LP-P001"
    assert after["geometry_hash"] != before["geometry_hash"]
    assert after["version"] == before["version"] + 1
    # The record read through the ordinary cadastral route agrees.
    assert client.get("/parcels/P-001").json()["prototype_ulpin"] == "VC-LP-P001"


def test_every_version_has_a_distinct_hash_and_one_identifier_on_postgres(client):
    for metres in (79.0, 78.0, 77.0):
        from app.db.session import session_scope
        from app.repositories.postgres import PostgresCadastreRepository

        with session_scope() as session:
            create_geometry_version(
                PostgresCadastreRepository(session),
                PARCEL,
                geometry=polygon_to_geojson(
                    create_rectangle(0.0, 0.0, metres, 52.0)
                ),
                change_reason="re-survey",
            )
    history = client.get(f"/geometry-versions/{PARCEL}/history").json()
    assert [v["version"] for v in history] == [1, 2, 3, 4]
    hashes = [v["geometry_hash"] for v in history]
    assert len(set(hashes)) == 4
    assert client.get(f"/geometry-versions/{PARCEL}").json()["prototype_ulpin"] == (
        "VC-LP-P001"
    )


def test_repeated_versioning_does_not_reissue_an_identifier_on_postgres(client):
    from app.db.session import session_scope
    from app.repositories.postgres import PostgresCadastreRepository

    before = client.get("/geometry-versions/PV-201").json()["prototype_ulpin"]
    for _ in range(4):
        with session_scope() as session:
            create_geometry_version(
                PostgresCadastreRepository(session),
                "PV-201",
                source_id="DS-REPEAT",
                change_reason="reprocess",
            )
        assert (
            client.get("/geometry-versions/PV-201").json()["prototype_ulpin"] == before
        )
    assert [v["version"] for v in client.get("/geometry-versions/PV-201/history").json()] == [1, 2, 3, 4, 5]


# ==========================================================================
# errors behave the same as in memory
# ==========================================================================


def test_nonexistent_object_is_a_404_on_postgres(client):
    r = client.get("/geometry-versions/parcel-does-not-exist")
    assert r.status_code == 404
    assert "no cadastral object" in r.json()["detail"].lower()


def test_nonexistent_version_is_a_404_on_postgres(client):
    r = client.get(f"/geometry-versions/{PARCEL}/versions/99")
    assert r.status_code == 404
    assert "no geometry version 99" in r.json()["detail"].lower()


def test_invalid_comparison_is_a_404_on_postgres(client):
    r = client.get(
        f"/geometry-versions/{PARCEL}/compare?from_version=1&to_version=42"
    )
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")


def test_discovery_over_postgis(client):
    body = client.get("/geometry-versions").json()
    assert set(body["versioned_collections"]) == {
        "parcels",
        "buildings",
        "properties",
        "infrastructure",
    }
    assert body["total_objects"] == 1 + 1 + 17 + 1


def test_timestamps_are_iso_strings_on_both_backends(client):
    """The parity trap this module exists for.

    ``created_at`` is a timestamptz column under PostGIS and a string in memory.
    If the repository did not normalise it, this route would return a ``datetime``
    on one backend and a string on the other -- a shape difference a typecheck
    cannot see and an in-memory test would never catch.
    """
    created = client.get(f"/geometry-versions/{PARCEL}").json()["created_at"]
    assert isinstance(created, str)
    # Parses as an ISO-8601 timestamp.
    from datetime import datetime

    assert datetime.fromisoformat(created).tzinfo is not None
