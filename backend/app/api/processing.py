"""Processing adapter endpoints.

These expose deterministic stand-ins for building extraction, floor
segmentation and vertical delineation. No model is loaded.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.api.deps import request_repository
from app.repositories.base import CadastreRepository
from app.services import processing

router = APIRouter(tags=["processing"])


@router.post("/processing/building-extraction")
def building_extraction(
    repository: CadastreRepository = Depends(request_repository),
) -> dict[str, Any]:
    return processing.building_extraction(repository)


@router.post("/processing/floor-segmentation")
def floor_segmentation(
    repository: CadastreRepository = Depends(request_repository),
) -> dict[str, Any]:
    return processing.floor_segmentation(repository)


@router.post("/processing/vertical-delineation")
def vertical_delineation(
    repository: CadastreRepository = Depends(request_repository),
) -> dict[str, Any]:
    return processing.vertical_delineation(repository)
