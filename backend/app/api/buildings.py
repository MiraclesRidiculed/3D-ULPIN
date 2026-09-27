"""Building endpoints, plus the floor stack for a building."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.api.deps import request_repository, search_record
from app.models.schemas import Building
from app.repositories.base import CadastreRepository
from app.services.demo import (
    BUILDING_BUSINESS_ID,
    FLOOR_COUNT,
    FLOOR_HEIGHT,
)

router = APIRouter(tags=["buildings"])


@router.get("/buildings", response_model=list[Building])
def list_buildings(
    repository: CadastreRepository = Depends(request_repository),
) -> list[dict[str, Any]]:
    return repository.records("buildings")


@router.get("/buildings/{ident}")
def get_building(ident: str, repository: CadastreRepository = Depends(request_repository)) -> dict[str, Any]:
    return search_record(repository, ident)


@router.get("/floors")
def list_floors(
    repository: CadastreRepository = Depends(request_repository),
) -> list[dict[str, Any]]:
    """The demo scene's storey stack, read from the store.

    Reads ``floors`` rather than returning a hardcoded 8 x 3.2 m stack for one
    building, so it cannot describe a scene it has not seen. The ``floors``
    records are vertical bands with no plan geometry, which is why they carry no
    ``confidence``: there is no measurement here to have an opinion about. Storey
    levels derived from point clouds live in ``extracted_floors`` and are measured
    there, not here.
    """
    return [
        {
            "id": f.get("id"),
            "building_id": f.get("building_id"),
            "floor_number": f.get("floor_number"),
            "floor_label": f.get("floor_label"),
            "z_min": f.get("z_min"),
            "z_max": f.get("z_max"),
        }
        for f in sorted(
            repository.records("floors"),
            key=lambda r: (str(r.get("building_id", "")), r.get("floor_number") or 0),
        )
    ]
