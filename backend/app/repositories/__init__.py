"""Repository implementations and the process-wide repository accessor.

Two backends implement :class:`CadastreRepository`:

``InMemoryRepository``
    Development/demo store. Non-durable, wiped on restart, no dependencies.

``PostgresCadastreRepository``
    PostGIS-backed. Selected automatically when ``DATABASE_URL`` is set, or when
    ``VCAD_REPOSITORY=postgres``.

Both are held to the same contract and return identical record shapes, so
services, routers and tests are unaware of which is active.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from app.repositories.base import (
    COLLECTIONS,
    SEARCH_ORDER,
    SEARCHABLE_COLLECTIONS,
    CadastreRepository,
    RecordNotFound,
)
from app.repositories.memory import InMemoryRepository

log = logging.getLogger(__name__)

#: Process-wide repository used when no session is supplied.
_repository: CadastreRepository = InMemoryRepository()


def get_repository() -> CadastreRepository:
    """Return the ambient repository.

    Defaults to the in-memory store. Request handlers should instead depend on
    :func:`app.api.deps.request_repository`, which returns a PostGIS repository
    sharing the request's transaction when PostGIS is configured.
    """
    return _repository


def set_repository(repository: CadastreRepository) -> None:
    """Swap the ambient repository (tests, scripts)."""
    global _repository
    _repository = repository


@contextmanager
def postgres_repository() -> Iterator[CadastreRepository]:
    """Yield a PostGIS repository bound to a committed session."""
    from app.db.session import session_scope
    from app.repositories.postgres import PostgresCadastreRepository

    with session_scope() as session:
        yield PostgresCadastreRepository(session)


@contextmanager
def storage() -> Iterator[CadastreRepository]:
    """Yield the repository matching the current configuration.

    For startup work, scripts and tests. Commits on clean exit and rolls back on
    error, so a failed seed cannot half-apply.
    """
    from app.db.session import uses_postgres

    if uses_postgres():
        with postgres_repository() as repository:
            yield repository
        return
    yield get_repository()


__all__ = [
    "COLLECTIONS",
    "SEARCH_ORDER",
    "SEARCHABLE_COLLECTIONS",
    "CadastreRepository",
    "InMemoryRepository",
    "RecordNotFound",
    "get_repository",
    "postgres_repository",
    "set_repository",
    "storage",
]
