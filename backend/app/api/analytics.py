"""Analytics endpoint."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.api.deps import request_repository
from app.repositories.base import CadastreRepository
from app.services.analytics import summarise

router = APIRouter(tags=["analytics"])


@router.get("/analytics/summary")
def analytics_summary(
    repository: CadastreRepository = Depends(request_repository),
) -> dict[str, Any]:
    return summarise(repository)
