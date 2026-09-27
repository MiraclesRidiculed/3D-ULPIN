"""V-CAD API — application wiring.

Layering:

* ``app.config``      environment-driven settings
* ``app.db``          SQLAlchemy models, session/transaction handling, PostGIS types
* ``app.models``      Pydantic schemas + domain vocabulary (the API contract)
* ``app.services``    domain logic (geometry, identity, versioning, validation, ...)
* ``app.repositories`` storage: in-memory demo store, or PostGIS when configured
* ``app.api``         HTTP routers, one per resource

Storage selection is by environment:

===============================  ==============================================
no ``DATABASE_URL``              in-memory store (default; offline demo)
``DATABASE_URL=postgresql://``  PostGIS, one transaction per request
===============================  ==============================================

Externally observable behaviour — routes, payload shapes, error messages,
deterministic identifiers and the demo's deliberate validation findings — is the
same on both backends.
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import ROUTERS
from app.config import cached_settings
from app.db.session import check_database_health, uses_postgres
from app.repositories import get_repository, postgres_repository, storage
from app.services.demo import seed_demo

settings = cached_settings()

app = FastAPI(
    title=settings.api_title,
    version=settings.api_version,
    description=settings.api_description,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _bootstrap_repository() -> None:
    """Seed the demo scene when the store is empty.

    With the in-memory store this state is process-global and non-durable, so a
    restart (or any ``uvicorn --reload`` save) re-seeds and discards prior
    changes. With PostGIS the data survives restarts.
    """
    with storage() as repository:
        if not repository.records("parcels"):
            seed_demo(repository)


@app.on_event("startup")
def bootstrap() -> None:
    """Seed the demo scene on first run.

    Non-fatal when the database is unreachable: the API still starts and
    ``/health`` reports the outage, so a database problem is diagnosable rather
    than a crash loop.
    """
    try:
        _bootstrap_repository()
    except Exception as exc:  # noqa: BLE001 - startup must not crash the app
        import logging

        logging.getLogger(__name__).error(
            "startup seed skipped: %s: %s", type(exc).__name__, exc
        )


@app.get("/health")
def health() -> dict[str, Any]:
    """Liveness plus storage detail.

    Always answers 200 so a database outage is reported in the body rather than
    as an unreachable service.
    """
    if uses_postgres():
        database = check_database_health()
        return {
            "status": "ok" if database["status"] == "ok" else "degraded",
            "storage": "postgresql+postgis",
            "database": database,
        }
    return {
        "status": "ok",
        "storage": "local deterministic demo store",
        "database": {"status": "not_configured", "backend": "memory"},
    }


@app.get("/demo/load")
def load_demo() -> dict[str, Any]:
    """Replace the store with the synthetic demo city and revalidate it."""
    with storage() as repository:
        return seed_demo(repository)


for _router in ROUTERS:
    app.include_router(_router)
