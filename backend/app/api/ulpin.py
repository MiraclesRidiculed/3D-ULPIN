"""Prototype 3D ULPIN lookup and issuance.

.. warning::
   Identifiers are a deterministic prototype format created for this demo and
   are **not** official Government of India ULPINs.

An identifier is a stable cadastral identity: it is derived only from
non-geometric attributes and is never recomputed from geometry. Re-surveying a
parcel increments its ``geometry_version`` and changes its ``geometry_hash``
without minting a new ULPIN.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import SEARCH_MISS_DETAIL, request_repository
from app.models.schemas import GenerateRequest, SearchResult
from app.repositories.base import CadastreRepository, RecordNotFound
from app.services.ulpin import assign_ulpins

router = APIRouter(tags=["ulpin"])


@router.get("/ulpin/{ulpin}", response_model=SearchResult)
def lookup_ulpin(ulpin: str, repository: CadastreRepository = Depends(request_repository)) -> SearchResult:
    """Look up any cadastral object by prototype identifier or label.

    Registered before ``/ulpin/generate`` so the historical route precedence is
    preserved.
    """
    try:
        return repository.search(ulpin)
    except RecordNotFound:
        raise HTTPException(404, SEARCH_MISS_DETAIL) from None


@router.post("/ulpin/generate")
def issue_ulpins(
    body: GenerateRequest | None = None,
    repository: CadastreRepository = Depends(request_repository),
) -> dict[str, Any]:
    """Assign stable prototype identifiers to records that lack one.

    Existing identifiers are left alone, so this endpoint can never re-issue a
    live identity. Returns the number of identifiers newly assigned.

    ``parent_parcel_id`` scopes the assignment. The parameter used to be named
    ``_`` and ignored, so a caller narrowing to one parcel silently got a
    repository-wide assignment and a count implying it had been narrowed -- the
    worst kind of contract, since the caller had no way to tell.
    """
    return assign_ulpins(repository, parent_parcel_id=(body.parent_parcel_id if body else None))
