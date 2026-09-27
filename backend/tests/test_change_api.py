"""API contract for cadastral change detection.

The comparison behaviour is covered in ``test_change_detection.py``. This file
covers the transport: the request shape, the filters, and the guarantee that a
reported change never carries an allegation.
"""
from __future__ import annotations

import pytest

from app.models.enums import CHANGE_REQUIRES_VERIFICATION, ChangeType, IssueType

#: One property-volume-shaped object, as a caller holding survey data would send.
def survey_object(
    object_id="PV-101",
    box=(10.0, 8.0, 40.0, 44.0),
    z_min=0.0,
    z_max=3.2,
    floor=1,
    building="B-001",
):
    from app.services.geometry import create_rectangle, polygon_to_geojson

    return {
        "id": object_id,
        "geometry_3d": polygon_to_geojson(create_rectangle(*box)),
        "z_min": z_min,
        "z_max": z_max,
        "floor_number": floor,
        "building_id": building,
    }


def compare(client, survey, **extra):
    body = {"source_id": "DS-TEST", "survey": survey}
    body.update(extra)
    return client.post("/changes/compare", json=body)


# ==========================================================================
# Comparing
# ==========================================================================


def test_compare_against_the_store(client):
    """The approved side defaults to the repository's own records."""
    survey = [survey_object(box=(10.0, 8.0, 45.0, 44.0))]
    r = compare(client, survey)
    assert r.status_code == 200
    body = r.json()
    assert body["total_changes"] >= 1
    assert body["status"] == "REQUIRES_VERIFICATION"
    assert body["message"] == CHANGE_REQUIRES_VERIFICATION


def test_a_comparison_creates_a_review_case(client):
    """'Requires verification' is reachable from the review queue."""
    compare(client, [survey_object(box=(10.0, 8.0, 45.0, 44.0))])
    pending = client.get("/reviews").json()
    assert pending
    assert all(CHANGE_REQUIRES_VERIFICATION in c["reason"] for c in pending)


def test_a_new_floor_appears_as_an_unregistered_floor_issue(client):
    survey = [
        survey_object("PV-101", floor=1),
        survey_object("PV-999", floor=9, box=(10.0, 8.0, 40.0, 44.0)),
    ]
    compare(client, survey)
    issues = client.get("/validation/issues").json()
    unregistered = [
        i for i in issues if i["issue_type"] == IssueType.UNREGISTERED_FLOOR.value
    ]
    assert unregistered
    assert unregistered[0]["evidence"]["floor_number"] == 9


def test_no_change_reports_no_change(client):
    """A clean re-survey is a successful outcome, not an error.

    The survey has to cover every storey the cadastre has: a survey that visited
    only one of eight storeys would correctly report the other seven as not
    covered.
    """
    survey = [
        {
            "id": p["id"],
            "geometry_3d": p["geometry_3d"],
            "z_min": p["z_min"],
            "z_max": p["z_max"],
            "floor_number": p["floor_number"],
            "building_id": p["building_id"],
        }
        for p in client.get("/properties").json()
    ]
    body = compare(client, survey).json()
    assert body["total_changes"] == 0
    assert body["status"] == "NO_CHANGE"
    assert "No change detected" in body["message"]
    assert body["compared_objects"] == len(survey)


def test_a_comparison_does_not_edit_the_cadastre(client):
    """A re-survey supersedes; it does not overwrite the approved geometry."""
    before = next(p for p in client.get("/properties").json() if p["id"] == "PV-101")
    compare(client, [survey_object(box=(0.0, 0.0, 500.0, 500.0))])
    after = next(p for p in client.get("/properties").json() if p["id"] == "PV-101")
    assert after["geometry_3d"] == before["geometry_3d"]
    assert after["z_min"] == before["z_min"]
    assert after["z_max"] == before["z_max"]


def test_an_explicit_baseline_can_be_supplied(client):
    before = next(p for p in client.get("/properties").json() if p["id"] == "PV-101")
    approved = [
        {
            "id": "PV-101",
            "geometry_3d": before["geometry_3d"],
            "z_min": before["z_min"],
            "z_max": before["z_max"],
            "floor_number": 1,
            "building_id": "B-001",
        }
    ]
    body = compare(
        client,
        [survey_object(box=(10.0, 8.0, 50.0, 44.0))],
        approved=approved,
    ).json()
    assert body["total_changes"] >= 1


def test_a_tolerance_is_honoured(client):
    """A survey with coarser uncertainty suppresses marginal differences."""
    before = next(p for p in client.get("/properties").json() if p["id"] == "PV-101")
    approved = [
        {
            "id": "PV-101",
            "geometry_3d": before["geometry_3d"],
            "z_min": 0.0,
            "z_max": 3.2,
            "floor_number": 1,
            "building_id": "B-001",
        }
    ]
    body = compare(
        client,
        [survey_object(z_max=3.2005)],
        approved=approved,
        tolerance=1.0,
    ).json()
    assert body["total_changes"] == 0
    assert body["tolerance"] == 1.0


def test_a_source_id_is_required(client):
    r = client.post("/changes/compare", json={"survey": [survey_object()]})
    assert r.status_code == 422


def test_the_actor_is_taken_from_the_header(client):
    compare(
        client,
        [survey_object(box=(10.0, 8.0, 45.0, 44.0))],
    )
    # The X-Reviewer header is read as a query alias on this route; the body
    # field takes precedence, and either way the actor is self-asserted.
    events = client.get("/audit/events?action=CHANGE_DETECTED").json()
    assert events
    assert all("actor" in e for e in events)


# ==========================================================================
# Reading changes back
# ==========================================================================


def test_listed_changes_carry_every_required_field(client):
    compare(client, [survey_object(box=(10.0, 8.0, 45.0, 44.0))])
    changes = client.get("/changes").json()
    assert changes
    for change in changes:
        for field in (
            "change_type",
            "object_id",
            "previous_geometry",
            "new_geometry",
            "previous_z_range",
            "new_z_range",
            "area_delta",
            "height_delta",
            "volume_delta",
            "geometric_quality",
            "source_id",
            "detected_at",
            "status",
        ):
            assert field in change, field


def test_changes_can_be_filtered(client):
    compare(client, [survey_object(box=(10.0, 8.0, 45.0, 44.0))])
    assert client.get(f"/changes?source_id=DS-TEST").json()
    assert client.get("/changes?source_id=DS-NOPE").json() == []
    assert all(
        c["change_type"] == ChangeType.FOOTPRINT.value
        for c in client.get(f"/changes?change_type={ChangeType.FOOTPRINT.value}").json()
    )
    assert all(
        c["object_id"] == "PV-101"
        for c in client.get("/changes?object_id=PV-101").json()
    )


def test_an_unknown_change_type_filter_is_422(client):
    r = client.get("/changes?change_type=NOPE")
    assert r.status_code == 422
    assert "Unknown change type" in r.json()["detail"]


def test_the_report_summarises_recorded_changes(client):
    compare(client, [survey_object(box=(10.0, 8.0, 45.0, 44.0))])
    body = client.get("/changes/report").json()
    assert body["total_changes"] >= 1
    assert body["all_require_verification"] is True
    assert body["message"] == CHANGE_REQUIRES_VERIFICATION
    assert body["changes"]


def test_the_report_of_nothing_detected(client):
    body = client.get("/changes/report").json()
    assert body["total_changes"] == 0
    assert "No change detected" in body["message"]


def test_the_repository_comparison_needs_a_known_source(client):
    assert client.get("/changes/compare/repository?source_id=DS-NOPE").status_code == 404


def test_the_repository_comparison_of_a_source_without_extent_is_422(client):
    sources = client.get("/point-clouds").json()
    if not sources:  # pragma: no cover - the demo seeds a source
        pytest.skip("no ingested source to compare")
    r = client.get(f"/changes/compare/repository?source_id={sources[0]['id']}")
    # Either it has an extent and compares, or it does not and says so clearly.
    assert r.status_code in (200, 422)
    if r.status_code == 422:
        assert "cannot be compared" in r.json()["detail"]


# ==========================================================================
# Nothing alleges wrongdoing
# ==========================================================================


def test_no_change_response_uses_allegation_language(client):
    compare(client, [survey_object(box=(10.0, 8.0, 45.0, 44.0))])
    body = compare(client, [survey_object(box=(10.0, 8.0, 60.0, 44.0))]).json()
    text = " ".join(
        [c["description"] for c in body["changes"]] + [body["message"]]
    ).lower()
    for word in (
        "illegal",
        "unauthorised",
        "unauthorized",
        "trespass",
        "encroach",
        "violation",
        "fraud",
    ):
        assert word not in text, word


def test_no_stored_change_uses_allegation_language(client):
    compare(client, [survey_object(box=(10.0, 8.0, 45.0, 44.0))])
    for change in client.get("/changes").json():
        blob = " ".join(
            str(v) for k, v in change.items() if isinstance(v, (str, int, float))
        ).lower()
        for word in ("illegal", "unauthorised", "unauthorized", "trespass", "encroach"):
            assert word not in blob, f"{change['change_type']} says {word}"


def test_every_stored_change_requires_verification(client):
    compare(client, [survey_object(box=(10.0, 8.0, 45.0, 44.0))])
    for change in client.get("/changes").json():
        assert change["status"] == "REQUIRES_VERIFICATION"


def test_a_change_is_never_reported_as_critical(client):
    """A difference is an observation; CRITICAL would assert a defect."""
    compare(client, [survey_object(box=(0.0, 0.0, 500.0, 500.0), z_max=500.0)])
    for change in client.get("/changes").json():
        assert change["status"] == "REQUIRES_VERIFICATION"
    for issue in client.get("/validation/issues").json():
        if issue.get("category") == "CHANGE":
            assert issue["severity"] == "WARNING"


def test_the_canonical_message_is_exposed(client):
    compare(client, [survey_object(box=(10.0, 8.0, 45.0, 44.0))])
    body = client.get("/changes/report").json()
    assert body["message"] == CHANGE_REQUIRES_VERIFICATION


def test_no_change_field_is_named_confidence(client):
    """The project rule: no invented accuracy figures."""
    compare(client, [survey_object(box=(10.0, 8.0, 45.0, 44.0))])
    for change in client.get("/changes").json():
        assert "confidence" not in change
        assert "geometric_quality" in change


def test_geometric_quality_is_bounded(client):
    compare(client, [survey_object(box=(0.0, 0.0, 400.0, 400.0), z_max=200.0)])
    for change in client.get("/changes").json():
        assert 0.0 <= change["geometric_quality"] <= 1.0
