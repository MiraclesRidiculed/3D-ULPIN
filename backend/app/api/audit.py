"""Read access to the cadastral audit log.

The log is written by :mod:`app.services.audit` as a side effect of the
operations themselves; nothing here creates events. There is deliberately no
``POST``/``PUT``/``DELETE`` here, and the collection has no updatable fields, so
there is no route by which an event could be rewritten.

.. warning::
   The ``actor`` on each event is **self-asserted**. Authentication is not
   implemented, so it records that somebody claimed to be acting, not who they
   were.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import request_repository
from app.models.enums import AuditAction
from app.models.schemas import AuditEvent
from app.repositories.base import CadastreRepository
from app.services.audit import get_audit_history, get_recent_audit_events

router = APIRouter(tags=["audit"])


@router.get("/audit/events", response_model=list[AuditEvent])
def recent_events(
    limit: int = Query(50, ge=1, le=500),
    action: str | None = None,
    actor: str | None = None,
    object_type: str | None = None,
    object_id: str | None = None,
    repository: CadastreRepository = Depends(request_repository),
) -> list[dict[str, Any]]:
    """Most recent events first, optionally filtered.

    ``action`` is validated against the known vocabulary so a typo returns a
    clear 422 rather than a plausible-looking empty list.
    """
    if action is not None and action not in {m.value for m in AuditAction}:
        raise HTTPException(
            422,
            f"Unknown audit action {action!r}; expected one of "
            f"{sorted(m.value for m in AuditAction)}",
        )
    return get_recent_audit_events(
        repository,
        limit=limit,
        action=action,
        actor=actor,
        object_type=object_type,
        object_id=object_id,
    )


@router.get("/audit/history/{object_id}", response_model=list[AuditEvent])
def object_history(
    object_id: str,
    repository: CadastreRepository = Depends(request_repository),
) -> list[dict[str, Any]]:
    """Every event recorded against one object, oldest first.

    Chronological, because reconstructing a sequence is the entire point of an
    audit trail. Returns an empty list for an object nobody has touched, which is
    the normal case for freshly imported data.
    """
    return get_audit_history(repository, object_id)


@router.get("/audit/actions")
def list_actions() -> dict[str, Any]:
    """The audit vocabulary, so a UI can build filters without hard-coding it."""
    return {
        "actions": [m.value for m in AuditAction],
        "decision_actions": sorted(
            m.value for m in AuditAction if m.value in _decision_actions()
        ),
        "actor_authenticated": False,
        "note": (
            "Every actor is self-asserted. Authentication is not implemented, so "
            "an event records that somebody claimed to be acting, not who they "
            "were."
        ),
    }


def _decision_actions() -> set[str]:
    from app.models.enums import DECISION_ACTIONS

    return set(DECISION_ACTIONS)
