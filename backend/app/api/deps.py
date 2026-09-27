"""Shared API helpers: repository access and error translation."""
from __future__ import annotations

from typing import Any

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_db_session
from app.repositories import get_repository
from app.repositories.base import CadastreRepository, RecordNotFound
from app.services.ingestion import IngestionError

#: Single source of truth for the search miss message.
SEARCH_MISS_DETAIL = "No cadastral object matches this search"


def repo() -> CadastreRepository:
    """Return the ambient repository.

    For use outside a request (startup, scripts). Routers should depend on
    :func:`request_repository`.
    """
    return get_repository()


def request_repository(
    session: Session = Depends(get_db_session),
) -> CadastreRepository:
    """FastAPI dependency yielding the repository for the current request.

    With PostGIS configured this is a repository bound to the request's session,
    so every read and write in one request shares a single transaction: the
    request commits as a unit or rolls back entirely. Without a database it
    returns the in-memory store and behaves exactly as before.
    """
    from app.db.session import uses_postgres

    if not uses_postgres():
        return get_repository()
    from app.repositories.postgres import PostgresCadastreRepository

    return PostgresCadastreRepository(session)


def search_record(repository: CadastreRepository, ident: str) -> dict[str, Any]:
    """Resolve an identifier to a record, or raise a 404.

    Translates the repository's transport-agnostic ``RecordNotFound`` into the
    HTTP error the API has always returned.
    """
    try:
        return repository.search(ident).record
    except RecordNotFound:
        raise HTTPException(404, SEARCH_MISS_DETAIL) from None


def as_http_error(exc: IngestionError) -> HTTPException:
    """Translate an ingestion domain error into an ``HTTPException``."""
    return HTTPException(exc.status_code, exc.detail)
