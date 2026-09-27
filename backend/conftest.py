"""Pytest configuration.

Ensures the ``app`` package is importable and gives every test a clean
repository so state cannot leak between tests.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from app.repositories import set_repository  # noqa: E402
from app.repositories.memory import InMemoryRepository  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_repository():
    """Reset the process-wide repository around every test."""
    set_repository(InMemoryRepository())
    yield
    set_repository(InMemoryRepository())


@pytest.fixture
def client():
    """FastAPI test client on a clean store.

    Entering the context runs the startup seed. With PostGIS configured the
    database is not reset between tests the way the in-memory store is, so it is
    truncated and re-seeded here — otherwise one test's uploads leak into the next
    and the two backends stop behaving identically.
    """
    from fastapi.testclient import TestClient

    from app.main import app

    _reset_persistent_store()

    with TestClient(app) as test_client:
        yield test_client


def _reset_persistent_store() -> None:
    """Truncate and re-seed, but only when a database is actually in use."""
    from app.db.models import COLLECTION_MODELS
    from app.db.session import session_scope, uses_postgres

    if not uses_postgres():
        return

    import sqlalchemy as sa

    from app.services.demo import seed_demo
    from app.repositories.postgres import PostgresCadastreRepository

    with session_scope() as session:
        repository = PostgresCadastreRepository(session)
        # Children before parents, so foreign keys stay satisfied. This list is
        # explicit and must name EVERY collection: a collection missing here
        # survives between tests, so one test's review decisions would be visible
        # to the next and the two backends would stop behaving identically.
        for kind in (
            "provenance_links",
            "cadastral_changes",
            "review_decisions"
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
        seed_demo(repository)


@pytest.fixture(scope="session")
def _point_cloud_fixtures(tmp_path_factory):
    from tests.fixtures.point_clouds import build_fixtures

    return build_fixtures(tmp_path_factory.mktemp("pc-api"))


@pytest.fixture
def point_cloud_upload(_point_cloud_fixtures):
    """Return a callable producing upload bytes for a given extension.

    Lets API tests exercise the real ingestion pipeline without each test
    rebuilding fixtures.
    """

    def _read(ext: str) -> bytes:
        return _point_cloud_fixtures[ext].read_bytes()

    return _read
