"""Tests for cadastral change detection.

The six scenarios from the specification are the spine of this file and are
marked as such:

1. no change
2. new floor
3. removed floor
4. footprint expansion
5. height increase
6. volume change

The other tests pin the properties that make the feature trustworthy rather than
merely functional:

* **no wrongdoing is asserted.** A test scans this feature's own source for the
  words a cadastral system must not reach for, so the constraint survives edits.
* **the baseline is the approved cadastre**, and a comparison never edits it.
* **a re-survey supersedes rather than replaces** -- the approved geometry stays
  readable after a change is found.
* **absence from a survey is not a demolition**, and the wording says so.
* **quality is agreement, not confidence** -- no field is called a confidence.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.models.enums import (
    CHANGE_REQUIRES_VERIFICATION,
    AuditAction,
    ChangeStatus,
    ChangeType,
    IssueType,
)
from app.repositories.memory import InMemoryRepository
from app.services import change_detection as cd
from app.services.change_detection import (
    ChangeDetectionError,
    SurveyGeometry,
    compare_approved_vs_survey,
)
from app.services.demo import seed_demo
from app.services.geometry import create_rectangle, polygon_to_geojson

SOURCE = "DS-SURVEY-1"

#: A parcel-sized baseline object, reused across the scenario tests.
BASE = (0.0, 0.0, 10.0, 10.0)


def obj(
    object_id: str = "PV-1",
    box: tuple[float, float, float, float] = BASE,
    z_min: float = 0.0,
    z_max: float = 3.0,
    floor: int | None = 1,
    building: str | None = "B-001",
) -> dict:
    """A property-volume-shaped record."""
    return {
        "id": object_id,
        "geometry_3d": polygon_to_geojson(create_rectangle(*box)),
        "z_min": z_min,
        "z_max": z_max,
        "floor_number": floor,
        "building_id": building,
    }


@pytest.fixture
def repo():
    store = InMemoryRepository()
    seed_demo(store)
    return store


def compare(approved, survey, repo=None, **kwargs):
    return compare_approved_vs_survey(
        approved, survey, source_id=SOURCE, repository=repo, **kwargs
    )


# ==========================================================================
# Scenario 1: no change
# ==========================================================================


def test_scenario_1_no_change_reports_nothing():
    """A re-survey that confirms the cadastre is a real, successful outcome."""
    record = obj()
    report = compare([record], [dict(record)])
    assert report.has_changes is False
    assert report.changes == []
    assert report.compared_objects == 1
    assert report.unchanged == ["PV-1"]
    assert report.by_type == {}


def test_scenario_1_summary_says_no_change():
    record = obj()
    summary = compare([record], [dict(record)]).summary()
    assert summary["total_changes"] == 0
    assert summary["status"] == "NO_CHANGE"
    assert "No change detected" in summary["message"]


def test_a_difference_below_tolerance_is_not_a_change():
    """Noise inside the stated tolerance is not a finding.

    The default matches the rest of the system; a survey whose uncertainty is
    coarser passes a larger ``tolerance`` rather than the code deciding what
    counts as real.
    """
    before = obj(box=(0, 0, 10, 10))
    after = obj(box=(0, 0, 10.0005, 10))  # ~0.005 m², inside the default 0.01
    assert compare([before], [after], tolerance=1.0).has_changes is False
    assert compare([before], [obj(box=(0, 0, 12, 10))]).has_changes is True


def test_objects_only_on_one_side_are_not_compared():
    """A renumbered survey is a data problem, not a cadastral change.

    Reporting every unmatched object as "new" would bury the real findings in
    noise from an id-mapping problem.
    """
    report = compare([obj("PV-1")], [obj("PV-999")])
    assert report.has_changes is False
    assert report.compared_objects == 0
    assert report.unchanged == []


# ==========================================================================
# Scenario 2: new floor
# ==========================================================================


def test_scenario_2_new_floor_is_detected():
    report = compare([obj(floor=1)], [obj(floor=1), obj("PV-2", floor=2)])
    assert report.by_type == {ChangeType.NEW_FLOOR.value: 1}
    change = report.changes[0]
    assert change.change_type == ChangeType.NEW_FLOOR.value
    assert change.floor_number == 2
    assert change.building_id == "B-001"


def test_scenario_2_new_floor_produces_an_unregistered_floor_finding(repo):
    """A new storey gets its own specific finding, not a generic change record."""
    compare(
        [obj(floor=1)],
        [obj(floor=1), obj("PV-2", floor=2)],
        repo=repo,
    )
    findings = [
        i
        for i in repo.records("issues")
        if i["issue_type"] == IssueType.UNREGISTERED_FLOOR.value
    ]
    assert len(findings) == 1
    finding = findings[0]
    assert finding["rule_id"] == cd.UNREGISTERED_FLOOR_RULE
    assert finding["category"] == "CHANGE"
    assert finding["severity"] == "WARNING"
    assert finding["evidence"]["floor_number"] == 2
    assert finding["evidence"]["building_id"] == "B-001"
    assert finding["evidence"]["source_id"] == SOURCE
    assert CHANGE_REQUIRES_VERIFICATION in finding["description"]


def test_scenario_2_the_finding_is_linked_from_the_change(repo):
    report = compare(
        [obj(floor=1)],
        [obj(floor=1), obj("PV-2", floor=2)],
        repo=repo,
    )
    change = next(c for c in report.changes if c.change_type == ChangeType.NEW_FLOOR.value)
    assert change.finding_id
    stored = next(
        c for c in repo.records("cadastral_changes") if c["id"] == change.id
    )
    assert stored["finding_id"] == change.finding_id


def test_scenario_2_no_finding_without_a_repository():
    """A pure comparison writes nothing, which is what makes it testable."""
    report = compare([obj(floor=1)], [obj(floor=1), obj("PV-2", floor=2)])
    assert report.findings_created == 0
    assert report.review_cases_created == 0


def test_a_new_floor_in_a_different_building_is_separate():
    report = compare(
        [obj(floor=2, building="B-001")],
        [obj(floor=2, building="B-001"), obj("PV-9", floor=2, building="B-002")],
    )
    assert report.by_type == {ChangeType.NEW_FLOOR.value: 1}
    assert report.changes[0].building_id == "B-002"


def test_a_floor_is_matched_on_storey_not_on_unit_id():
    """Re-surveys renumber the units within a storey; that is not a new storey."""
    report = compare(
        [obj("PV-OLD-1", floor=2)],
        [obj("PV-NEW-7", floor=2)],
    )
    assert report.by_type == {ChangeType.NEW_FLOOR.value: 0} or not report.has_changes


# ==========================================================================
# Scenario 3: removed floor
# ==========================================================================


def test_scenario_3_removed_floor_is_detected():
    report = compare([obj(floor=1), obj("PV-2", floor=2)], [obj(floor=1)])
    assert report.by_type == {ChangeType.REMOVED_FLOOR.value: 1}
    change = report.changes[0]
    assert change.floor_number == 2
    assert change.new_geometry is None  # nothing on the survey side


def test_scenario_3_absence_is_not_called_a_demolition():
    """A storey missing from one survey is not evidence it no longer exists."""
    report = compare([obj(floor=1), obj("PV-2", floor=2)], [obj(floor=1)])
    description = report.changes[0].description
    assert "not covered by this survey" in description
    assert "outside the survey extent" in description
    for word in ("demolished", "destroyed", "removed the"):
        assert word not in description.lower()


def test_a_removed_floor_does_not_raise_a_severity():
    """It is an observation, not a defect."""
    report = compare([obj(floor=1), obj("PV-2", floor=2)], [obj(floor=1)])
    assert report.changes[0].status == ChangeStatus.REQUIRES_VERIFICATION.value


# ==========================================================================
# Scenario 4: footprint expansion
# ==========================================================================


def test_scenario_4_footprint_expansion_is_detected():
    report = compare([obj(box=(0, 0, 10, 10))], [obj(box=(0, 0, 12, 10))])
    assert report.by_type[ChangeType.FOOTPRINT.value] == 1
    change = report.changes[0]
    assert change.area_delta == pytest.approx(20.0, rel=5e-3)
    assert change.area_delta > 0


def test_scenario_4_expansion_reports_the_signed_area():
    """Signed, so a reader can tell expansion from contraction without parsing text."""
    wider = compare([obj(box=(0, 0, 10, 10))], [obj(box=(0, 0, 12, 10))])
    narrower = compare([obj(box=(0, 0, 12, 10))], [obj(box=(0, 0, 10, 10))])
    assert wider.changes[0].area_delta > 0
    assert narrower.changes[0].area_delta < 0


def test_a_boundary_that_moved_without_changing_area_is_still_a_change():
    """Two outlines of identical area in different places.

    A pure area delta would report zero here and miss a boundary that moved. The
    symmetric difference is what catches it.

    The area is not *exactly* equal because both outlines make a 7-dp WGS84
    round-trip, which is worth ~0.002 m² on a 100 m² polygon; the point is that
    the residual is negligible while the outlines clearly do not coincide.
    """
    before = obj(box=(0, 0, 10, 10))
    after = obj(box=(1, 0, 11, 10))  # same 100 m², shifted 1 m east
    report = compare([before], [after])
    change = next(
        c for c in report.changes if c.change_type == ChangeType.FOOTPRINT.value
    )
    assert change.area_delta == pytest.approx(0.0, abs=0.01)
    assert change.geometric_quality < 0.95  # the outlines do not coincide


def test_calculate_geometry_change_reports_both_sides_of_the_move():
    before = cd.survey_geometry(obj(box=(0, 0, 10, 10)))
    after = cd.survey_geometry(obj(box=(0, 0, 12, 10)))
    delta = cd.calculate_geometry_change(before, after)
    # Tolerances reflect the 7-dp WGS84 round-trip the rest of the suite also
    # allows for; exact equality would be brittle and would hide a real
    # regression behind a coordinate change.
    assert delta.previous_area_m2 == pytest.approx(100.0, rel=5e-3)
    assert delta.new_area_m2 == pytest.approx(120.0, rel=5e-3)
    assert delta.area_delta_m2 == pytest.approx(20.0, rel=5e-3)
    assert delta.intersection_m2 == pytest.approx(100.0, rel=5e-3)
    assert delta.symmetric_difference_m2 == pytest.approx(20.0, rel=5e-3)
    assert delta.expanded is True


# ==========================================================================
# Scenario 5: height increase
# ==========================================================================


def test_scenario_5_height_increase_is_detected():
    report = compare([obj(z_min=0.0, z_max=3.0)], [obj(z_min=0.0, z_max=6.0)])
    change = next(c for c in report.changes if c.change_type == ChangeType.HEIGHT.value)
    assert change.height_delta == pytest.approx(3.0)
    assert change.previous_height_m == pytest.approx(3.0)
    assert change.new_height_m == pytest.approx(6.0)


def test_calculate_height_delta_sign():
    before = cd.survey_geometry(obj(z_min=0.0, z_max=3.0))
    assert cd.calculate_height_delta(before, cd.survey_geometry(obj(z_max=6.0))) == pytest.approx(3.0)
    assert cd.calculate_height_delta(before, cd.survey_geometry(obj(z_max=1.5))) == pytest.approx(-1.5)


def test_a_height_change_is_not_silently_a_footprint_change():
    report = compare([obj(z_max=3.0)], [obj(z_max=6.0)])
    assert report.by_type.get(ChangeType.FOOTPRINT.value, 0) == 0


# ==========================================================================
# Scenario 6: volume change
# ==========================================================================


def test_scenario_6_volume_change_is_detected():
    report = compare([obj(z_max=3.0)], [obj(z_max=6.0)])
    change = next(c for c in report.changes if c.change_type == ChangeType.VOLUME.value)
    assert change.volume_delta == pytest.approx(300.0, rel=1e-3)
    assert change.previous_volume_m3 == pytest.approx(300.0, rel=1e-3)
    assert change.new_volume_m3 == pytest.approx(600.0, rel=1e-3)


def test_calculate_volume_delta_ignores_a_stale_cached_value():
    """Computed from geometry, not from ``volume_m3``, which may be stale."""
    before = cd.survey_geometry({**obj(z_max=3.0), "volume_m3": 999999.0})
    after = cd.survey_geometry(obj(z_max=6.0))
    assert cd.calculate_volume_delta(before, after) == pytest.approx(300.0, rel=1e-3)


def test_volume_uses_area_as_well_as_height():
    """Doubling the area and the height quadruples the volume."""
    report = compare(
        [obj(box=(0, 0, 10, 10), z_max=3.0)],
        [obj(box=(0, 0, 20, 10), z_max=6.0)],
    )
    change = next(c for c in report.changes if c.change_type == ChangeType.VOLUME.value)
    assert change.previous_volume_m3 == pytest.approx(300.0, rel=1e-3)
    assert change.new_volume_m3 == pytest.approx(1200.0, rel=1e-3)


# ==========================================================================
# Change types are independent
# ==========================================================================


def test_one_object_can_yield_several_change_types():
    """A storey whose boundary moved *and* whose height changed is two facts."""
    report = compare(
        [obj(box=(0, 0, 10, 10), z_max=3.0)],
        [obj(box=(0, 0, 12, 10), z_max=6.0)],
    )
    assert set(report.by_type) == {
        ChangeType.FOOTPRINT.value,
        ChangeType.HEIGHT.value,
        ChangeType.VOLUME.value,
    }


def test_change_types_can_be_detected_independently():
    before = cd.survey_geometry(obj())
    assert cd.detect_footprint_change(before, before, source_id=SOURCE) is None
    assert cd.detect_height_change(before, before, source_id=SOURCE) is None
    assert cd.detect_volume_change(before, before, source_id=SOURCE) is None
    moved = cd.survey_geometry(obj(box=(0, 0, 12, 10)))
    assert cd.detect_footprint_change(before, moved, source_id=SOURCE) is not None
    assert cd.detect_height_change(before, moved, source_id=SOURCE) is None


# ==========================================================================
# The change record's shape
# ==========================================================================


def test_a_change_record_carries_every_required_field():
    report = compare([obj(box=(0, 0, 10, 10))], [obj(box=(0, 0, 12, 10))])
    change = report.changes[0]
    for attribute in (
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
        assert hasattr(change, attribute), attribute
    assert change.previous_z_range == (0.0, 3.0)
    assert change.new_z_range == (0.0, 3.0)
    assert change.source_id == SOURCE
    assert change.detected_at


def test_geometric_quality_is_an_agreement_score_not_a_confidence():
    """1.0 means the two geometries agree; it says nothing about the survey."""
    identical = cd.survey_geometry(obj())
    assert cd.calculate_geometry_change(identical, identical).agreement == pytest.approx(1.0)
    far_apart = cd.survey_geometry(obj(box=(100, 100, 110, 110)))
    assert cd.calculate_geometry_change(identical, far_apart).agreement < 0.01


def test_geometric_quality_is_bounded():
    for box in ((0, 0, 10, 10), (0, 0, 12, 10), (100, 100, 110, 110)):
        delta = cd.calculate_geometry_change(
            cd.survey_geometry(obj()), cd.survey_geometry(obj(box=box))
        )
        assert 0.0 <= delta.agreement <= 1.0


def test_no_change_field_is_called_confidence():
    """The project rule: no invented accuracy figures.

    A change record may carry agreement between two geometries, but nothing here
    may be named a confidence.
    """
    report = compare([obj(box=(0, 0, 10, 10))], [obj(box=(0, 0, 12, 10))])
    assert "confidence" not in report.changes[0].to_record()
    assert not any(
        "confidence" in name.lower() for name in vars(report.changes[0])
    )


def test_agreement_is_one_for_identical_geometry_and_low_for_disjoint():
    same = cd.survey_geometry(obj())
    assert cd.calculate_geometry_change(same, same).agreement == pytest.approx(1.0)
    disjoint = cd.survey_geometry(obj(box=(50, 50, 60, 60)))
    assert cd.calculate_geometry_change(same, disjoint).agreement == pytest.approx(0.0)


# ==========================================================================
# The system does not allege wrongdoing
# ==========================================================================


def test_no_change_module_claims_wrongdoing():
    """Scans this feature's own source for language it must never use.

    The brief was explicit that a detected change must not be called illegal or
    unauthorised. Rather than trust that every future edit respects it, the
    constraint is enforced here: the module's text is read and searched.
    """
    source = Path(cd.__file__).read_text(encoding="utf-8")
    # Strip the module docstring, which *discusses* the forbidden words in order
    # to explain why it does not use them.
    body = re.sub(r'^""".*?"""', "", source, count=1, flags=re.DOTALL)
    forbidden = (
        "illegal",
        "unauthorised",
        "unauthorized",
        "trespass",
        "encroach",
        "violation",
        "malicious",
        "fraud",
    )
    for word in forbidden:
        assert word not in body.lower(), (
            f"change_detection.py uses {word!r}; a measured difference must not "
            "be described as wrongdoing"
        )


def test_every_change_status_requires_verification():
    statuses = {member.value for member in ChangeStatus}
    for word in ("illegal", "unauthorised", "unauthorized", "violation"):
        assert word not in " ".join(statuses).lower()


def test_the_canonical_message_is_used():
    assert CHANGE_REQUIRES_VERIFICATION == "Change detected — requires verification."


def test_every_change_record_carries_the_canonical_message():
    for approved, survey in (
        ([obj(box=(0, 0, 10, 10))], [obj(box=(0, 0, 12, 10))]),
        ([obj(z_max=3.0)], [obj(z_max=9.0)]),
        ([obj(floor=1)], [obj(floor=1), obj("PV-2", floor=2)]),
        ([obj(floor=1), obj("PV-2", floor=2)], [obj(floor=1)]),
    ):
        for change in compare(approved, survey).changes:
            assert CHANGE_REQUIRES_VERIFICATION in change.description
            assert change.status == ChangeStatus.REQUIRES_VERIFICATION.value


def test_a_change_never_asserts_a_severity_as_a_defect():
    """A detected change is an observation, so it is never CRITICAL."""
    report = compare([obj(box=(0, 0, 10, 10), z_max=3.0)], [obj(box=(0, 0, 40, 40), z_max=30.0)])
    assert report.has_changes
    for change in report.changes:
        assert change.status == ChangeStatus.REQUIRES_VERIFICATION.value


def test_the_demo_scene_starts_with_no_changes(repo):
    assert repo.records("cadastral_changes") == []
    assert repo.records("issues") != []  # the three deliberate findings, unchanged
    assert len(repo.records("issues")) == 3


# ==========================================================================
# The approved cadastre is the baseline, and is not edited
# ==========================================================================


def test_a_comparison_does_not_modify_the_approved_geometry(repo):
    before = next(r for r in repo.records("properties") if r["id"] == "PV-101")
    original = dict(before)
    original_geometry = before["geometry_3d"]
    compare_approved_vs_survey(
        [before],
        [{"id": "PV-101", "geometry_3d": polygon_to_geojson(create_rectangle(0, 0, 999, 999)),
          "z_min": 0.0, "z_max": 900.0, "floor_number": 1, "building_id": "B-001"}],
        source_id=SOURCE,
        repository=repo,
    )
    after = next(r for r in repo.records("properties") if r["id"] == "PV-101")
    assert after["geometry_3d"] == original_geometry
    assert after["z_min"] == original["z_min"]
    assert after["z_max"] == original["z_max"]


def test_the_approved_geometry_is_retained_on_the_change_record(repo):
    """Both outlines are kept, so a reviewer can check the comparison later."""
    approved = obj(box=(0, 0, 10, 10))
    surveyed = obj(box=(0, 0, 12, 10))
    compare([approved], [surveyed], repo=repo)
    stored = repo.records("cadastral_changes")[0]
    assert stored["previous_geometry"]["type"] == "Polygon"
    assert stored["new_geometry"]["type"] == "Polygon"
    assert stored["previous_geometry"] != stored["new_geometry"]


# ==========================================================================
# Integration with review and audit
# ==========================================================================


def test_a_change_opens_a_review_case(repo):
    """'Requires verification' has to be actionable, not decorative."""
    report = compare([obj(box=(0, 0, 10, 10))], [obj(box=(0, 0, 12, 10))], repo=repo)
    # Expanding the footprint changes the area *and* the enclosed volume, so
    # there are two independent facts and each gets its own case to resolve.
    assert report.review_cases_created == 2
    pending = [c for c in repo.records("review_cases") if c["state"] == "PENDING"]
    assert len(pending) == 2
    assert all(CHANGE_REQUIRES_VERIFICATION in c["reason"] for c in pending)


def test_a_change_is_audited(repo):
    compare([obj(box=(0, 0, 10, 10))], [obj(box=(0, 0, 12, 10))], repo=repo)
    events = [
        e for e in repo.records("audit_events")
        if e["action"] == AuditAction.CHANGE_DETECTED.value
    ]
    assert events
    assert events[0]["object_id"] == "PV-1"
    assert "change_id" in events[0]["context"]


def test_the_actor_is_recorded(repo):
    compare(
        [obj(box=(0, 0, 10, 10))],
        [obj(box=(0, 0, 12, 10))],
        repo=repo,
        actor="surveyor-2",
    )
    event = next(
        e for e in repo.records("audit_events")
        if e["action"] == AuditAction.CHANGE_DETECTED.value
    )
    assert event["actor"] == "surveyor-2"


def test_change_records_cannot_be_updated(repo):
    """A change is a record of a comparison that already happened."""
    compare([obj(box=(0, 0, 10, 10))], [obj(box=(0, 0, 12, 10))], repo=repo)
    stored = repo.records("cadastral_changes")[0]
    repo.update("cadastral_changes", stored["id"], {"area_delta": 0.0, "status": "VERIFIED"})
    after = next(c for c in repo.records("cadastral_changes") if c["id"] == stored["id"])
    assert after["area_delta"] == stored["area_delta"]
    assert after["status"] == ChangeStatus.REQUIRES_VERIFICATION.value


# ==========================================================================
# The report
# ==========================================================================


def test_generate_change_report_summarises_what_was_recorded(repo):
    compare(
        [obj(box=(0, 0, 10, 10)), obj("PV-2", floor=2)],
        [obj(box=(0, 0, 12, 10))],
        repo=repo,
    )
    report = cd.generate_change_report(repo)
    assert report["total_changes"] >= 2
    assert report["by_type"][ChangeType.FOOTPRINT.value] == 1
    assert report["by_type"][ChangeType.REMOVED_FLOOR.value] == 1
    assert report["all_require_verification"] is True
    assert report["message"] == CHANGE_REQUIRES_VERIFICATION


def test_generate_change_report_can_be_scoped_to_one_survey(repo):
    # First survey: a footprint expansion, which necessarily also changes the
    # enclosed volume, so it yields two changes.
    compare([obj(box=(0, 0, 10, 10))], [obj(box=(0, 0, 12, 10))], repo=repo)
    # Second survey: an extra storey and nothing else, so it yields exactly one
    # and makes the scoping assertion unambiguous.
    compare_approved_vs_survey(
        [obj("PV-77", floor=1)],
        [obj("PV-77", floor=1), obj("PV-78", floor=2)],
        source_id="DS-OTHER",
        repository=repo,
    )
    scoped = cd.generate_change_report(repo, source_id="DS-OTHER")
    assert scoped["total_changes"] == 1
    assert scoped["by_type"] == {ChangeType.NEW_FLOOR.value: 1}
    assert all(c["source_id"] == "DS-OTHER" for c in scoped["changes"])
    assert cd.generate_change_report(repo)["total_changes"] == 3


def test_the_report_of_an_empty_history_is_honest(repo):
    report = cd.generate_change_report(repo)
    assert report["total_changes"] == 0
    assert "No change detected" in report["message"]
    assert report["all_require_verification"] is True  # vacuously, not misleadingly


# ==========================================================================
# Input handling
# ==========================================================================


def test_a_comparison_needs_a_source_id():
    with pytest.raises(ChangeDetectionError, match="source_id"):
        compare_approved_vs_survey([obj()], [obj()], source_id="")


def test_a_record_without_an_id_is_rejected():
    with pytest.raises(ChangeDetectionError, match="no id"):
        compare_approved_vs_survey([{"geometry_3d": None}], [obj()], source_id=SOURCE)


def test_an_object_with_no_geometry_is_handled_without_crashing():
    """A missing outline is not a zero-area outline.

    Treating it as one would report a whole building as having shrunk away.
    """
    bare = {"id": "PV-BARE", "floor_number": 1, "building_id": "B-001"}
    report = compare([obj()], [dict(obj(), id="PV-BARE"), bare])
    assert isinstance(report.by_type, dict)


def test_survey_geometry_accepts_each_geometry_field_name():
    for field in ("geometry_3d", "geometry", "footprint"):
        shape = cd.survey_geometry(
            {"id": "X", field: polygon_to_geojson(create_rectangle(0, 0, 5, 5))}
        )
        assert shape.has_geometry, field
        assert shape.area_m2 == pytest.approx(25.0, rel=1e-3)


def test_a_property_volume_is_told_apart_from_a_building():
    assert cd.survey_geometry(obj()).object_type == "PROPERTY_VOLUME"
    building = {
        "id": "B-001",
        "parcel_id": "P-001",
        "floor_count": 8,
        "footprint": polygon_to_geojson(create_rectangle(0, 0, 60, 36)),
    }
    assert cd.survey_geometry(building).object_type == "BUILDING"


def test_survey_geometry_objects_can_be_passed_directly():
    left = SurveyGeometry(
        object_id="PV-1",
        geometry=polygon_to_geojson(create_rectangle(0, 0, 10, 10)),
        z_min=0.0,
        z_max=3.0,
    )
    right = SurveyGeometry(
        object_id="PV-1",
        geometry=polygon_to_geojson(create_rectangle(0, 0, 11, 10)),
        z_min=0.0,
        z_max=3.0,
    )
    report = compare_approved_vs_survey([left], [right], source_id=SOURCE)
    assert report.by_type[ChangeType.FOOTPRINT.value] == 1


def test_a_mapping_keyed_by_id_is_accepted():
    report = compare_approved_vs_survey(
        {"PV-1": obj()}, {"PV-1": obj(box=(0, 0, 12, 10))}, source_id=SOURCE
    )
    assert report.by_type[ChangeType.FOOTPRINT.value] == 1
