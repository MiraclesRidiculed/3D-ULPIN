"""Human verification of validation findings.

The validation engine files findings; it cannot decide whether they are real. A
building that overhangs a parcel boundary may be a genuine encroachment or a
known and accepted situation, and only a person who can see the site can say
which. This module is that person, recorded.

The workflow
------------
::

    ISSUE ──▶ REVIEW ──┬──▶ APPROVED             finding confirmed
                        ├──▶ REJECTED             finding dismissed (false positive)
                        ├──▶ REQUEST_RESURVEY     geometry is wrong; re-measure
                        ├──▶ MARK_EXPECTED        known condition, not a defect
                        └──▶ (close / reopen)     finish, or start again

Three collections, on purpose
-----------------------------
=========================  ====================================================
``review_cases``           Mutable current state. One per issue. What a queue
                           screen reads.
``review_decisions``       Immutable history. One per decision, never updated.
                           Why the log cannot be rewritten.
``audit_events``           The cross-cutting trail (see
                           :mod:`app.services.audit`). A decision appears here
                           too, as a summary pointing back at the decision row.
=========================  ====================================================

Keeping decisions in their own append-only collection rather than a list on the
case is the same reasoning as ``geometry_versions`` beside the geometry-bearing
records: current state is mutable, history is not, and conflating them means a
later decision can quietly rewrite an earlier one.

``RESURVEY_REQUESTED`` is not a flavour of ``REJECTED``
--------------------------------------------------------
Rejecting says the *finding* is wrong. Requesting a re-survey says the *geometry*
is wrong. Only the second sends someone out with a theodolite, so the two are
kept apart.

The reviewer is not authenticated
---------------------------------
.. warning::
   Authentication is out of scope for this milestone. Every decision records the
   reviewer string it was given, which is **self-asserted** and unverified. It is
   recorded because a decision with no named actor cannot be audited, but it is
   not proof of identity. See :data:`app.services.audit.DEVELOPMENT_ACTOR`.

Re-running validation does not erase a review
---------------------------------------------
:func:`app.services.validation.validate` clears and rebuilds the issues
collection. A reviewer who approved a finding must not silently lose that
approval because somebody pressed the button again, so
:func:`restore_review_outcomes` carries decided cases forward onto the
rebuilt issues.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Mapping

from app.models.enums import (
    DECIDABLE_STATES,
    AuditAction,
    ReviewState,
    ProvenanceStage,
)
from app.repositories.base import CadastreRepository
from app.services.audit import create_audit_event
from app.utils import now


class ReviewError(RuntimeError):
    """Raised when a review transition is not legal from the current state."""


class IssueNotFound(LookupError):
    """Raised when a review refers to an issue that does not exist."""


#: Reasons are mandatory on every decision. A bare approval with no stated
#: justification is the single most common way an audit trail becomes worthless,
#: so the workflow refuses to record one.
REASON_REQUIRED = "a reason is required for every review decision"


# --------------------------------------------------------------------------
# internal helpers
# --------------------------------------------------------------------------


def _time_prefixed_id(prefix: str) -> str:
    """Id whose leading digits are the creation time in microseconds.

    Gives the append-only histories a *total* order that agrees with their
    timestamps. Sorting on the timestamp alone leaves same-tick events tied, and
    a tie broken on a random suffix yields an arbitrary order that can differ
    between two reads of the same data -- which would make a decision log
    unreadable.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    return f"{prefix}-{stamp}-{uuid.uuid4().hex[:8]}"


def _case_id() -> str:
    return _time_prefixed_id("RC")


def _decision_id() -> str:
    return _time_prefixed_id("RD")


def _issue(repo: CadastreRepository, issue_id: str) -> dict[str, Any]:
    for issue in repo.records("issues"):
        if issue.get("id") == issue_id:
            return issue
    raise IssueNotFound(f"No validation issue {issue_id!r}")


def _case_record(repo: CadastreRepository, case_id: str) -> dict[str, Any] | None:
    for case in repo.records("review_cases"):
        if case.get("id") == case_id:
            return case
    return None


def _case_for_issue(
    repo: CadastreRepository, issue_id: str
) -> dict[str, Any] | None:
    for case in repo.records("review_cases"):
        if case.get("issue_id") == issue_id:
            return case
    return None


def resolve_case(repo: CadastreRepository, ident: str) -> dict[str, Any]:
    """Find a review case by its own id, or by the issue it reviews.

    The workflow's function names are inconsistently case-centric
    (``approve_review``) and issue-centric (``mark_issue_expected``), so every
    entry point accepts either identifier rather than forcing the caller to know
    which one a given function wants.
    """
    case = _case_record(repo, ident)
    if case is not None:
        return case
    case = _case_for_issue(repo, ident)
    if case is not None:
        return case
    raise ReviewError(
        f"No review case for {ident!r}. Create one with create_review_case()."
    )


def _affected_object(issue: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """``(primary object, related object)`` an issue is about.

    Recorded explicitly rather than being re-derived later: an audit entry that
    only says "the issue" cannot be followed to the parcel it concerns once the
    issue has been rebuilt by a later validation run.
    """
    return issue.get("object_a"), issue.get("object_b")


def _object_type_for(issue: Mapping[str, Any]) -> str | None:
    """Best-effort record-kind label for the issue's primary object.

    Derived from the id prefix, which is the only thing available: a validation
    finding records *which* objects it concerns, not what kind they are.
    """
    primary = issue.get("object_a") or ""
    if not primary:
        return None
    if primary.startswith("PV-"):
        return "PROPERTY_VOLUME"
    if primary.startswith("B-"):
        return "BUILDING"
    if primary.startswith("P-"):
        return "PARCEL"
    if primary.startswith("INF-"):
        return "INFRASTRUCTURE"
    return "UNKNOWN"


def _set_issue_status(
    repo: CadastreRepository, issue_id: str, status: str
) -> dict[str, Any]:
    """Project the review state onto the issue record.

    The review case is the single source of truth; the issue status is kept in
    step so existing consumers of ``issues[].status`` see the outcome without
    having to join to the review workflow.
    """
    updated = repo.update("issues", issue_id, {"status": status})
    if updated is None:  # pragma: no cover - guarded by callers
        raise IssueNotFound(issue_id)
    return updated


# --------------------------------------------------------------------------
# review cases
# --------------------------------------------------------------------------


def create_review_case(
    repo: CadastreRepository,
    issue_id: str,
    *,
    reason: str | None = None,
    reviewer: str | None = None,
    priority: str | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Open a review case for a validation finding.

    One case per issue: a second call returns the existing case rather than
    opening a duplicate, because two open cases on one finding means two people
    can decide it and the audit trail records a contradiction.
    """
    issue = _issue(repo, issue_id)
    existing = _case_for_issue(repo, issue_id)
    if existing is not None:
        return existing

    state = (
        ReviewState.IN_REVIEW.value
        if reviewer
        else ReviewState.PENDING.value
    )
    case = repo.add(
        "review_cases",
        {
            "id": _case_id(),
            "issue_id": issue_id,
            "issue_type": issue.get("issue_type"),
            "severity": issue.get("severity"),
            "object_a": issue.get("object_a"),
            "object_b": issue.get("object_b"),
            "object_type": _object_type_for(issue),
            "state": state,
            "assigned_to": reviewer,
            "priority": priority,
            "reason": reason,
            "created_by": actor,
            "created_at": now(),
            "updated_at": now(),
            "decided_at": None,
            "decided_by": None,
            "decision": None,
            "decision_reason": None,
            "review_count": 0,
        },
    )
    _record_review_provenance(repo, case, issue)
    if reviewer:
        create_audit_event(
            repo,
            action=AuditAction.REVIEW_ASSIGNED.value,
            object_id=issue.get("object_a"),
            object_type=case["object_type"],
            actor=actor or reviewer,
            detail=f"Review of {issue_id} assigned to {reviewer}",
            issue_id=issue_id,
            review_case_id=case["id"],
            previous_state=ReviewState.OPEN.value,
            new_state=state,
        )
    else:
        create_audit_event(
            repo,
            action=AuditAction.REVIEW_REQUESTED.value,
            object_id=issue.get("object_a"),
            object_type=case["object_type"],
            actor=actor,
            detail=f"Human review requested for {issue_id}",
            issue_id=issue_id,
            review_case_id=case["id"],
            previous_state=ReviewState.OPEN.value,
            new_state=state,
        )
    _set_issue_status(repo, issue_id, state)
    return case


def _record_review_provenance(
    repo: CadastreRepository, case: dict[str, Any], issue: dict[str, Any]
) -> None:
    """Put the REVIEW stage on the lineage, inheriting the finding's origin.

    A human looking at a finding is the last stage of the chain, so the link
    carries the finding's source. When the finding has no recorded source -- it
    is about a cadastral record that was never derived from a survey -- the link
    records none either. That is the honest answer, and it is what lets
    ``get_object_lineage`` report ``complete: false`` rather than inventing a
    parent.
    """
    from app.services import provenance
    from app.services.provenance import get_source_objects

    about = issue.get("object_a")
    source_id = next(iter(get_source_objects(repo, issue["id"])), None) if about else None
    provenance.create_provenance_record(
        repo,
        stage=ProvenanceStage.REVIEW.value,
        object_id=case["id"],
        source_id=source_id,
        parent_id=issue["id"],
        parent_stage=ProvenanceStage.VALIDATION.value,
        algorithm="human-review",
        parameters={"issue_id": issue["id"], "about_object": about},
    )


def get_review_case(
    repo: CadastreRepository, ident: str
) -> dict[str, Any] | None:
    """One review case by case id or issue id; ``None`` when there is none.

    A lookup, not a getter that raises, so a caller can ask "is this under
    review?" without handling an exception as control flow.
    """
    return _case_record(repo, ident) or _case_for_issue(repo, ident)


def _severity_rank(severity: Any) -> int:
    """Sort rank for a review queue: most severe first.

    Severity arrives as an ``IssueSeverity`` *member*, not a plain string, because
    the validation engine stores the enum. ``str()`` on a ``str``-Enum under
    Python 3.11+ yields ``"IssueSeverity.WARNING"`` rather than ``"WARNING"``, so
    a naive dict lookup would rank every finding as unknown and silently sort the
    queue by creation time instead.
    """
    value = getattr(severity, "value", severity)
    return {"CRITICAL": 0, "WARNING": 1, "INFO": 2}.get(str(value), 3)


def get_pending_reviews(
    repo: CadastreRepository, *, reviewer: str | None = None
) -> list[dict[str, Any]]:
    """Cases still awaiting a decision, most severe first.

    Severity order is the reviewer's actual queue order: a CRITICAL overlap and
    an INFO change note are not equally urgent. Unassigned cases come first
    within a severity, since they are the ones at risk of being missed.
    """
    pending = [
        case
        for case in repo.records("review_cases")
        if case.get("state") in DECIDABLE_STATES
    ]
    if reviewer is not None:
        pending = [c for c in pending if c.get("assigned_to") == reviewer]
    return sorted(
        pending,
        key=lambda c: (
            _severity_rank(c.get("severity")),
            0 if not c.get("assigned_to") else 1,
            str(c.get("created_at") or ""),
        ),
    )


def assign_review_case(
    repo: CadastreRepository,
    ident: str,
    reviewer: str,
    *,
    reason: str | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Assign a case to a reviewer, or move it between reviewers.

    Reassignment is allowed while undecided, and the previous holder is kept on
    the case so the trail shows a handover rather than a silent replacement.
    """
    if not reviewer or not reviewer.strip():
        raise ReviewError("a reviewer is required to assign a case")
    case = resolve_case(repo, ident)
    if case.get("state") not in DECIDABLE_STATES:
        raise ReviewError(
            f"case {case['id']} is {case.get('state')} and can no longer be assigned; "
            "reopen the issue to review it again"
        )

    previous_assignee = case.get("assigned_to")
    previous_state = case.get("state")
    changes: dict[str, Any] = {
        "assigned_to": reviewer,
        "state": ReviewState.IN_REVIEW.value,
        "updated_at": now(),
    }
    if reason:
        changes["reason"] = reason
    repo.update("review_cases", case["id"], changes)
    case = _case_record(repo, case["id"]) or case

    create_audit_event(
        repo,
        action=AuditAction.REVIEW_ASSIGNED.value,
        object_id=case.get("object_a"),
        object_type=case.get("object_type"),
        actor=actor or reviewer,
        detail=(
            f"Review of {case.get('issue_id')} assigned to {reviewer}"
            + (f" (was {previous_assignee})" if previous_assignee else "")
        ),
        issue_id=case.get("issue_id"),
        review_case_id=case["id"],
        previous_state=previous_state,
        new_state=ReviewState.IN_REVIEW.value,
        extra={"previous_assignee": previous_assignee},
    )
    if case.get("issue_id"):
        _set_issue_status(repo, case["issue_id"], ReviewState.IN_REVIEW.value)
    return case


# --------------------------------------------------------------------------
# decisions
# --------------------------------------------------------------------------


def _decide(
    repo: CadastreRepository,
    ident: str,
    state: ReviewState,
    action: AuditAction,
    *,
    reason: str,
    reviewer: str | None,
    actor: str | None,
) -> dict[str, Any]:
    """Shared body of the four decision functions.

    Every decision does the same four things -- validate, snapshot the previous
    state, append an immutable decision row, audit it -- so they share one
    implementation rather than four near-copies that can drift apart.
    """
    if not reason or not reason.strip():
        raise ReviewError(REASON_REQUIRED)

    case = resolve_case(repo, ident)
    if case.get("state") not in DECIDABLE_STATES:
        raise ReviewError(
            f"case {case['id']} is already {case.get('state')}; a review is decided "
            "once. Reopen the issue to revisit it."
        )
    issue_id = case.get("issue_id")
    issue = _issue(repo, issue_id) if issue_id else {}
    primary, related = _affected_object(issue) or (None, None)
    previous_state = case.get("state")
    decided_at = now()

    decision = repo.add(
        "review_decisions",
        {
            "id": _decision_id(),
            "review_case_id": case["id"],
            "issue_id": issue_id,
            "decision": state.value,
            "action": action.value,
            "reviewer": reviewer or actor or None,
            "reason": reason,
            "object_id": primary,
            "related_object_id": related,
            "object_type": case.get("object_type"),
            "previous_state": previous_state,
            "new_state": state.value,
            "decided_at": decided_at,
        },
    )

    repo.update(
        "review_cases",
        case["id"],
        {
            "state": state.value,
            "decision": state.value,
            "decision_reason": reason,
            "decided_at": decided_at,
            "decided_by": decision["reviewer"],
            "assigned_to": case.get("assigned_to") or decision["reviewer"],
            "updated_at": decided_at,
            "review_count": int(case.get("review_count") or 0) + 1,
        },
    )
    case = _case_record(repo, case["id"]) or case

    create_audit_event(
        repo,
        action=action.value,
        object_id=primary,
        object_type=case.get("object_type"),
        actor=decision["reviewer"],
        detail=f"{state.value}: {reason}",
        issue_id=issue_id,
        review_case_id=case["id"],
        previous_state=previous_state,
        new_state=state.value,
        extra={
            "related_object_id": related,
            "decision_id": decision["id"],
        },
    )
    if issue_id:
        _set_issue_status(repo, issue_id, state.value)
    return case


def approve_review(
    repo: CadastreRepository,
    ident: str,
    *,
    reason: str,
    reviewer: str | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Confirm the finding is real and correct.

    The finding stands and the record it concerns is accepted as surveyed. Note
    this asserts the *finding* is right; it does not re-survey anything.
    """
    return _decide(
        repo,
        ident,
        ReviewState.APPROVED,
        AuditAction.APPROVED,
        reason=reason,
        reviewer=reviewer,
        actor=actor,
    )


def reject_review(
    repo: CadastreRepository,
    ident: str,
    *,
    reason: str,
    reviewer: str | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Dismiss the finding as a false positive.

    The record is left exactly as it is. A rejected finding says the rule
    misfired here, not that the geometry was corrected.
    """
    return _decide(
        repo,
        ident,
        ReviewState.REJECTED,
        AuditAction.REJECTED,
        reason=reason,
        reviewer=reviewer,
        actor=actor,
    )


def request_resurvey(
    repo: CadastreRepository,
    ident: str,
    *,
    reason: str,
    reviewer: str | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """The finding is real but the geometry is wrong; re-measure the object.

    Raises ``RESURVEY_REQUESTED`` rather than closing anything: the finding
    stands until someone measures again, and the object is not corrected here.
    """
    return _decide(
        repo,
        ident,
        ReviewState.RESURVEY_REQUESTED,
        AuditAction.RESURVEY_REQUESTED,
        reason=reason,
        reviewer=reviewer,
        actor=actor,
    )


def mark_issue_expected(
    repo: CadastreRepository,
    ident: str,
    *,
    reason: str,
    reviewer: str | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Record the condition as known and accepted, so it stops being a defect.

    The honest counterpart to rejecting: the finding is *correct* -- the overlap
    really is there -- but it is intended, and the record should not keep
    demanding attention. The reason must therefore explain why it is expected,
    not merely assert that it is.
    """
    return _decide(
        repo,
        ident,
        ReviewState.EXPECTED,
        AuditAction.MARKED_EXPECTED,
        reason=reason,
        reviewer=reviewer,
        actor=actor,
    )


# --------------------------------------------------------------------------
# closing and reopening
# --------------------------------------------------------------------------


def close_validation_issue(
    repo: CadastreRepository,
    issue_id: str,
    *,
    reason: str,
    reviewer: str | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Close a finding, optionally after a decision.

    Closes the *issue*, which is the outward-facing outcome; a case left
    ``PENDING`` is moved to ``CLOSED`` so a closed issue cannot still look
    reviewable. The issue is never deleted -- a closed finding that disappeared
    would leave a review trail pointing at nothing.
    """
    if not reason or not reason.strip():
        raise ReviewError(REASON_REQUIRED)
    issue = _issue(repo, issue_id)
    case = _case_for_issue(repo, issue_id)
    if case is None:
        raise ReviewError(
            f"{issue_id} has no review case; create one before closing it, so the "
            "closure is attributable to a review"
        )
    if case.get("state") == ReviewState.CLOSED.value:
        return case

    previous_state = case.get("state")
    case = repo.update(
        "review_cases",
        case["id"],
        {
            "state": ReviewState.CLOSED.value,
            "decision_reason": reason,
            "decided_at": now(),
            "decided_by": reviewer or actor,
            "updated_at": now(),
        },
    ) or case

    create_audit_event(
        repo,
        action=AuditAction.ISSUE_CLOSED.value,
        object_id=issue.get("object_a"),
        object_type=_object_type_for(issue),
        actor=reviewer or actor,
        detail=f"Issue {issue_id} closed: {reason}",
        issue_id=issue_id,
        review_case_id=case["id"],
        previous_state=previous_state,
        new_state=ReviewState.CLOSED.value,
    )
    _set_issue_status(repo, issue_id, ReviewState.CLOSED.value)
    return case


def reopen_validation_issue(
    repo: CadastreRepository,
    issue_id: str,
    *,
    reason: str,
    reviewer: str | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Reopen a closed finding for review.

    Returns the case to ``PENDING`` and clears the decision, but **keeps every
    decision row**: the history of why it was closed is exactly what a reviewer
    reopening it needs to see.

    Clearing the decision uses delete-then-insert rather than ``update``, because
    a change set drops ``None`` values -- an update would leave the stale
    ``APPROVED`` and its reviewer sitting on a case that claims to be pending.
    This is the same reason ``property_volumes._upsert_volume`` replaces rather
    than updates.
    """
    if not reason or not reason.strip():
        raise ReviewError(REASON_REQUIRED)
    issue = _issue(repo, issue_id)
    case = _case_for_issue(repo, issue_id)
    if case is None:
        raise ReviewError(
            f"{issue_id} has no review case; create one before reopening it"
        )
    previous_state = case.get("state")
    reopened = {
        **case,
        "state": ReviewState.PENDING.value,
        "decision": None,
        "decision_reason": None,
        "decided_at": None,
        "decided_by": None,
        "updated_at": now(),
    }
    repo.delete("review_cases", case["id"])
    case = repo.add("review_cases", reopened) or reopened

    create_audit_event(
        repo,
        action=AuditAction.ISSUE_REOPENED.value,
        object_id=issue.get("object_a"),
        object_type=_object_type_for(issue),
        actor=reviewer or actor,
        detail=f"Issue {issue_id} reopened: {reason}",
        issue_id=issue_id,
        review_case_id=case["id"],
        previous_state=previous_state,
        new_state=ReviewState.PENDING.value,
    )
    _set_issue_status(repo, issue_id, ReviewState.PENDING.value)
    return case


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------


def get_review_decisions(
    repo: CadastreRepository, ident: str
) -> list[dict[str, Any]]:
    """Every decision on a case (or an issue's case), oldest first."""
    case = resolve_case(repo, ident)
    decisions = [
        d for d in repo.records("review_decisions") if d.get("review_case_id") == case["id"]
    ]
    return sorted(
        decisions, key=lambda d: (str(d.get("decided_at") or ""), str(d.get("id") or ""))
    )


def restore_review_outcomes(repo: CadastreRepository) -> set[str]:
    """Carry decided review states onto issues that a validation run rebuilt.

    ``validate()`` clears and regenerates the issues collection, which would
    otherwise reset every ``status`` to ``OPEN`` and silently discard a human's
    approval. Findings that still exist after a re-run get their reviewed state
    back; findings the rules no longer produce are left alone, and their review
    history remains queryable by id.

    Returns the set of issue ids whose status was restored.
    """
    issues = {issue["id"]: issue for issue in repo.records("issues")}
    restored: set[str] = set()
    for case in repo.records("review_cases"):
        state = case.get("state")
        issue_id = case.get("issue_id")
        if not issue_id or issue_id not in issues or state is None:
            continue
        if state == ReviewState.OPEN.value:
            continue
        if issues[issue_id].get("status") != state:
            _set_issue_status(repo, issue_id, state)
            restored.add(issue_id)
    return restored


__all__ = [
    "IssueNotFound",
    "ReviewError",
    "approve_review",
    "assign_review_case",
    "close_validation_issue",
    "create_review_case",
    "get_pending_reviews",
    "get_review_case",
    "get_review_decisions",
    "mark_issue_expected",
    "reopen_validation_issue",
    "reject_review",
    "request_resurvey",
    "resolve_case",
    "restore_review_outcomes",
]
