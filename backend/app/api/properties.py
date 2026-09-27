"""Property-volume endpoints."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.api.deps import request_repository, search_record
from app.models.schemas import PropertyVolume
from app.repositories.base import CadastreRepository

router = APIRouter(tags=["properties"])


@router.get("/properties", response_model=list[PropertyVolume])
def list_properties(
    repository: CadastreRepository = Depends(request_repository),
) -> list[dict[str, Any]]:
    return repository.records("properties")


@router.get("/properties/{ident}")
def get_property(ident: str, repository: CadastreRepository = Depends(request_repository)) -> dict[str, Any]:
    return search_record(repository, ident)
