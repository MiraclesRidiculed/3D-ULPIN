"""Tests for human verification and the audit log.

Organised around the guarantees that are easy to get wrong:

* **immutability** -- decisions and audit events must not be rewritable, or they
  are not evidence;
* **attribution** -- every decision records who, when, what, why, which finding,
  which object, and the states either side;
* **legality** -- a review is decided once, and a reason is never optional;
* **durability across a validation re-run** -- rebuilding the issues collection
  must not silently discard a human's approval;
* **honesty about identity** -- the reviewer is self-asserted, and nothing here
  pretends otherwise.

The seeded demo scene is untouched by this milestone's own fixtures: no review
case is fabricated, because a decision nobody made must not appear in a
cadastral trail.
"""
from __future__ import annotations

import pytest

from app.models.enums import DECISION_ACTIONS, AuditAction, ReviewState
from app.repositories.memory import InMemoryRepository
from app.services import audit, reviews
from app.services.demo import seed_demo
from app.services.geometry import create_rectangle, polygon_to_geojson
from app.services.reviews import IssueNotFound, ReviewError
from app.services.validation import validate

#: The three findings the demo scene is built around.
ENCROACHMENT = "VAL-OUT-PV-502"
OVERLAP = "VAL-OVR-PV-201-PV-202"
INFRA = "VAL-INF-PV-B001"


@pytest.fixture
def repo():
    store = InMemoryRepository()
    seed_demo(store)
    return store


def issue_status(repo, issue_id):
    return next(i["status"] for i in repo.records("issues") if i["id"] == issue_id)


def actions(repo):
    return [e["action"] for e in repo.records("audit_events")]


# ==========================================================================
# Audit log
# ==========================================================================


def test_create_audit_event_returns_a_complete_record(repo):
    event = audit.create_audit_event(
        repo,
        action=AuditAction.GEOMETRY_CHANGED.value,
        object_id="P-001",
        object_type="PARCEL",
        actor="surveyor-1",
        detail="boundary revised",
        previous_state="version 1 (ABC)",
        new_state="version 2 (DEF)",
        extra={"method": "total-station"},
    )
    for field in (
        "id",
        "action",
        "object_id",
        "object_type",
        "actor",
        "detail",
        "issue_id",
        "review_case_id",
        "previous_state",
        "new_state",
        "occurred_at",
        "context",
    ):
        assert field in event, field
    assert event["id"].startswith("AE-")
    assert event["context"] == {"method": "total-station"}


def test_audit_event_actor_defaults_to_the_development_actor(repo):
    event = audit.create_audit_event(repo, action=AuditAction.VALIDATED.value)
    assert event["actor"] == audit.DEVELOPMENT_ACTOR
    # Deliberately self-describing: an unauthenticated caller must not be
    # credited with a plausible-looking identity.
    assert "unauthenticated" in event["actor"]


def test_audit_event_rejects_an_unknown_action(repo):
    with pytest.raises(audit.AuditError, match="unknown audit action"):
        audit.create_audit_event(repo, action="TOTALLY_MADE_UP")


def test_audit_context_defaults_to_none_rather_than_an_empty_dict(repo):
    event = audit.create_audit_event(repo, action=AuditAction.UPDATED.value)
    assert event["context"] is None


def test_every_required_audit_action_is_in_the_vocabulary():
    required = {
        "CREATED",
        "UPDATED",
        "IMPORTED",
        "PROCESSED",
        "VALIDATED",
        "APPROVED",
        "REJECTED",
        "RESURVEY_REQUESTED",
        "GEOMETRY_CHANGED",
        "ULPIN_GENERATED",
    }
    assert required <= {m.value for m in AuditAction}


def test_get_audit_history_is_chronological(repo):
    audit.create_audit_event(
        repo, action=AuditAction.UPDATED.value, object_id="PV-502", detail="first"
    )
    audit.create_audit_event(
        repo, action=AuditAction.UPDATED.value, object_id="PV-502", detail="second"
    )
    history = audit.get_audit_history(repo, "PV-502")
    assert len(history) >= 3  # the seeded CREATED plus these two
    stamps = [e["occurred_at"] for e in history]
    assert stamps == sorted(stamps)
    assert history[-1]["detail"] == "second"


def test_audit_history_order_is_total_even_within_one_clock_tick(repo):
    """A tie on the timestamp must not fall back to an arbitrary order.

    Ids carry a microsecond prefix precisely so same-tick events still have a
    defined order; otherwise two reads could disagree about what happened first.
    """
    for index in range(12):
        audit.create_audit_event(
            repo, action=AuditAction.UPDATED.value, object_id="TIE", detail=str(index)
        )
    first = [e["detail"] for e in audit.get_audit_history(repo, "TIE")]
    second = [e["detail"] for e in audit.get_audit_history(repo, "TIE")]
    assert first == second
    assert first == [str(i) for i in range(12)]


def test_audit_events_key_on_the_record_primary_key(repo):
    """The parcel's row id is ``parcel-001``; ``P-001`` is its business id.

    Worth pinning because a finding's ``object_b`` carries the business id while
    an audit event's ``object_id`` carries the row id, and conflating them would
    make the trail unjoinable to the records.
    """
    parcel = repo.records("parcels")[0]
    assert parcel["parcel_id"] == "P-001"
    assert parcel["id"] == "parcel-001"
    assert [e["object_id"] for e in audit.get_audit_history(repo, "parcel-001")]
    assert audit.get_audit_history(repo, "P-001") == []


def test_get_audit_history_only_returns_that_object(repo):
    audit.create_audit_event(repo, action=AuditAction.UPDATED.value, object_id="ZZ-9")
    assert all(e["object_id"] == "ZZ-9" for e in audit.get_audit_history(repo, "ZZ-9"))


def test_get_audit_history_of_an_untouched_object_is_empty(repo):
    assert audit.get_audit_history(repo, "NEVER-SEEN") == []


def test_get_recent_audit_events_is_newest_first(repo):
    for index in range(3):
        audit.create_audit_event(
            repo, action=AuditAction.UPDATED.value, detail=f"n{index}"
        )
    recent = audit.get_recent_audit_events(repo, limit=2)
    assert len(recent) == 2
    assert recent[0]["occurred_at"] >= recent[1]["occurred_at"]


def test_get_recent_audit_events_filters(repo):
    audit.create_audit_event(repo, action=AuditAction.APPROVED.value, object_id="A")
    audit.create_audit_event(repo, action=AuditAction.REJECTED.value, object_id="B")
    assert all(
        e["action"] == AuditAction.APPROVED.value
        for e in audit.get_recent_audit_events(repo, action=AuditAction.APPROVED.value)
    )
    assert all(
        e["object_id"] == "B"
        for e in audit.get_recent_audit_events(repo, object_id="B")
    )
    assert (
        audit.get_recent_audit_events(repo, action="NOT_AN_ACTION") == []
    )


def test_get_recent_audit_events_clamps_the_limit(repo):
    assert audit.get_recent_audit_events(repo, limit=0)
    assert audit.get_recent_audit_events(repo, limit=-5)


def test_audit_events_cannot_be_updated(repo):
    """The append-only guarantee, enforced by the storage layer.

    An empty updatable-field set means a write is silently dropped rather than
    applied. Asserted here because it is the property the whole feature rests
    on, and it would fail silently if someone widened the field set.
    """
    event = audit.create_audit_event(
        repo, action=AuditAction.APPROVED.value, detail="original"
    )
    repo.update("audit_events", event["id"], {"detail": "tampered", "action": "FORGED"})
    stored = next(e for e in repo.records("audit_events") if e["id"] == event["id"])
    assert stored["detail"] == "original"
    assert stored["action"] == AuditAction.APPROVED.value


def test_review_decisions_cannot_be_updated(repo):
    case = reviews.create_review_case(repo, ENCROACHMENT, reason="check")
    reviews.approve_review(repo, ENCROACHMENT, reason="confirmed", reviewer="r")
    decision = reviews.get_review_decisions(repo, case["id"])[0]
    repo.update("review_decisions", decision["id"], {"reason": "tampered"})
    stored = next(
        d for d in repo.records("review_decisions") if d["id"] == decision["id"]
    )
    assert stored["reason"] == "confirmed"


def test_a_review_case_cannot_be_repointed_at_another_finding(repo):
    """``review_cases`` is deliberately mutable current state; its *identity* is not.

    The service updates ``state`` freely -- that is the queue. What must not
    happen is a case being re-pointed at a different finding, which would make
    its whole decision history describe the wrong issue.
    """
    case = reviews.create_review_case(repo, ENCROACHMENT, reason="check")
    repo.update("review_cases", case["id"], {"issue_id": OVERLAP})
    stored = reviews.get_review_case(repo, case["id"])
    assert stored["issue_id"] == ENCROACHMENT


def test_a_review_case_holds_no_geometry(repo):
    """A review records a judgement, never a replacement footprint."""
    from app.services.geometry import create_rectangle, polygon_to_geojson

    case = reviews.create_review_case(repo, ENCROACHMENT, reason="check")
    repo.update(
        "review_cases",
        case["id"],
        {"geometry_3d": polygon_to_geojson(create_rectangle(0, 0, 1, 1))},
    )
    stored = reviews.get_review_case(repo, case["id"])
    assert "geometry_3d" not in stored


# ==========================================================================
# The operations record themselves
# ==========================================================================


def test_seeding_records_its_own_operations(repo):
    """A demo load really did issue identifiers, version geometry and validate."""
    recorded = set(actions(repo))
    assert AuditAction.ULPIN_GENERATED.value in recorded
    assert AuditAction.CREATED.value in recorded
    assert AuditAction.VALIDATED.value in recorded


def test_geometry_revision_is_audited_as_a_change(repo):
    from app.services.geometry_versioning import create_geometry_version

    before = len(repo.records("audit_events"))
    create_geometry_version(
        repo,
        "PV-101",
        geometry=polygon_to_geojson(create_rectangle(11, 8, 40, 44)),
        change_reason="re-surveyed",
    )
    new = repo.records("audit_events")[before:]
    assert [e["action"] for e in new] == [AuditAction.GEOMETRY_CHANGED.value]
    assert new[0]["previous_state"].startswith("version 1")
    assert new[0]["new_state"].startswith("version 2")


def test_a_provenance_only_revision_is_not_called_a_geometry_change(repo):
    """Recording a version without moving the geometry must not claim a survey."""
    from app.services.geometry_versioning import snapshot_geometry

    before = len(repo.records("audit_events"))
    snapshot_geometry(repo, "PV-101", version=2, change_reason="provenance only")
    new = repo.records("audit_events")[before:]
    assert [e["action"] for e in new] == [AuditAction.UPDATED.value]
    assert new[0]["context"]["geometry_moved"] is False


def test_validation_is_audited(repo):
    before = len(repo.records("audit_events"))
    validate(repo)
    validated = [
        e
        for e in repo.records("audit_events")[before:]
        if e["action"] == AuditAction.VALIDATED.value
    ]
    assert len(validated) == 1
    assert validated[0]["context"]["findings"] == 3
    assert validated[0]["context"]["critical"] == 2


def test_importing_a_source_is_audited(repo):
    from app.services.ingestion import register_source

    before = len(repo.records("audit_events"))
    before_ids = {s["id"] for s in repo.records("sources")}
    register_source(repo, "parcel.geojson", b"{}")
    new_source = next(
        s for s in repo.records("sources") if s["id"] not in before_ids
    )
    imported = [
        e
        for e in repo.records("audit_events")[before:]
        if e["action"] == AuditAction.IMPORTED.value
    ]
    assert len(imported) == 1
    assert imported[0]["object_id"] == new_source["id"]
    assert imported[0]["object_type"] == "DATA_SOURCE"
    assert imported[0]["context"]["filename"] == "parcel.geojson"


def test_ulpin_issuance_lists_what_it_issued(repo):
    from app.services.ulpin import assign_ulpins

    event = next(
        e
        for e in repo.records("audit_events")
        if e["action"] == AuditAction.ULPIN_GENERATED.value
    )
    # A batch event: the identifiers themselves live in the payload, so a demo
    # seed does not bury the trail under twenty near-identical rows.
    assert event["context"]["identifiers"]
    assert "VC-" in event["context"]["identifiers"][0]
    # Re-running assigns nothing and so records nothing.
    before = len(repo.records("audit_events"))
    assert assign_ulpins(repo)["assigned"] == 0
    assert len(repo.records("audit_events")) == before


# ==========================================================================
# Opening and assigning
# ==========================================================================


def test_create_review_case_starts_pending(repo):
    case = reviews.create_review_case(repo, ENCROACHMENT, reason="overhang")
    assert case["state"] == ReviewState.PENDING.value
    assert case["issue_id"] == ENCROACHMENT
    assert case["severity"] == "WARNING"
    assert case["object_a"] == "PV-502"
    assert case["object_b"] == "P-001"
    assert case["object_type"] == "PROPERTY_VOLUME"
    assert case["id"].startswith("RC-")


def test_create_review_case_projects_onto_the_issue(repo):
    assert issue_status(repo, ENCROACHMENT) == "OPEN"
    reviews.create_review_case(repo, ENCROACHMENT, reason="overhang")
    assert issue_status(repo, ENCROACHMENT) == ReviewState.PENDING.value


def test_create_review_case_with_a_reviewer_goes_straight_to_in_review(repo):
    case = reviews.create_review_case(
        repo, ENCROACHMENT, reviewer="surveyor-1", reason="overhang"
    )
    assert case["state"] == ReviewState.IN_REVIEW.value
    assert case["assigned_to"] == "surveyor-1"


def test_create_review_case_is_idempotent_per_issue(repo):
    first = reviews.create_review_case(repo, ENCROACHMENT, reason="one")
    second = reviews.create_review_case(repo, ENCROACHMENT, reason="two")
    assert first["id"] == second["id"]
    assert len(repo.records("review_cases")) == 1
    assert second["reason"] == "one"


def test_create_review_case_rejects_an_unknown_issue(repo):
    with pytest.raises(IssueNotFound):
        reviews.create_review_case(repo, "VAL-NOPE", reason="x")


def test_get_review_case_looks_up_by_case_id_or_issue_id(repo):
    case = reviews.create_review_case(repo, ENCROACHMENT, reason="overhang")
    assert reviews.get_review_case(repo, case["id"])["id"] == case["id"]
    assert reviews.get_review_case(repo, ENCROACHMENT)["id"] == case["id"]
    assert reviews.get_review_case(repo, "NOT-A-CASE") is None


def test_get_review_case_returns_none_rather_than_raising(repo):
    """A lookup, so a caller can ask without exception-driven control flow."""
    assert reviews.get_review_case(repo, "anything") is None


def test_assign_review_case(repo):
    case = reviews.create_review_case(repo, ENCROACHMENT, reason="overhang")
    assigned = reviews.assign_review_case(repo, case["id"], "surveyor-1")
    assert assigned["state"] == ReviewState.IN_REVIEW.value
    assert assigned["assigned_to"] == "surveyor-1"
    assert issue_status(repo, ENCROACHMENT) == ReviewState.IN_REVIEW.value


def test_assign_records_the_previous_assignee_on_a_handover(repo):
    case = reviews.create_review_case(repo, ENCROACHMENT, reviewer="a", reason="x")
    reviews.assign_review_case(repo, case["id"], "b")
    # The handover event is the second REVIEW_ASSIGNED for this case; the first
    # came from opening the case with a reviewer already named.
    events = [
        e
        for e in repo.records("audit_events")
        if e["action"] == AuditAction.REVIEW_ASSIGNED.value
        and e["review_case_id"] == case["id"]
    ]
    assert len(events) == 2
    assert events[1]["context"]["previous_assignee"] == "a"
    assert "was a" in events[1]["detail"]


def test_assign_requires_a_reviewer(repo):
    case = reviews.create_review_case(repo, ENCROACHMENT, reason="x")
    with pytest.raises(ReviewError, match="reviewer is required"):
        reviews.assign_review_case(repo, case["id"], "  ")


def test_assign_a_decided_case_is_refused(repo):
    reviews.create_review_case(repo, ENCROACHMENT, reason="x")
    reviews.approve_review(repo, ENCROACHMENT, reason="confirmed", reviewer="r")
    with pytest.raises(ReviewError, match="no longer be assigned"):
        reviews.assign_review_case(repo, ENCROACHMENT, "someone-else")


# ==========================================================================
# The queue
# ==========================================================================


def test_get_pending_reviews_lists_undecided_cases(repo):
    reviews.create_review_case(repo, ENCROACHMENT, reason="x")
    reviews.create_review_case(repo, OVERLAP, reason="y")
    pending = reviews.get_pending_reviews(repo)
    assert {c["issue_id"] for c in pending} == {ENCROACHMENT, OVERLAP}


def test_pending_reviews_are_ordered_by_severity(repo):
    reviews.create_review_case(repo, ENCROACHMENT, reason="x")  # WARNING
    reviews.create_review_case(repo, OVERLAP, reason="y")  # CRITICAL
    pending = reviews.get_pending_reviews(repo)
    assert pending[0]["issue_id"] == OVERLAP
    assert pending[0]["severity"] == "CRITICAL"


def test_unassigned_cases_sort_before_assigned_within_a_severity(repo):
    reviews.create_review_case(repo, INFRA, reviewer="r", reason="y")
    reviews.create_review_case(repo, OVERLAP, reason="x")  # same severity
    pending = reviews.get_pending_reviews(repo)
    assert pending[0]["issue_id"] == OVERLAP


def test_decided_cases_leave_the_queue(repo):
    reviews.create_review_case(repo, ENCROACHMENT, reason="x")
    reviews.create_review_case(repo, OVERLAP, reason="y")
    reviews.approve_review(repo, ENCROACHMENT, reason="confirmed", reviewer="r")
    assert [c["issue_id"] for c in reviews.get_pending_reviews(repo)] == [OVERLAP]


def test_pending_reviews_can_be_filtered_by_reviewer(repo):
    reviews.create_review_case(repo, ENCROACHMENT, reviewer="a", reason="x")
    reviews.create_review_case(repo, OVERLAP, reviewer="b", reason="y")
    assert [c["issue_id"] for c in reviews.get_pending_reviews(repo, reviewer="a")] == [
        ENCROACHMENT
    ]


def test_the_demo_scene_starts_with_an_empty_review_queue(repo):
    """No fabricated decisions in a cadastral trail."""
    assert repo.records("review_cases") == []
    assert repo.records("review_decisions") == []
    assert reviews.get_pending_reviews(repo) == []


# ==========================================================================
# Decisions
# ==========================================================================


@pytest.mark.parametrize(
    ("state", "action"),
    [
        (ReviewState.APPROVED, AuditAction.APPROVED),
        (ReviewState.REJECTED, AuditAction.REJECTED),
        (ReviewState.RESURVEY_REQUESTED, AuditAction.RESURVEY_REQUESTED),
        (ReviewState.EXPECTED, AuditAction.MARKED_EXPECTED),
    ],
)
def test_each_decision_sets_its_state_and_projects_it(repo, state, action):
    reviews.create_review_case(repo, ENCROACHMENT, reason="x")
    call = {
        AuditAction.APPROVED: reviews.approve_review,
        AuditAction.REJECTED: reviews.reject_review,
        AuditAction.RESURVEY_REQUESTED: reviews.request_resurvey,
        AuditAction.MARKED_EXPECTED: reviews.mark_issue_expected,
    }[action]
    case = call(repo, ENCROACHMENT, reason="because", reviewer="surveyor-1")
    assert case["state"] == state.value
    assert case["decision"] == state.value
    assert issue_status(repo, ENCROACHMENT) == state.value
    assert action.value in actions(repo)


def test_resurvey_is_not_the_same_as_reject(repo):
    """One says the finding is wrong; the other says the geometry is.

    Collapsing them would lose the only distinction that matters to whoever has
    to go out and re-measure.
    """
    reviews.create_review_case(repo, ENCROACHMENT, reason="x")
    resurveyed = reviews.request_resurvey(
        repo, ENCROACHMENT, reason="boundary is wrong", reviewer="r"
    )
    assert resurveyed["state"] == ReviewState.RESURVEY_REQUESTED.value

    reviews.create_review_case(repo, OVERLAP, reason="x")
    rejected = reviews.reject_review(
        repo, OVERLAP, reason="rule misfired", reviewer="r"
    )
    assert rejected["state"] == ReviewState.REJECTED.value
    assert rejected["state"] != resurveyed["state"]


def test_a_decision_records_everything_an_auditor_needs(repo):
    reviews.create_review_case(repo, ENCROACHMENT, reason="x")
    reviews.assign_review_case(repo, ENCROACHMENT, "surveyor-1")
    reviews.approve_review(
        repo, ENCROACHMENT, reason="confirmed on site 2026-09-26", reviewer="surveyor-1"
    )
    decision = reviews.get_review_decisions(repo, ENCROACHMENT)[0]
    assert decision["reviewer"] == "surveyor-1"
    assert decision["decided_at"]
    assert decision["decision"] == ReviewState.APPROVED.value
    assert decision["reason"] == "confirmed on site 2026-09-26"
    assert decision["issue_id"] == ENCROACHMENT
    assert decision["object_id"] == "PV-502"
    assert decision["related_object_id"] == "P-001"
    assert decision["object_type"] == "PROPERTY_VOLUME"
    assert decision["previous_state"] == ReviewState.IN_REVIEW.value
    assert decision["new_state"] == ReviewState.APPROVED.value
    assert decision["action"] == AuditAction.APPROVED.value


def test_a_decision_needs_a_reason(repo):
    reviews.create_review_case(repo, ENCROACHMENT, reason="x")
    for call in (
        reviews.approve_review,
        reviews.reject_review,
        reviews.request_resurvey,
        reviews.mark_issue_expected,
    ):
        with pytest.raises(ReviewError, match="reason is required"):
            call(repo, ENCROACHMENT, reason="   ", reviewer="r")


def test_a_review_is_decided_only_once(repo):
    reviews.create_review_case(repo, ENCROACHMENT, reason="x")
    reviews.approve_review(repo, ENCROACHMENT, reason="confirmed", reviewer="r")
    with pytest.raises(ReviewError, match="already APPROVED"):
        reviews.reject_review(repo, ENCROACHMENT, reason="changed my mind", reviewer="r")


def test_a_second_decision_adds_no_history(repo):
    reviews.create_review_case(repo, ENCROACHMENT, reason="x")
    reviews.approve_review(repo, ENCROACHMENT, reason="confirmed", reviewer="r")
    with pytest.raises(ReviewError):
        reviews.mark_issue_expected(repo, ENCROACHMENT, reason="actually fine", reviewer="r")
    assert len(reviews.get_review_decisions(repo, ENCROACHMENT)) == 1


def test_a_decision_from_a_bare_pending_case_records_its_previous_state(repo):
    reviews.create_review_case(repo, ENCROACHMENT, reason="x")
    reviews.reject_review(repo, ENCROACHMENT, reason="false positive", reviewer="r")
    decision = reviews.get_review_decisions(repo, ENCROACHMENT)[0]
    assert decision["previous_state"] == ReviewState.PENDING.value


def test_deciding_an_unopened_finding_is_refused(repo):
    with pytest.raises(ReviewError, match="No review case"):
        reviews.approve_review(repo, ENCROACHMENT, reason="x", reviewer="r")


def test_a_decision_also_appears_in_the_audit_log(repo):
    reviews.create_review_case(repo, ENCROACHMENT, reason="x")
    reviews.approve_review(repo, ENCROACHMENT, reason="confirmed", reviewer="surveyor-1")
    event = next(
        e
        for e in repo.records("audit_events")
        if e["action"] == AuditAction.APPROVED.value
    )
    assert event["actor"] == "surveyor-1"
    assert event["object_id"] == "PV-502"
    assert event["issue_id"] == ENCROACHMENT
    assert event["previous_state"] == ReviewState.PENDING.value
    assert event["new_state"] == ReviewState.APPROVED.value
    # The audit row points at the decision rather than copying it.
    decision_id = reviews.get_review_decisions(repo, ENCROACHMENT)[0]["id"]
    assert event["context"]["decision_id"] == decision_id


def test_every_decision_action_is_in_the_declared_set():
    assert DECISION_ACTIONS == {
        AuditAction.APPROVED.value,
        AuditAction.REJECTED.value,
        AuditAction.RESURVEY_REQUESTED.value,
        AuditAction.MARKED_EXPECTED.value,
    }


# ==========================================================================
# Closing and reopening
# ==========================================================================


def test_close_validation_issue(repo):
    reviews.create_review_case(repo, ENCROACHMENT, reason="x")
    case = reviews.close_validation_issue(
        repo, ENCROACHMENT, reason="resolved on site", reviewer="surveyor-1"
    )
    assert case["state"] == ReviewState.CLOSED.value
    assert issue_status(repo, ENCROACHMENT) == ReviewState.CLOSED.value
    assert AuditAction.ISSUE_CLOSED.value in actions(repo)


def test_closing_a_pending_case_closes_it_rather_than_leaving_it_reviewable(repo):
    """A closed issue must not still look like it is waiting for a reviewer."""
    reviews.create_review_case(repo, ENCROACHMENT, reason="x")
    reviews.close_validation_issue(repo, ENCROACHMENT, reason="done", reviewer="r")
    assert reviews.get_pending_reviews(repo) == []


def test_closing_deletes_nothing(repo):
    """A closed finding that disappeared would leave a trail pointing at nothing."""
    reviews.create_review_case(repo, ENCROACHMENT, reason="x")
    reviews.close_validation_issue(repo, ENCROACHMENT, reason="done", reviewer="r")
    assert ENCROACHMENT in [i["id"] for i in repo.records("issues")]


def test_close_needs_a_review_case_so_the_closure_is_attributable(repo):
    with pytest.raises(ReviewError, match="no review case"):
        reviews.close_validation_issue(repo, ENCROACHMENT, reason="done", reviewer="r")


def test_close_needs_a_reason(repo):
    reviews.create_review_case(repo, ENCROACHMENT, reason="x")
    with pytest.raises(ReviewError, match="reason is required"):
        reviews.close_validation_issue(repo, ENCROACHMENT, reason="", reviewer="r")


def test_close_is_idempotent(repo):
    reviews.create_review_case(repo, ENCROACHMENT, reason="x")
    reviews.close_validation_issue(repo, ENCROACHMENT, reason="done", reviewer="r")
    case = reviews.close_validation_issue(repo, ENCROACHMENT, reason="again", reviewer="r")
    assert case["state"] == ReviewState.CLOSED.value
    assert actions(repo).count(AuditAction.ISSUE_CLOSED.value) == 1


def test_reopen_returns_the_case_to_the_queue(repo):
    reviews.create_review_case(repo, ENCROACHMENT, reason="x")
    reviews.close_validation_issue(repo, ENCROACHMENT, reason="done", reviewer="r")
    case = reviews.reopen_validation_issue(
        repo, ENCROACHMENT, reason="new evidence", reviewer="surveyor-2"
    )
    assert case["state"] == ReviewState.PENDING.value
    assert issue_status(repo, ENCROACHMENT) == ReviewState.PENDING.value
    assert [c["issue_id"] for c in reviews.get_pending_reviews(repo)] == [ENCROACHMENT]


def test_reopen_keeps_the_decision_history(repo):
    """Why it was closed is exactly what a reviewer reopening it needs to see."""
    reviews.create_review_case(repo, ENCROACHMENT, reason="x")
    reviews.approve_review(repo, ENCROACHMENT, reason="confirmed", reviewer="r")
    reviews.close_validation_issue(repo, ENCROACHMENT, reason="done", reviewer="r")
    reviews.reopen_validation_issue(repo, ENCROACHMENT, reason="new evidence", reviewer="r")
    history = reviews.get_review_decisions(repo, ENCROACHMENT)
    assert [d["decision"] for d in history] == [ReviewState.APPROVED.value]
    case = reviews.get_review_case(repo, ENCROACHMENT)
    assert case["decision"] is None
    assert case["decided_at"] is None


def test_reopen_needs_a_reason(repo):
    reviews.create_review_case(repo, ENCROACHMENT, reason="x")
    reviews.close_validation_issue(repo, ENCROACHMENT, reason="done", reviewer="r")
    with pytest.raises(ReviewError, match="reason is required"):
        reviews.reopen_validation_issue(repo, ENCROACHMENT, reason="", reviewer="r")


def test_reopen_needs_a_case(repo):
    with pytest.raises(ReviewError, match="no review case"):
        reviews.reopen_validation_issue(repo, ENCROACHMENT, reason="x", reviewer="r")


def test_a_full_case_lifecycle_leaves_a_readable_trail(repo):
    reviews.create_review_case(repo, ENCROACHMENT, reason="possible overhang")
    reviews.assign_review_case(repo, ENCROACHMENT, "surveyor-1")
    reviews.request_resurvey(
        repo, ENCROACHMENT, reason="boundary is wrong, re-measure", reviewer="surveyor-1"
    )
    reviews.reopen_validation_issue(
        repo, ENCROACHMENT, reason="re-measured", reviewer="surveyor-1"
    )
    reviews.mark_issue_expected(
        repo, ENCROACHMENT, reason="known overhang, permitted", reviewer="surveyor-1"
    )
    reviews.close_validation_issue(
        repo, ENCROACHMENT, reason="no action needed", reviewer="surveyor-1"
    )

    trail = audit.get_audit_history(repo, "PV-502")
    assert [e["action"] for e in trail][-6:] == [
        AuditAction.REVIEW_REQUESTED.value,
        AuditAction.REVIEW_ASSIGNED.value,
        AuditAction.RESURVEY_REQUESTED.value,
        AuditAction.ISSUE_REOPENED.value,
        AuditAction.MARKED_EXPECTED.value,
        AuditAction.ISSUE_CLOSED.value,
    ]
    # Every step states both sides of the transition it performed.
    for event in trail[-4:]:
        assert event["previous_state"]
        assert event["new_state"]


# ==========================================================================
# Re-validation must not discard a human decision
# ==========================================================================


def test_revalidation_keeps_an_approval(repo):
    """The point of the whole exercise.

    ``validate()`` clears and rebuilds the issues collection. Without carrying
    reviewed states across, a re-run would silently reset every status to OPEN
    and discard a human's decision.
    """
    reviews.create_review_case(repo, ENCROACHMENT, reason="x")
    reviews.approve_review(repo, ENCROACHMENT, reason="confirmed", reviewer="r")
    assert issue_status(repo, ENCROACHMENT) == ReviewState.APPROVED.value
    validate(repo)
    assert issue_status(repo, ENCROACHMENT) == ReviewState.APPROVED.value


def test_revalidation_keeps_a_resurvey_request(repo):
    reviews.create_review_case(repo, ENCROACHMENT, reason="x")
    reviews.request_resurvey(repo, ENCROACHMENT, reason="re-measure", reviewer="r")
    validate(repo)
    assert issue_status(repo, ENCROACHMENT) == ReviewState.RESURVEY_REQUESTED.value


def test_revalidation_keeps_a_closure(repo):
    reviews.create_review_case(repo, ENCROACHMENT, reason="x")
    reviews.close_validation_issue(repo, ENCROACHMENT, reason="done", reviewer="r")
    validate(repo)
    assert issue_status(repo, ENCROACHMENT) == ReviewState.CLOSED.value


def test_revalidation_records_that_it_restored_outcomes(repo):
    reviews.create_review_case(repo, ENCROACHMENT, reason="x")
    reviews.approve_review(repo, ENCROACHMENT, reason="confirmed", reviewer="r")
    before = len(repo.records("audit_events"))
    validate(repo)
    validated = [
        e
        for e in repo.records("audit_events")[before:]
        if e["action"] == AuditAction.VALIDATED.value
    ][0]
    assert validated["context"]["review_outcomes_restored"] == [ENCROACHMENT]


def test_revalidation_still_reports_the_same_three_findings(repo):
    reviews.create_review_case(repo, ENCROACHMENT, reason="x")
    reviews.approve_review(repo, ENCROACHMENT, reason="confirmed", reviewer="r")
    assert validate(repo) == {"issues": 3, "critical": 2, "warning": 1}


def test_restore_ignores_a_finding_the_rules_no_longer_produce(repo):
    """A decision on a vanished finding must not resurrect the finding."""
    reviews.create_review_case(repo, ENCROACHMENT, reason="x")
    reviews.approve_review(repo, ENCROACHMENT, reason="confirmed", reviewer="r")
    repo.records("issues")[:] = [
        i for i in repo.records("issues") if i["id"] != ENCROACHMENT
    ]
    assert reviews.restore_review_outcomes(repo) == set()
    assert ENCROACHMENT not in [i["id"] for i in repo.records("issues")]


def test_review_history_outlives_a_rebuild(repo):
    reviews.create_review_case(repo, ENCROACHMENT, reason="x")
    reviews.approve_review(repo, ENCROACHMENT, reason="confirmed", reviewer="r")
    validate(repo)
    assert reviews.get_review_decisions(repo, ENCROACHMENT)[0]["reason"] == "confirmed"


# ==========================================================================
# Multiple findings in parallel
# ==========================================================================


def test_each_finding_gets_its_own_case(repo):
    reviews.create_review_case(repo, ENCROACHMENT, reason="x")
    reviews.create_review_case(repo, OVERLAP, reason="y")
    reviews.create_review_case(repo, INFRA, reason="z")
    assert len(repo.records("review_cases")) == 3
    assert {c["state"] for c in repo.records("review_cases")} == {
        ReviewState.PENDING.value
    }


def test_decisions_do_not_leak_between_findings(repo):
    reviews.create_review_case(repo, ENCROACHMENT, reason="x")
    reviews.create_review_case(repo, OVERLAP, reason="y")
    reviews.approve_review(repo, ENCROACHMENT, reason="confirmed", reviewer="r")
    assert issue_status(repo, OVERLAP) == ReviewState.PENDING.value
    assert reviews.get_pending_reviews(repo)[0]["issue_id"] == OVERLAP
