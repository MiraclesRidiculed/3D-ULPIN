"""Provenance and lineage retrieval.

Read-only: provenance is a record of what already happened, and the collection has
no updatable fields, so there is no route by which a link could be rewritten.

.. warning::
   A lineage that does not begin at a data source reports ``complete: false`` and
   says why. Gaps are never filled in with a plausible parent -- a lineage that
   looks complete and is wrong is worse than one that admits it does not know.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import request_repository
from app.models.enums import STAGE_ORDER, ProvenanceStage
from app.models.schemas import (
    DerivedObjects,
    Lineage,
    ObjectLineage,
    ProcessingHistoryEntry,
    ProvenanceRecord,
)
from app.repositories.base import CadastreRepository
from app.services.provenance import (
    STAGE_COLLECTION,
    get_derived_objects,
    get_object_lineage,
    get_processing_history,
    get_provenance,
    get_source_objects,
)

router = APIRouter(tags=["provenance"])


@router.get("/provenance/stages")
def list_stages() -> dict[str, Any]:
    """The lineage vocabulary, in canonical order.

    Exposed so a UI can render a chain without hard-coding the stage list, and so
    the order is part of the contract rather than an implementation detail.
    """
    return {
        "stages": list(STAGE_ORDER),
        "stage_collections": {
            stage: STAGE_COLLECTION.get(stage) for stage in STAGE_ORDER
        },
        "note": (
            "Not every object traverses every stage. An object with no recorded "
            "provenance reports none rather than an inferred origin."
        ),
    }


@router.get("/provenance/{object_id}", response_model=list[ProvenanceRecord])
def object_provenance(
    object_id: str,
    repository: CadastreRepository = Depends(request_repository),
) -> list[dict[str, Any]]:
    """Every provenance link mentioning an object, in stage order.

    Matches both roles, so an object shows the job that produced it and the things
    it in turn produced.
    """
    return get_provenance(repository, object_id)


@router.get("/provenance/{object_id}/lineage", response_model=ObjectLineage)
def object_lineage(
    object_id: str,
    repository: CadastreRepository = Depends(request_repository),
) -> dict[str, Any]:
    """The chain from a data source to an object, and its descendants.

    ``complete`` is ``False`` when the chain does not begin at a recorded data
    source, which is the honest answer for a hand-entered or seeded record.
    """
    return get_object_lineage(repository, object_id)


@router.get("/provenance/{object_id}/sources")
def object_sources(
    object_id: str,
    repository: CadastreRepository = Depends(request_repository),
) -> dict[str, Any]:
    """The data sources behind an object, walking the whole chain.

    More than one is possible: an object re-surveyed from a second upload has two
    origins, and reporting only the newest would hide the first.
    """
    return {"object_id": object_id, "source_ids": get_source_objects(repository, object_id)}


@router.get("/provenance/{object_id}/processing", response_model=list[ProcessingHistoryEntry])
def object_processing(
    object_id: str,
    repository: CadastreRepository = Depends(request_repository),
) -> list[dict[str, Any]]:
    """The processing jobs that produced an object, oldest first.

    Read from the ``processing_jobs`` rows rather than from the links, so the
    status, detail and method shown are the recorded ones.
    """
    return get_processing_history(repository, object_id)


@router.get("/provenance/source/{source_id}/derived", response_model=DerivedObjects)
def source_derived_objects(
    source_id: str,
    stage: str | None = None,
    repository: CadastreRepository = Depends(request_repository),
) -> dict[str, Any]:
    """Everything one data source produced, grouped by stage.

    Answers "what did this upload actually produce?", which is what a surveyor asks
    when deciding whether to keep or re-upload a file.
    """
    if stage is not None and stage not in {m.value for m in ProvenanceStage}:
        raise HTTPException(
            422,
            f"Unknown stage {stage!r}; expected one of {sorted(STAGE_ORDER)}",
        )
    groups = get_derived_objects(repository, source_id, stage=stage)
    return {
        "source_id": source_id,
        "stages": groups,
        "total_objects": sum(group["count"] for group in groups),
    }


@router.get("/provenance/{object_id}/lineage/stages")
def lineage_stages(
    object_id: str,
    repository: CadastreRepository = Depends(request_repository),
) -> dict[str, Any]:
    """The stages present in one object's lineage, in canonical order.

    Convenience over the full lineage for a UI that only needs the shape of the
    chain, and for a quick check that a stage is missing.
    """
    lineage = get_object_lineage(repository, object_id)
    present = {node["stage"] for node in lineage["lineage"]}
    return {
        "object_id": object_id,
        "stages": [stage for stage in STAGE_ORDER if stage in present],
        "missing_stages": [stage for stage in STAGE_ORDER if stage not in present],
        "complete": lineage["complete"],
    }


__all__ = ["router", "Lineage"]
