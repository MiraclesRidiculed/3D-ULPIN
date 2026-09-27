"""Underground infrastructure endpoints."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.api.deps import request_repository
from app.models.schemas import Infrastructure
from app.repositories.base import CadastreRepository

router = APIRouter(tags=["infrastructure"])


@router.get("/infrastructure", response_model=list[Infrastructure])
def list_infrastructure(
    repository: CadastreRepository = Depends(request_repository),
) -> list[dict[str, Any]]:
    return repository.records("infrastructure")
