"""API contract for the review workflow and the audit log.

The service-level behaviour is covered in ``test_review_and_audit.py``. This file
covers what the HTTP layer adds on top: status codes, the self-asserted actor
plumbing, and the deliberate absence of any route that can rewrite history.
"""
from __future__ import annotations

import pytest

from app.models.enums import AuditAction

#: The demo's three findings.
ENCROACHMENT = "VAL-OUT-PV-502"
OVERLAP = "VAL-OVR-PV-201-PV-202"


def _open(client, issue_id=ENCROACHMENT, **body):
    return client.post(f"/reviews?issue_id={issue_id}", json=body)


# ==========================================================================
# Opening and listing
# ==========================================================================


def test_open_a_review_case(client):
    r = _open(client, reason="apartment overhangs the boundary")
    assert r.status_code == 201
    body = r.json()
    assert body["issue_id"] == ENCROACHMENT
    assert body["state"] == "PENDING"
    assert body["severity"] == "WARNING"
    assert body["object_a"] == "PV-502"
    assert body["reason"] == "apartment overhangs the boundary"


def test_opening_a_case_marks_the_issue_under_review(client):
    _open(client, reason="check")
    issues = {i["id"]: i for i in client.get("/validation/issues").json()}
    assert issues[ENCROACHMENT]["status"] == "PENDING"
    assert issues[OVERLAP]["status"] == "OPEN"


def test_opening_twice_returns_the_same_case(client):
    first = _open(client, reason="one").json()
    second = _open(client, reason="two").json()
    assert first["id"] == second["id"]
    assert len(client.get("/reviews").json()) == 1


def test_opening_a_case_for_an_unknown_issue_is_404(client):
    assert _open(client, issue_id="VAL-NOPE", reason="x").status_code == 404


def test_pending_reviews_are_listed(client):
    _open(client, ENCROACHMENT, reason="x")
    _open(client, OVERLAP, reason="y")
    body = client.get("/reviews").json()
    assert {c["issue_id"] for c in body} == {ENCROACHMENT, OVERLAP}
    # Most severe first: the CRITICAL overlap leads.
    assert body[0]["issue_id"] == OVERLAP


def test_pending_reviews_can_be_filtered_by_reviewer(client):
    _open(client, ENCROACHMENT, reviewer="a", reason="x")
    _open(client, OVERLAP, reviewer="b", reason="y")
    body = client.get("/reviews?reviewer=a").json()
    assert [c["issue_id"] for c in body] == [ENCROACHMENT]


def test_read_a_case_by_id_or_issue(client):
    case = _open(client, reason="x").json()
    assert client.get(f"/reviews/{case['id']}").json()["id"] == case["id"]
    assert client.get(f"/reviews/{ENCROACHMENT}").json()["id"] == case["id"]


def test_reading_an_unknown_case_is_404(client):
    assert client.get("/reviews/NOPE").status_code == 404


def test_the_queue_starts_empty_on_a_fresh_scene(client):
    """No fabricated reviews in a cadastral trail."""
    assert client.get("/reviews").json() == []


# ==========================================================================
# Assigning
# ==========================================================================


def test_assign_a_case(client):
    case = _open(client, reason="x").json()
    r = client.post(
        f"/reviews/{case['id']}/assign", json={"reviewer": "surveyor-1"}
    )
    assert r.status_code == 200
    assert r.json()["state"] == "IN_REVIEW"
    assert r.json()["assigned_to"] == "surveyor-1"


def test_assign_without_a_reviewer_is_422(client):
    case = _open(client, reason="x").json()
    assert client.post(f"/reviews/{case['id']}/assign", json={}).status_code == 422


def test_assign_an_unknown_case_is_409(client):
    r = client.post("/reviews/NOPE/assign", json={"reviewer": "a"})
    assert r.status_code == 409
    assert "No review case" in r.json()["detail"]


# ==========================================================================
# Decisions
# ==========================================================================


@pytest.mark.parametrize(
    ("endpoint", "state"),
    [
        ("approve", "APPROVED"),
        ("reject", "REJECTED"),
        ("request-resurvey", "RESURVEY_REQUESTED"),
        ("mark-expected", "EXPECTED"),
    ],
)
def test_each_decision_endpoint(client, endpoint, state):
    _open(client, reason="x")
    r = client.post(
        f"/reviews/{ENCROACHMENT}/{endpoint}",
        json={"reason": "because", "reviewer": "surveyor-1"},
    )
    assert r.status_code == 200
    assert r.json()["state"] == state
    assert client.get("/reviews").json() == []


def test_a_decision_without_a_reason_is_422(client):
    _open(client, reason="x")
    r = client.post(f"/reviews/{ENCROACHMENT}/approve", json={})
    assert r.status_code == 422


def test_deciding_an_unopened_finding_is_409(client):
    r = client.post(f"/reviews/{ENCROACHMENT}/approve", json={"reason": "x"})
    assert r.status_code == 409


def test_deciding_twice_is_409_and_leaves_one_decision(client):
    _open(client, reason="x")
    client.post(
        f"/reviews/{ENCROACHMENT}/approve", json={"reason": "confirmed", "reviewer": "r"}
    )
    again = client.post(
        f"/reviews/{ENCROACHMENT}/reject", json={"reason": "changed my mind"}
    )
    assert again.status_code == 409
    assert "already APPROVED" in again.json()["detail"]
    assert len(client.get(f"/reviews/{ENCROACHMENT}/decisions").json()) == 1


def test_the_reviewer_header_is_used_when_the_body_omits_one(client):
    _open(client, reason="x")
    client.post(
        f"/reviews/{ENCROACHMENT}/approve",
        json={"reason": "confirmed"},
        headers={"X-Reviewer": "surveyor-9"},
    )
    decision = client.get(f"/reviews/{ENCROACHMENT}/decisions").json()[0]
    assert decision["reviewer"] == "surveyor-9"


def test_an_explicit_reviewer_body_field_wins_over_the_header(client):
    _open(client, reason="x")
    client.post(
        f"/reviews/{ENCROACHMENT}/approve",
        json={"reason": "confirmed", "reviewer": "body-wins"},
        headers={"X-Reviewer": "header-loses"},
    )
    decision = client.get(f"/reviews/{ENCROACHMENT}/decisions").json()[0]
    assert decision["reviewer"] == "body-wins"


def test_a_decision_response_carries_the_full_attribution(client):
    _open(client, reason="check")
    client.post(
        f"/reviews/{ENCROACHMENT}/approve",
        json={"reason": "confirmed on site", "reviewer": "surveyor-1"},
    )
    decision = client.get(f"/reviews/{ENCROACHMENT}/decisions").json()[0]
    assert decision["reviewer"] == "surveyor-1"
    assert decision["decided_at"]
    assert decision["decision"] == "APPROVED"
    assert decision["reason"] == "confirmed on site"
    assert decision["issue_id"] == ENCROACHMENT
    assert decision["object_id"] == "PV-502"
    assert decision["related_object_id"] == "P-001"
    assert decision["previous_state"] == "PENDING"
    assert decision["new_state"] == "APPROVED"


# ==========================================================================
# Close and reopen
# ==========================================================================


def test_close_and_reopen_an_issue(client):
    _open(client, reason="x")
    closed = client.post(
        f"/reviews/issues/{ENCROACHMENT}/close",
        json={"reason": "resolved on site", "reviewer": "surveyor-1"},
    )
    assert closed.status_code == 200
    assert closed.json()["state"] == "CLOSED"
    issues = {i["id"]: i for i in client.get("/validation/issues").json()}
    assert issues[ENCROACHMENT]["status"] == "CLOSED"
    assert len(issues) == 3  # closed, never deleted

    reopened = client.post(
        f"/reviews/issues/{ENCROACHMENT}/reopen",
        json={"reason": "new evidence", "reviewer": "surveyor-1"},
    )
    assert reopened.status_code == 200
    assert reopened.json()["state"] == "PENDING"
    assert reopened.json()["decision"] is None


def test_closing_an_unopened_issue_is_409(client):
    r = client.post(f"/reviews/issues/{ENCROACHMENT}/close", json={"reason": "x"})
    assert r.status_code == 409
    assert "no review case" in r.json()["detail"]


def test_closing_an_unknown_issue_is_404(client):
    r = client.post("/reviews/issues/VAL-NOPE/close", json={"reason": "x"})
    assert r.status_code == 404


def test_reopening_keeps_the_decision_history(client):
    _open(client, reason="x")
    client.post(
        f"/reviews/{ENCROACHMENT}/approve", json={"reason": "confirmed", "reviewer": "r"}
    )
    client.post(f"/reviews/issues/{ENCROACHMENT}/close", json={"reason": "done"})
    client.post(f"/reviews/issues/{ENCROACHMENT}/reopen", json={"reason": "recheck"})
    history = client.get(f"/reviews/{ENCROACHMENT}/decisions").json()
    assert [d["decision"] for d in history] == ["APPROVED"]


# ==========================================================================
# The audit log over HTTP
# ==========================================================================


def test_the_demo_load_records_real_operations(client):
    body = client.get("/audit/events?limit=500").json()
    actions_seen = {e["action"] for e in body}
    assert AuditAction.ULPIN_GENERATED.value in actions_seen
    assert AuditAction.CREATED.value in actions_seen
    assert AuditAction.VALIDATED.value in actions_seen


def test_a_decision_shows_up_in_the_audit_log(client):
    _open(client, reason="check")
    client.post(
        f"/reviews/{ENCROACHMENT}/approve", json={"reason": "confirmed", "reviewer": "r"}
    )
    events = client.get("/audit/events?action=APPROVED").json()
    assert len(events) == 1
    assert events[0]["actor"] == "r"
    assert events[0]["object_id"] == "PV-502"
    assert events[0]["previous_state"] == "PENDING"
    assert events[0]["new_state"] == "APPROVED"


def test_audit_history_for_one_object_is_chronological(client):
    body = client.get("/audit/history/PV-502").json()
    stamps = [e["occurred_at"] for e in body]
    assert stamps == sorted(stamps)
    assert body[0]["action"] == AuditAction.CREATED.value


def test_audit_history_of_an_unknown_object_is_an_empty_list(client):
    assert client.get("/audit/history/NEVER-SEEN").json() == []


def test_audit_events_can_be_filtered(client):
    assert client.get("/audit/events?action=CREATED").json()
    assert client.get("/audit/events?action=REJECTED").json() == []


def test_an_unknown_audit_action_filter_is_422(client):
    r = client.get("/audit/events?action=NOT_AN_ACTION")
    assert r.status_code == 422
    assert "Unknown audit action" in r.json()["detail"]


def test_audit_actions_endpoint_discloses_that_actors_are_unverified(client):
    body = client.get("/audit/actions").json()
    assert body["actor_authenticated"] is False
    assert "self-asserted" in body["note"]
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
    assert required <= set(body["actions"])


def test_there_is_no_route_that_writes_or_deletes_audit_events(client):
    """Asserted from the OpenAPI document, so a new one cannot be added quietly."""
    spec = client.get("/openapi.json").json()
    for path, operations in spec["paths"].items():
        if not path.startswith("/audit"):
            continue
        assert set(operations) == {"get"}, f"{path} exposes {sorted(operations)}"


def test_audit_events_survive_a_validation_re_run(client):
    """A re-run rebuilds the findings; it must not erase the trail."""
    _open(client, reason="check")
    client.post(f"/reviews/{ENCROACHMENT}/approve", json={"reason": "confirmed", "reviewer": "r"})
    before = len(client.get("/audit/events?limit=500").json())
    client.post("/validation/run")
    after = client.get("/audit/events?limit=500").json()
    assert len(after) > before  # the re-run is itself audited
    assert any(e["action"] == AuditAction.APPROVED.value for e in after)


def test_a_review_outcome_survives_a_validation_re_run(client):
    """The whole point: a rebuild must not silently discard a human's approval."""
    _open(client, reason="check")
    client.post(f"/reviews/{ENCROACHMENT}/approve", json={"reason": "confirmed", "reviewer": "r"})
    assert client.post("/validation/run").json() == {
        "issues": 3,
        "critical": 2,
        "warning": 1,
    }
    issues = {i["id"]: i for i in client.get("/validation/issues").json()}
    assert issues[ENCROACHMENT]["status"] == "APPROVED"
    assert issues[OVERLAP]["status"] == "OPEN"
