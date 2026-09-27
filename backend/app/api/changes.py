"""Cadastral change detection over HTTP.

Compares the approved cadastre against a new survey and reports the differences.

.. warning::
   A reported change is a **measured difference between two geometries**. This
   API never asserts that a change was illegal, unauthorised or an encroachment:
   it did not observe who altered what, or whether they were entitled to. Every
   record comes back with ``status="REQUIRES_VERIFICATION"`` and the message
   "Change detected — requires verification." Resolving that status is a human
   decision, made through the review workflow in :mod:`app.api.reviews`.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import request_repository
from app.models.enums import ChangeType
from app.models.schemas import (
    ChangeRecord,
    ChangeSummary,
    CompareRequest,
)
from app.repositories.base import CadastreRepository
from app.services.audit import ACTOR_HEADER
from app.services.change_detection import (
    ChangeDetectionError,
    ChangeReport,
    compare_approved_vs_survey,
    generate_change_report,
    survey_geometry,
)

router = APIRouter(tags=["changes"])


@router.get("/changes", response_model=list[ChangeRecord])
def list_changes(
    source_id: str | None = None,
    change_type: str | None = None,
    object_id: str | None = None,
    repository: CadastreRepository = Depends(request_repository),
) -> list[dict[str, Any]]:
    """Changes recorded for a survey, or across all surveys.

    Reads what was detected rather than re-running a comparison: a stored change
    is a record of a specific survey against a specific baseline, and recomputing
    it would report whatever the cadastre looks like now.
    """
    valid = {member.value for member in ChangeType}
    if change_type is not None and change_type not in valid:
        raise HTTPException(
            422, f"Unknown change type {change_type!r}; expected one of {sorted(valid)}"
        )
    records = [
        change
        for change in repository.records("cadastral_changes")
        if (source_id is None or change.get("source_id") == source_id)
        and (change_type is None or change.get("change_type") == change_type)
        and (object_id is None or change.get("object_id") == object_id)
    ]
    return [
        ChangeRecord.from_record(change).model_dump() for change in records
    ]


@router.get("/changes/report", response_model=ChangeSummary)
def change_report(
    source_id: str | None = None,
    limit: int = Query(500, ge=1, le=2000),
    repository: CadastreRepository = Depends(request_repository),
) -> dict[str, Any]:
    """Summarise the changes on record.

    ``all_require_verification`` is reported explicitly so a consumer can see at a
    glance that nothing has been resolved, rather than having to check each row.
    """
    return generate_change_report(repository, source_id=source_id, limit=limit)


@router.post("/changes/compare", response_model=ChangeSummary)
def compare(
    body: CompareRequest,
    repository: CadastreRepository = Depends(request_repository),
    reviewer: str | None = Query(None, alias=ACTOR_HEADER),
) -> dict[str, Any]:
    """Compare the approved cadastre with a new survey and record the differences.

    Omit ``approved`` to use the repository's own records as the approved side --
    the usual case of re-surveying a known parcel. Supply it to compare against a
    specific earlier baseline instead.

    Each detected change is stored, filed as an audit event, and opened as a
    review case. A storey that the approved cadastre does not have additionally
    produces an ``UNREGISTERED_FLOOR`` finding.

    The comparison does not modify any cadastral geometry: a re-survey supersedes
    rather than replaces, so what was approved stays readable after a change is
    found.
    """
    approved = body.approved
    if approved is None:
        approved = [
            record
            for kind in ("properties", "buildings", "parcels")
            for record in repository.records(kind)
        ]
    try:
        report = compare_approved_vs_survey(
            approved,
            body.survey,
            source_id=body.source_id,
            repository=repository,
            actor=body.actor or reviewer,
            tolerance=body.tolerance,
        )
    except ChangeDetectionError as exc:
        raise HTTPException(422, str(exc)) from None
    return _summary(report)


@router.get("/changes/compare/repository", response_model=ChangeSummary)
def compare_against_store(
    source_id: str,
    tolerance: float = 0.01,
    repository: CadastreRepository = Depends(request_repository),
    reviewer: str | None = Query(None, alias=ACTOR_HEADER),
) -> dict[str, Any]:
    """Report how a survey's own header compares to the store.

    A convenience for the common case where a survey's point cloud or plan has
    already been ingested: compares the ingested source's recorded extent with
    the approved cadastre, without the caller having to restate the baseline.
    """
    source = next(
        (s for s in repository.records("sources") if s["id"] == source_id), None
    )
    if source is None:
        raise HTTPException(404, f"No data source {source_id!r}")
    bounds = ((source.get("metadata") or {}).get("display_bounds")) or (
        source.get("bounds")
    )
    if not bounds:
        raise HTTPException(
            422,
            f"Source {source_id} records no extent, so it cannot be compared "
            "against the cadastre. POST /changes/compare with explicit geometry.",
        )
    approved = [
        record
        for kind in ("properties", "buildings", "parcels")
        for record in repository.records(kind)
    ]
    report = compare_approved_vs_survey(
        approved,
        [survey_geometry({"id": source_id, "geometry": bounds})],
        source_id=source_id,
        repository=repository,
        actor=reviewer,
        tolerance=tolerance,
    )
    return _summary(report)


def _summary(report: ChangeReport) -> dict[str, Any]:
    body = report.summary()
    body["changes"] = [ChangeRecord.from_record(c).model_dump() for c in report.changes]
    return body
