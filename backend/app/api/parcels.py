"""Parcel endpoints."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.api.deps import request_repository, search_record
from app.models.schemas import Parcel
from app.repositories.base import CadastreRepository

router = APIRouter(tags=["parcels"])


@router.get("/parcels", response_model=list[Parcel])
def list_parcels(repository: CadastreRepository = Depends(request_repository)) -> list[dict[str, Any]]:
    return repository.records("parcels")


@router.get("/parcels/{ident}")
def get_parcel(ident: str, repository: CadastreRepository = Depends(request_repository)) -> dict[str, Any]:
    return search_record(repository, ident)
