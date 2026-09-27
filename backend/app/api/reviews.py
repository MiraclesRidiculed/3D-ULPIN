"""Human review of validation findings.

The review workflow, exposed over HTTP. The workflow itself lives in
:mod:`app.services.reviews`; this module only translates transport.

.. warning::
   **There is no authentication.** The ``reviewer`` recorded by every decision is
   whatever the caller supplied, either in the request body or in the
   ``X-Reviewer`` header. It is an unverified assertion, not an identity. Do not
   present a decision's reviewer as proof of who made it.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException

from app.api.deps import request_repository
from app.models.schemas import (
    AssignRequest,
    CloseRequest,
    DecisionRequest,
    ReviewCase,
    ReviewDecision,
    ReviewRequest,
)
from app.repositories.base import CadastreRepository
from app.services.audit import ACTOR_HEADER
from app.services.reviews import (
    IssueNotFound,
    ReviewError,
    approve_review,
    assign_review_case,
    close_validation_issue,
    create_review_case,
    get_pending_reviews,
    get_review_case,
    get_review_decisions,
    mark_issue_expected,
    reopen_validation_issue,
    reject_review,
    request_resurvey,
)

router = APIRouter(tags=["reviews"])


def _actor(reviewer: str | None, header: str | None) -> str | None:
    """The self-asserted actor: explicit reviewer, else header, else default."""
    return reviewer or header


def _fail(exc: Exception) -> HTTPException:
    """Translate a domain error into the right status code.

    A missing issue or case is a 404; an illegal transition is a 409, because
    the request was well-formed but conflicts with the current state.
    """
    if isinstance(exc, IssueNotFound):
        return HTTPException(404, str(exc))
    if isinstance(exc, ReviewError):
        return HTTPException(409, str(exc))
    return HTTPException(400, str(exc))


@router.get("/reviews", response_model=list[ReviewCase])
def list_pending_reviews(
    reviewer: str | None = None,
    repository: CadastreRepository = Depends(request_repository),
) -> list[dict[str, Any]]:
    """Cases still awaiting a decision, most severe first.

    Decided and closed cases are not pending, so they do not appear here; query
    an issue directly to see where it ended up.
    """
    return get_pending_reviews(repository, reviewer=reviewer)


@router.post("/reviews", response_model=ReviewCase, status_code=201)
def open_review(
    issue_id: str,
    body: ReviewRequest | None = None,
    repository: CadastreRepository = Depends(request_repository),
    reviewer: str | None = Header(None, alias=ACTOR_HEADER),
) -> dict[str, Any]:
    """Open a review case for a finding.

    Idempotent by design: a second call for the same finding returns the case
    that already exists rather than opening a competing one.
    """
    payload = body or ReviewRequest()
    try:
        return create_review_case(
            repository,
            issue_id,
            reason=payload.reason,
            reviewer=payload.reviewer or reviewer,
            priority=payload.priority,
            actor=_actor(payload.reviewer, reviewer),
        )
    except (IssueNotFound, ReviewError) as exc:
        raise _fail(exc) from None


@router.get("/reviews/{ident}", response_model=ReviewCase)
def read_review(
    ident: str,
    repository: CadastreRepository = Depends(request_repository),
) -> dict[str, Any]:
    """One review case, by case id or by the issue it reviews."""
    case = get_review_case(repository, ident)
    if case is None:
        raise HTTPException(404, f"No review case for {ident!r}")
    return case


@router.get("/reviews/{ident}/decisions", response_model=list[ReviewDecision])
def read_review_decisions(
    ident: str,
    repository: CadastreRepository = Depends(request_repository),
) -> list[dict[str, Any]]:
    """Every decision on a case, oldest first.

    Survives a reopen: the rows are immutable, so the reason a case was closed
    is still readable after it goes back to ``PENDING``.
    """
    try:
        return get_review_decisions(repository, ident)
    except ReviewError as exc:
        raise _fail(exc) from None


@router.post("/reviews/{ident}/assign", response_model=ReviewCase)
def assign_review(
    ident: str,
    body: AssignRequest,
    repository: CadastreRepository = Depends(request_repository),
    reviewer: str | None = Header(None, alias=ACTOR_HEADER),
) -> dict[str, Any]:
    """Assign a case to a reviewer, or hand it to somebody else."""
    try:
        return assign_review_case(
            repository, ident, body.reviewer, reason=body.reason, actor=reviewer
        )
    except (IssueNotFound, ReviewError) as exc:
        raise _fail(exc) from None


def _decide(
    repository: CadastreRepository,
    ident: str,
    body: DecisionRequest,
    action,
    reviewer: str | None,
) -> dict[str, Any]:
    try:
        return action(
            repository,
            ident,
            reason=body.reason,
            reviewer=body.reviewer or reviewer,
            actor=body.reviewer or reviewer,
        )
    except (IssueNotFound, ReviewError) as exc:
        raise _fail(exc) from None


@router.post("/reviews/{ident}/approve", response_model=ReviewCase)
def approve(
    ident: str,
    body: DecisionRequest,
    repository: CadastreRepository = Depends(request_repository),
    reviewer: str | None = Header(None, alias=ACTOR_HEADER),
) -> dict[str, Any]:
    """Confirm the finding is real and correct. ``reason`` is required."""
    return _decide(repository, ident, body, approve_review, reviewer)


@router.post("/reviews/{ident}/reject", response_model=ReviewCase)
def reject(
    ident: str,
    body: DecisionRequest,
    repository: CadastreRepository = Depends(request_repository),
    reviewer: str | None = Header(None, alias=ACTOR_HEADER),
) -> dict[str, Any]:
    """Dismiss the finding as a false positive. ``reason`` is required."""
    return _decide(repository, ident, body, reject_review, reviewer)


@router.post("/reviews/{ident}/request-resurvey", response_model=ReviewCase)
def resurvey(
    ident: str,
    body: DecisionRequest,
    repository: CadastreRepository = Depends(request_repository),
    reviewer: str | None = Header(None, alias=ACTOR_HEADER),
) -> dict[str, Any]:
    """The finding is real, the geometry is wrong: re-measure the object."""
    return _decide(repository, ident, body, request_resurvey, reviewer)


@router.post("/reviews/{ident}/mark-expected", response_model=ReviewCase)
def mark_expected(
    ident: str,
    body: DecisionRequest,
    repository: CadastreRepository = Depends(request_repository),
    reviewer: str | None = Header(None, alias=ACTOR_HEADER),
) -> dict[str, Any]:
    """Record the condition as known and accepted, so it stops being a defect."""
    return _decide(repository, ident, body, mark_issue_expected, reviewer)


@router.post("/reviews/issues/{issue_id}/close", response_model=ReviewCase)
def close_issue(
    issue_id: str,
    body: CloseRequest,
    repository: CadastreRepository = Depends(request_repository),
    reviewer: str | None = Header(None, alias=ACTOR_HEADER),
) -> dict[str, Any]:
    """Close a finding. The issue is marked closed, never deleted."""
    try:
        return close_validation_issue(
            repository,
            issue_id,
            reason=body.reason,
            reviewer=body.reviewer or reviewer,
            actor=body.reviewer or reviewer,
        )
    except (IssueNotFound, ReviewError) as exc:
        raise _fail(exc) from None


@router.post("/reviews/issues/{issue_id}/reopen", response_model=ReviewCase)
def reopen_issue(
    issue_id: str,
    body: CloseRequest,
    repository: CadastreRepository = Depends(request_repository),
    reviewer: str | None = Header(None, alias=ACTOR_HEADER),
) -> dict[str, Any]:
    """Reopen a closed finding, keeping its full decision history."""
    try:
        return reopen_validation_issue(
            repository,
            issue_id,
            reason=body.reason,
            reviewer=body.reviewer or reviewer,
            actor=body.reviewer or reviewer,
        )
    except (IssueNotFound, ReviewError) as exc:
        raise _fail(exc) from None
