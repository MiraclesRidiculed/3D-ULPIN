"""Validation endpoints.

``/validation/run`` and ``/validation/issues`` keep their original behaviour
exactly. The rest of this module exposes the rule engine underneath, so a caller
can ask which rules exist, run one in isolation, or summarise a run.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import request_repository
from app.models.schemas import ValidationIssue
from app.repositories.base import CadastreRepository
from app.services.validation import validate
from app.services.validation_engine import (
    get_rule_definitions,
    get_validation_summary,
    run_rule,
)

router = APIRouter(tags=["validation"])


@router.post("/validation/run")
def run_validation(
    repository: CadastreRepository = Depends(request_repository),
) -> dict[str, Any]:
    """Recompute all findings from scratch and replace the stored set."""
    return validate(repository)


@router.get("/validation/issues", response_model=list[ValidationIssue])
def list_issues(
    repository: CadastreRepository = Depends(request_repository),
) -> list[dict[str, Any]]:
    return repository.records("issues")


@router.get("/validation/rules")
def list_rules() -> list[dict[str, Any]]:
    """Every registered rule: id, category, issue type, severity and description.

    Does not touch the store, so it is safe to call before anything is seeded.
    """
    return get_rule_definitions()


@router.post("/validation/rules/{rule_id}/run")
def run_single_rule(
    rule_id: str,
    repository: CadastreRepository = Depends(request_repository),
) -> dict[str, Any]:
    """Run one rule in isolation, without touching the stored findings.

    For explaining a finding or reproducing one rule's behaviour without the rest
    of the engine running around it.
    """
    try:
        results = run_rule(repository, rule_id)
    except KeyError:
        raise HTTPException(404, f"Unknown validation rule {rule_id!r}") from None
    return {
        "rule_id": rule_id,
        "findings": [result.to_issue_record() for result in results],
        "count": len(results),
    }


@router.get("/validation/summary")
def summarise(
    repository: CadastreRepository = Depends(request_repository),
) -> dict[str, Any]:
    """Counts of the stored findings by severity, rule and category."""
    return get_validation_summary(repository)
