"""Append-only audit log of cadastral operations.

Every important operation records what happened, to whom it happened, and what
changed. The log is **append-only**: audit events are never updated and never
deleted, because a trail that can be rewritten is not a trail. The repository
layer enforces this by giving the collection an empty set of updatable fields.

Why a separate log rather than columns on the records
-----------------------------------------------------
A cadastral object changes many times, and each change has a different actor and
a different justification. Storing only "current state" loses the history; storing
history on the object makes every read pay for it. A separate append-only
collection keeps both cheap, and lets a single query answer "what has happened to
this parcel" across every subsystem that touched it.

What the log deliberately does **not** claim
---------------------------------------------
.. warning::
   The ``actor`` on an event is **self-asserted**. Authentication is not
   implemented in this milestone, so a recorded name is an assertion made by the
   caller, not an authenticated identity. It is recorded because an audit trail
   with no actor is useless, but it must not be read as proof of *who* did
   something. See :data:`DEVELOPMENT_ACTOR`.

Every event carries ``previous_state``/``new_state`` where a transition happened,
so the log can be replayed to reconstruct the sequence rather than merely
eyeballed.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Mapping

from app.models.enums import AuditAction
from app.repositories.base import CadastreRepository
from app.utils import now

#: Actor recorded when a caller does not name one.
#:
#: Authentication is out of scope for this milestone, so this stands in for a
#: signed-in user. It is deliberately an obvious placeholder rather than a
#: plausible-looking name, because a log that quietly credits a real-looking
#: identity to an unauthenticated caller is worse than one that admits the gap.
DEVELOPMENT_ACTOR = "development-actor (unauthenticated)"

#: Header an HTTP caller may use to name itself. Unverified; see the module
#: warning.
ACTOR_HEADER = "X-Reviewer"


class AuditError(RuntimeError):
    """Raised when an audit event cannot be recorded as asked."""


def _event_id() -> str:
    """Event id with a time-ordered prefix.

    The timestamp prefix is what makes the log's chronological order *stable*.
    Sorting by ``occurred_at`` alone is not enough: two events written inside the
    same clock tick tie, and a tie broken on a random uuid gives an arbitrary
    order that can differ between two reads of the same data. Prefixing the id
    with microseconds gives a total order that agrees with the timestamps.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    return f"AE-{stamp}-{uuid.uuid4().hex[:8]}"


def _known_object(object_type: str | None) -> str | None:
    return str(object_type) if object_type else None


def create_audit_event(
    repo: CadastreRepository,
    *,
    action: str,
    object_id: str | None = None,
    object_type: str | None = None,
    actor: str | None = None,
    detail: str | None = None,
    issue_id: str | None = None,
    review_case_id: str | None = None,
    previous_state: str | None = None,
    new_state: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one event to the log and return it.

    ``action`` must be a member of :class:`~app.models.enums.AuditAction`;
    accepting an unlisted string would let a typo create an event no auditor
    would ever think to search for.

    ``detail`` is a human-readable sentence. ``extra`` is a JSONB payload for
    anything machine-readable a caller wants to carry (counts, method names,
    thresholds) -- kept separate so the sentence stays readable in a log dump.
    """
    valid = {member.value for member in AuditAction}
    if action not in valid:
        raise AuditError(
            f"unknown audit action {action!r}; expected one of {sorted(valid)}"
        )

    payload: dict[str, Any] = dict(extra or {})
    event = {
        "id": _event_id(),
        "action": action,
        "object_id": object_id,
        "object_type": _known_object(object_type),
        "actor": actor or DEVELOPMENT_ACTOR,
        "detail": detail,
        "issue_id": issue_id,
        "review_case_id": review_case_id,
        "previous_state": previous_state,
        "new_state": new_state,
        "occurred_at": now(),
        "context": payload or None,
    }
    return repo.add("audit_events", event)


def get_audit_history(
    repo: CadastreRepository, object_id: str
) -> list[dict[str, Any]]:
    """Every event recorded against one object, oldest first.

    Chronological order is the only order an audit trail is useful in, and the
    underlying store makes no ordering promise, so it is applied here.
    """
    events = [
        e for e in repo.records("audit_events") if e.get("object_id") == object_id
    ]
    return sorted(events, key=_sequence_key)


def get_recent_audit_events(
    repo: CadastreRepository,
    *,
    limit: int = 50,
    action: str | None = None,
    actor: str | None = None,
    object_type: str | None = None,
    object_id: str | None = None,
) -> list[dict[str, Any]]:
    """Most recent events first, optionally filtered.

    Newest-first because this is the "what just happened" view a reviewer opens;
    :func:`get_audit_history` is the per-object chronological view.

    ``limit`` is clamped to at least one so a caller cannot ask for a negative
    window and get a confusing empty list back.
    """
    events = repo.records("audit_events")
    if action is not None:
        events = [e for e in events if e.get("action") == action]
    if actor is not None:
        events = [e for e in events if e.get("actor") == actor]
    if object_type is not None:
        events = [e for e in events if e.get("object_type") == object_type]
    if object_id is not None:
        events = [e for e in events if e.get("object_id") == object_id]
    ordered = sorted(events, key=_sequence_key, reverse=True)
    return ordered[: max(1, limit)]


def _sequence_key(event: Mapping[str, Any]) -> tuple[str, str]:
    """Sort key that is total even when timestamps tie.

    Two events written in the same millisecond must still have a defined order,
    or ``sorted`` would leave it to whatever the store happened to return.
    """
    return (str(event.get("occurred_at") or ""), str(event.get("id") or ""))


__all__ = [
    "ACTOR_HEADER",
    "AuditError",
    "DEVELOPMENT_ACTOR",
    "create_audit_event",
    "get_audit_history",
    "get_recent_audit_events",
]
