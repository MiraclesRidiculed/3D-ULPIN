"""Engine, session lifecycle, transaction handling and health checks.

Import of this module is side-effect free: no connection is opened until a
session is actually requested, so the in-memory repository keeps working in
environments with no database.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

#: Driver used for every connection. psycopg 3.
DEFAULT_URL = "postgresql+psycopg://vcad:vcad@localhost:5432/vcad"

#: Set by Alembic to force a URL for ``upgrade``/``downgrade``, which run
#: outside a request and so cannot rely on ``VCAD_REPOSITORY`` being set.
_alembic_url_override: str | None = None

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_database_url() -> str | None:
    """Resolve the database URL, or ``None`` when none is configured.

    Set ``VCAD_REPOSITORY=postgres`` to force the PostGIS adapter even without a
    URL; otherwise a URL implies it.
    """
    explicit = os.getenv("VCAD_REPOSITORY", "").strip().lower()
    if explicit in ("memory", "inmemory", "in-memory"):
        return None
    url = (
        _alembic_url_override
        or os.getenv("DATABASE_URL")
        or get_settings().database_url
    )
    if explicit in ("postgres", "postgresql", "postgis"):
        return url or DEFAULT_URL
    return url or None


def set_database_url(url: str | None) -> None:
    """Override the resolved database URL for the current process.

    Used by Alembic, which must target a database regardless of whether the
    application itself is configured to use one.
    """
    global _alembic_url_override
    _alembic_url_override = url


def uses_postgres() -> bool:
    """True when the PostGIS-backed repository should be used."""
    return get_database_url() is not None


def get_engine(url: str | None = None) -> Engine:
    """Return the process-wide engine, creating it on first use.

    ``pool_pre_ping`` guards against connections dropped by the database
    restarting, which otherwise surfaces as a stale-connection error mid-request.
    """
    global _engine
    if _engine is None:
        target = url or get_database_url() or DEFAULT_URL
        connect_args = {}
        if target.startswith("postgresql+psycopg://"):
            # PostGIS needs no special client setup beyond the driver.
            connect_args = {}
        _engine = sa.create_engine(
            target,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
            future=True,
            connect_args=connect_args,
        )
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    """Return the process-wide session factory."""
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(), expire_on_commit=False, future=True
        )
    return _session_factory


def reset_engine() -> None:
    """Dispose of the engine. Used by tests that swap the database URL."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


def get_db_session() -> Iterator[Session]:
    """FastAPI dependency yielding a session with request-scoped transactions.

    Commits when the request succeeds and rolls back if it raises, so a failed
    request never leaves partial writes.
    """
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for scripts, seeding and tests.

    Usage::

        with session_scope() as session:
            session.add(...)
        # committed here; rolled back on exception
    """
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def check_database_health() -> dict:
    """Report connectivity and PostGIS availability.

    Never raises: an unreachable database is a reported condition, not a crash,
    because ``/health`` must still answer.
    """
    result: dict = {
        "status": "unavailable",
        "dialect": None,
        "postgis": None,
        "server_version": None,
        "error": None,
    }
    try:
        engine = get_engine()
        with engine.connect() as conn:
            result["dialect"] = conn.dialect.name
            result["server_version"] = conn.exec_driver_sql("SHOW server_version").scalar()
            installed = conn.execute(
                sa.text(
                    "SELECT count(*) FROM pg_extension WHERE extname = 'postgis'"
                )
            ).scalar()
            result["postgis"] = bool(installed)
            # Confirms we can actually round-trip a spatial value.
            conn.execute(
                sa.text(
                    "SELECT ST_SRID(ST_GeomFromText('POLYGON((0 0,1 0,1 1,0 1,0 0))',4326))"
                )
            ).scalar()
        result["status"] = "ok" if result["postgis"] else "degraded"
    except SQLAlchemyError as exc:
        result["error"] = f"{type(exc).__name__}: {exc}".strip()[:300]
    except Exception as exc:  # noqa: BLE001 - health must never raise
        result["error"] = f"{type(exc).__name__}: {exc}".strip()[:300]
    return result
