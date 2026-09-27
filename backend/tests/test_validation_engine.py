"""Tests for the rule-based validation engine.

Every rule gets a test that **makes it fire** and a test that it stays **silent on
the clean demo scene**. The second half matters as much as the first: the seeded
scene is a deliberate showcase that must report exactly three findings, so a rule
that fires on it has a bug.

The load-bearing properties:

* the engine's four entry points behave as advertised;
* findings carry a rule id, an issue type, a severity, both objects, geometry
  where applicable, numerical evidence, a description and a status;
* **behaviour is preserved**: the demo still reports exactly the same three
  findings, including the two pre-existing crashes.
"""
from __future__ import annotations

import pytest
from shapely.errors import GEOSException

from app.models.enums import RuleCategory
from app.repositories.memory import InMemoryRepository
from app.services import validation
from app.services.demo import seed_demo
from app.services.geometry import xy_to_ll
from app.services.validation_engine import (
    ValidationEngine,
    ValidationResult,
    ValidationRule,
    get_engine,
    get_rule_definitions,
    get_validation_summary,
    persist_results,
    run_all_rules,
    run_rule,
)

ALL_RULE_IDS = [
    "GEOM-INVALID",
    "GEOM-SELF-INTERSECTION",
    "GEOM-ZERO-AREA",
    "GEOM-ZERO-VOLUME",
    "GEOM-INVALID-Z-RANGE",
    "PARCEL-PROPERTY-OUTSIDE",
    "PARCEL-BUILDING-OUTSIDE",
    "PARCEL-UNASSIGNED-BUILDING",
    "PARCEL-UNASSIGNED-PROPERTY",
    "VERT-FLOOR-OVERLAP",
    "VERT-PROPERTY-OVERLAP",
    "VERT-FLOOR-GAP",
    "VERT-DISCONNECTED-STACK",
    "VERT-Z-ORDER",
    "VERT-FLOOR-HEIGHT",
    "CAD-DUPLICATE-ULPIN",
    "CAD-DUPLICATE-GEOMETRY",
    "CAD-MISSING-PARENT",
    "CAD-ORPHAN-PROPERTY",
    "CAD-INVALID-HIERARCHY",
    "INFRA-UTILITY-COLLISION",
    "INFRA-TUNNEL",
    "INFRA-BASEMENT",
    "INFRA-FOUNDATION",
    "INFRA-ELEVATED_STRUCTURE",
    "CHANGE-FOOTPRINT",
    "CHANGE-HEIGHT",
    "CHANGE-VOLUME",
    "CHANGE-NEW-FLOOR",
    "CHANGE-REMOVED-FLOOR",
]

#: The eight rules carried over from the original implementation. Three of them
#: fire on the seeded scene -- that is what the scene is for -- so they are
#: excluded from the "stays silent" checks below.
LEGACY_RULE_IDS = {
    "GEOM-INVALID",
    "PARCEL-PROPERTY-OUTSIDE",
    "VERT-PROPERTY-OVERLAP",
    "VERT-FLOOR-GAP",
    "VERT-DISCONNECTED-STACK",
    "VERT-Z-ORDER",
    "CAD-DUPLICATE-ULPIN",
    "INFRA-UTILITY-COLLISION",
}

#: The twenty-two rules added by this milestone.
NEW_RULE_IDS = [rule_id for rule_id in ALL_RULE_IDS if rule_id not in LEGACY_RULE_IDS]

#: The only rules that may fire on the untouched demo scene.
DEMO_ACTIVE_RULES = {
    "PARCEL-PROPERTY-OUTSIDE",
    "VERT-PROPERTY-OVERLAP",
    "INFRA-UTILITY-COLLISION",
}


@pytest.fixture
def repo():
    """A seeded demo scene, freshly built per test."""
    store = InMemoryRepository()
    seed_demo(store)
    return store


def find(repo, rule_id):
    """Stored findings produced by one rule."""
    return [i for i in repo.records("issues") if i.get("rule_id") == rule_id]


def results_for(repo, rule_id):
    """In-memory results from running one rule, without persisting."""
    return run_rule(repo, rule_id)


def square(x0, y0, x1, y1, z_min=0.0, z_max=3.2):
    """A property-shaped record at the given local-plane extent."""
    from app.services.geometry import create_rectangle, polygon_to_geojson

    return {
        "id": f"PV-T-{x0}-{y0}-{z_min}",
        "unit_label": f"Test {x0},{y0},{z_min}",
        "floor_number": 1,
        "z_min": z_min,
        "z_max": z_max,
        "geometry_3d": polygon_to_geojson(create_rectangle(x0, y0, x1, y1)),
        "area_m2": (x1 - x0) * (y1 - y0),
        "volume_m3": (x1 - x0) * (y1 - y0) * (z_max - z_min),
        "geometry_hash": f"H{x0}{y0}{z_min}{x1}{y1}{z_max}",
        "prototype_ulpin": f"VC-VP-P001-T{x0}{y0}{z_min}{x1}{y1}{z_max}",
        "parent_parcel_id": "P-001",
        "building_id": "B-001",
    }


# ==========================================================================
# The engine contract
# ==========================================================================


def test_every_rule_is_registered():
    assert [r.rule_id for r in get_engine().rules] == ALL_RULE_IDS


@pytest.mark.parametrize("rule_id", NEW_RULE_IDS)
def test_every_new_rule_stays_silent_on_the_demo_scene(repo, rule_id):
    """The showcase scene must not gain findings from the new rules.

    Parametrised over all twenty-two rather than grouped by category so a rule
    added later inherits the check.
    """
    assert results_for(repo, rule_id) == [], f"{rule_id} fired on the demo scene"


def test_the_thirty_rules_cover_the_six_categories():
    defs = get_rule_definitions()
    assert len(defs) == 30
    counts: dict[str, int] = {}
    for definition in defs:
        counts[definition["category"]] = counts.get(definition["category"], 0) + 1
    assert counts == {
        RuleCategory.GEOMETRY.value: 5,
        RuleCategory.PARCEL.value: 4,
        RuleCategory.VERTICAL.value: 6,
        RuleCategory.CADASTRAL.value: 5,
        RuleCategory.INFRASTRUCTURE.value: 5,
        RuleCategory.CHANGE.value: 5,
    }


def test_rule_definitions_are_machine_readable():
    for definition in get_rule_definitions():
        assert set(definition) == {
            "rule_id",
            "category",
            "issue_type",
            "severity",
            "description",
            "collections",
        }
        assert definition["description"]
        assert definition["collections"]


def test_run_rule_rejects_an_unknown_id(repo):
    with pytest.raises(KeyError):
        run_rule(repo, "NOT-A-RULE")


def test_run_rule_returns_only_that_rules_findings(repo):
    findings = run_rule(repo, "PARCEL-PROPERTY-OUTSIDE")
    assert findings
    assert all(f.rule_id == "PARCEL-PROPERTY-OUTSIDE" for f in findings)
    # ...and leaves the stored set untouched.
    assert len(repo.records("issues")) == 3


def test_run_all_rules_returns_findings_without_persisting(repo):
    results = run_all_rules(repo)
    assert results
    assert len(repo.records("issues")) == 3  # unchanged


def test_run_all_rules_is_deterministic(repo):
    first = [f.id for f in run_all_rules(repo)]
    second = [f.id for f in run_all_rules(repo)]
    assert first == second


def test_summary_counts_by_severity_category_and_rule(repo):
    summary = get_validation_summary(repo, results=run_all_rules(repo))
    assert summary["total"] == 3
    assert summary["rules_evaluated"] == 30
    assert sum(summary["by_severity"].values()) == 3
    assert sum(summary["by_category"].values()) == 3
    assert sum(summary["by_rule"].values()) == 3
    assert "PARCEL-PROPERTY-OUTSIDE" in summary["by_rule"]


def test_summary_can_read_the_stored_findings(repo):
    summary = get_validation_summary(repo)
    assert summary["source"] == "stored"
    assert summary["total"] == 3


def test_a_custom_engine_can_hold_a_subset_of_rules(repo):
    rule = get_engine().rule("PARCEL-PROPERTY-OUTSIDE")
    engine = ValidationEngine([rule])
    results = engine.run_all_rules(repo)
    assert results
    assert all(r.rule_id == rule.rule_id for r in results)
    assert engine.definitions()[0]["rule_id"] == rule.rule_id


def test_a_custom_rule_can_be_registered(repo):
    def always_finds(ctx):
        yield ValidationResult(
            rule_id="TEST-ALWAYS",
            issue_type="TEST",
            severity="WARNING",
            category="GEOMETRY",
            object_a="X",
            description="always",
            evidence={"n": 1},
            ident="VAL-TEST-1",
        )

    engine = ValidationEngine(
        [
            ValidationRule(
                rule_id="TEST-ALWAYS",
                category=RuleCategory.GEOMETRY,
                issue_type="TEST",
                severity="WARNING",
                description="test rule",
                check=always_finds,
            )
        ]
    )
    results = engine.run_all_rules(repo)
    assert [r.id for r in results] == ["VAL-TEST-1"]


# ==========================================================================
# Behaviour preservation
# ==========================================================================


def test_demo_scene_still_reports_exactly_three_findings(repo):
    assert len(repo.records("issues")) == 3
    assert sum(i["severity"] == "CRITICAL" for i in repo.records("issues")) == 2
    assert sum(i["severity"] == "WARNING" for i in repo.records("issues")) == 1


def test_the_three_deliberate_findings_are_unchanged(repo):
    by_id = {i["id"]: i for i in repo.records("issues")}
    assert set(by_id) == {
        "VAL-OUT-PV-502",
        "VAL-OVR-PV-201-PV-202",
        "VAL-INF-PV-B001",
    }
    assert by_id["VAL-OUT-PV-502"]["issue_type"] == "OUTSIDE_PARENT_PARCEL"
    assert by_id["VAL-OUT-PV-502"]["object_b"] == "P-001"
    assert by_id["VAL-OVR-PV-201-PV-202"]["overlap_volume"] == pytest.approx(
        575.83, abs=0.01
    )
    assert by_id["VAL-INF-PV-B001"]["overlap_volume"] == pytest.approx(387.43, abs=0.01)


def test_no_new_rule_fires_on_the_demo_scene(repo):
    """The twenty-two new rules must stay silent on the showcase scene."""
    fired = {i.get("rule_id") for i in repo.records("issues")}
    assert fired == {
        "PARCEL-PROPERTY-OUTSIDE",
        "VERT-PROPERTY-OVERLAP",
        "INFRA-UTILITY-COLLISION",
    }


def test_every_finding_carries_its_rule_and_evidence(repo):
    for issue in repo.records("issues"):
        assert issue["rule_id"]
        assert issue["category"] in {c.value for c in RuleCategory}
        assert issue["evidence"]
        assert issue["status"] == "OPEN"
        assert issue["description"]


def test_validate_still_replaces_previous_findings(repo):
    repo.records("issues").append({"id": "STALE"})
    validation.validate(repo)
    assert "STALE" not in [i["id"] for i in repo.records("issues")]


def test_validate_summary_shape_is_unchanged(repo):
    assert validation.validate(repo) == {"issues": 3, "critical": 2, "warning": 1}


def test_overlap_detection_is_still_same_floor_only(repo):
    """Pre-existing limitation, preserved: inter-floor overlaps are not detected."""
    a = repo.records("properties")[1]
    b = repo.records("properties")[3]
    b["geometry_3d"] = a["geometry_3d"]
    b["z_min"], b["z_max"] = a["z_min"], a["z_max"]
    validation.validate(repo)
    assert not any(
        i["issue_type"] == "VERTICAL_VOLUME_OVERLAP"
        and {i["object_a"], i["object_b"]} == {"PV-101", "PV-202"}
        for i in repo.records("issues")
    )


def test_invalid_geometry_is_still_appended_then_raises(repo):
    """Pre-existing crash, preserved by the rule order."""
    target = repo.records("properties")[1]
    bowtie = [[0, 0], [1, 1], [1, 0], [0, 1], [0, 0]]
    target["geometry_3d"] = {
        "type": "Polygon",
        "coordinates": [[xy_to_ll(x, y) for x, y in bowtie]],
    }
    with pytest.raises(GEOSException):
        validation.validate(repo)
    assert any(
        i["issue_type"] == "INVALID_GEOMETRY" for i in repo.records("issues")
    )


def test_edge_touching_duplicate_ulpin_still_raises(repo):
    """Pre-existing crash, preserved: a LineString intersection is serialised."""
    props = repo.records("properties")
    props[2]["prototype_ulpin"] = props[1]["prototype_ulpin"]
    with pytest.raises(AttributeError):
        validation.validate(repo)


def test_empty_store_still_raises_index_error():
    """Pre-existing limitation, preserved: rules index parcels[0]."""
    with pytest.raises(IndexError):
        validation.validate(InMemoryRepository())


# ==========================================================================
# GEOMETRY
# ==========================================================================


def test_geom_invalid_geometry(repo):
    target = repo.records("properties")[1]
    bowtie = [[0, 0], [1, 1], [1, 0], [0, 1], [0, 0]]
    target["geometry_3d"] = {
        "type": "Polygon",
        "coordinates": [[xy_to_ll(x, y) for x, y in bowtie]],
    }
    results = results_for(repo, "GEOM-INVALID")
    assert any(r.object_a == target["id"] for r in results)
    assert all(r.severity == "CRITICAL" for r in results)


def test_geom_self_intersection(repo):
    target = repo.records("properties")[1]
    bowtie = [[0, 0], [10, 10], [10, 0], [0, 10], [0, 0]]
    target["geometry_3d"] = {
        "type": "Polygon",
        "coordinates": [[xy_to_ll(x, y) for x, y in bowtie]],
    }
    results = results_for(repo, "GEOM-SELF-INTERSECTION")
    assert any(r.object_a == target["id"] for r in results)
    assert results[0].evidence["is_simple"] is False


def test_geom_zero_area(repo):
    repo.records("properties")[1]["geometry_3d"] = {
        "type": "Polygon",
        "coordinates": [[xy_to_ll(5, 5), xy_to_ll(5, 5), xy_to_ll(5, 5), xy_to_ll(5, 5)]],
    }
    results = results_for(repo, "GEOM-ZERO-AREA")
    assert any(r.object_a == "PV-101" for r in results)
    assert results[0].evidence["area_m2"] <= 0.01


def test_geom_zero_volume(repo):
    target = repo.records("properties")[1]
    target["z_min"] = target["z_max"] = 3.2
    results = results_for(repo, "GEOM-ZERO-VOLUME")
    assert any(r.object_a == target["id"] for r in results)
    assert results[0].evidence["volume_m3"] <= 0.01


def test_geom_invalid_z_range(repo):
    target = repo.records("properties")[1]
    target["z_min"], target["z_max"] = 10.0, 10.0
    results = results_for(repo, "GEOM-INVALID-Z-RANGE")
    assert any(r.object_a == target["id"] for r in results)
    assert results[0].evidence == {"z_min": 10.0, "z_max": 10.0}


# ==========================================================================
# PARCEL
# ==========================================================================


def test_parcel_property_outside(repo):
    results = results_for(repo, "PARCEL-PROPERTY-OUTSIDE")
    assert any(r.object_a == "PV-502" for r in results)
    finding = next(r for r in results if r.object_a == "PV-502")
    assert finding.object_b == "P-001"
    assert finding.evidence["outside_area_m2"] > 0.01
    assert finding.geometry is not None


def test_parcel_building_outside(repo):
    from app.services.geometry import create_rectangle, polygon_to_geojson

    repo.records("buildings")[0]["footprint"] = polygon_to_geojson(
        create_rectangle(-40, -40, 200, 200)
    )
    results = results_for(repo, "PARCEL-BUILDING-OUTSIDE")
    assert results
    assert results[0].evidence["outside_area_m2"] > 0.01


def test_parcel_unassigned_building(repo):
    repo.records("buildings")[0]["parcel_id"] = None
    results = results_for(repo, "PARCEL-UNASSIGNED-BUILDING")
    assert results
    assert results[0].object_b == "P-001"
    assert results[0].evidence["containing_parcel_id"] == "P-001"


def test_parcel_unassigned_property(repo):
    repo.records("properties")[1]["parent_parcel_id"] = None
    results = results_for(repo, "PARCEL-UNASSIGNED-PROPERTY")
    assert any(r.object_a == "PV-101" for r in results)


# ==========================================================================
# VERTICAL
# ==========================================================================


def test_vertical_floor_overlap(repo):
    from app.services.geometry import create_rectangle, polygon_to_geojson

    plate = polygon_to_geojson(create_rectangle(10, 8, 70, 44))
    for index, number in enumerate((4, 5), start=1):
        repo.add(
            "floors",
            {
                "id": f"FLOOR-G{index}",
                "building_id": "B-001",
                "floor_number": number,
                "z_min": 9.6 + index,
                "z_max": 12.8 + index,
                "geometry_3d": plate,
            },
        )
    results = results_for(repo, "VERT-FLOOR-OVERLAP")
    assert results
    assert results[0].evidence["overlap_volume_m3"] > 0.01
    assert results[0].geometry is not None


def test_vertical_floor_overlap_ignores_bands_without_geometry(repo):
    """The demo's floor stack is bands with no footprint; it cannot overlap."""
    assert results_for(repo, "VERT-FLOOR-OVERLAP") == []


def test_vertical_property_overlap(repo):
    results = results_for(repo, "VERT-PROPERTY-OVERLAP")
    assert any(
        {r.object_a, r.object_b} == {"PV-201", "PV-202"} for r in results
    )
    finding = next(
        r for r in results if {r.object_a, r.object_b} == {"PV-201", "PV-202"}
    )
    assert finding.evidence["overlap_volume_m3"] == pytest.approx(575.83, abs=0.01)
    assert finding.location == "B-001 / Floor 2"


def test_vertical_floor_gap(repo):
    for p in repo.records("properties"):
        if p["floor_number"] == 3:
            p["z_min"] += 2.0
            p["z_max"] += 2.0
    results = results_for(repo, "VERT-FLOOR-GAP")
    assert results
    assert results[0].evidence["gap_m"] > 0.05
    assert results[0].object_b.startswith("FLOOR-")


def test_vertical_disconnected_stack(repo):
    """Move floor 3's footprint somewhere it touches no other storey."""
    for p in repo.records("properties"):
        if p["floor_number"] == 3:
            p["geometry_3d"] = {
                "type": "Polygon",
                "coordinates": [
                    [
                        xy_to_ll(0, 60),
                        xy_to_ll(2, 60),
                        xy_to_ll(2, 62),
                        xy_to_ll(0, 62),
                        xy_to_ll(0, 60),
                    ]
                ],
            }
    results = results_for(repo, "VERT-DISCONNECTED-STACK")
    assert results
    assert results[0].evidence["shared_area_m2"] < 0.01


def test_vertical_z_order(repo):
    target = repo.records("properties")[1]
    target["z_min"], target["z_max"] = 10.0, 10.0
    results = results_for(repo, "VERT-Z-ORDER")
    assert any(r.object_a == target["id"] for r in results)


def test_vertical_floor_height(repo):
    """A double-height storey among uniform ones."""
    repo.records("floors").clear()
    for number, height in ((1, 3.2), (2, 3.2), (3, 9.6), (4, 3.2), (5, 3.2)):
        repo.add(
            "floors",
            {
                "id": f"FLOOR-H{number}",
                "building_id": "B-001",
                "floor_number": number,
                "z_min": sum((3.2, 3.2, 9.6, 3.2, 3.2)[: number - 1]),
                "z_max": sum((3.2, 3.2, 9.6, 3.2, 3.2)[: number]),
            },
        )
    results = results_for(repo, "VERT-FLOOR-HEIGHT")
    assert any(r.object_a == "FLOOR-3" for r in results)
    finding = next(r for r in results if r.object_a == "FLOOR-3")
    assert finding.evidence["floor_height_m"] == pytest.approx(9.6)
    assert finding.evidence["median_height_m"] == pytest.approx(3.2)
    assert finding.evidence["deviation_ratio"] > 0.30


def test_vertical_floor_height_needs_a_distribution(repo):
    """One or two storeys have no median to be inconsistent with."""
    repo.records("floors").clear()
    repo.add("floors", {"id": "F1", "building_id": "B-001", "floor_number": 1,
                        "z_min": 0.0, "z_max": 9.0})
    assert results_for(repo, "VERT-FLOOR-HEIGHT") == []


# ==========================================================================
# CADASTRAL
# ==========================================================================


def test_cadastral_duplicate_ulpin(repo):
    props = repo.records("properties")
    props[2]["prototype_ulpin"] = props[1]["prototype_ulpin"]
    props[2]["geometry_3d"] = props[1]["geometry_3d"]
    props[2]["z_min"] = props[1]["z_min"]
    props[2]["z_max"] = props[1]["z_max"]
    results = results_for(repo, "CAD-DUPLICATE-ULPIN")
    assert results
    assert results[0].evidence["overlap_volume_m3"] > 0.01
    assert "VC-VP-" in results[0].evidence["prototype_ulpin"]


def test_cadastral_duplicate_geometry(repo):
    props = repo.records("properties")
    props[2]["geometry_hash"] = props[1]["geometry_hash"]
    results = results_for(repo, "CAD-DUPLICATE-GEOMETRY")
    assert results
    assert results[0].evidence["geometry_hash"] == props[1]["geometry_hash"]


def test_cadastral_missing_parent(repo):
    repo.records("properties")[1]["parent_parcel_id"] = "P-NOPE"
    results = results_for(repo, "CAD-MISSING-PARENT")
    assert any(r.object_a == "PV-101" for r in results)
    assert results[0].object_b == "P-NOPE"


def test_cadastral_orphan_property(repo):
    repo.records("properties")[1]["building_id"] = "B-NOPE"
    results = results_for(repo, "CAD-ORPHAN-PROPERTY")
    assert any(r.object_a == "PV-101" for r in results)
    assert results[0].object_b == "B-NOPE"


def test_cadastral_invalid_hierarchy(repo):
    """PV-101 claims floor 1 and occupies floor 1's band, so it is consistent."""
    assert results_for(repo, "CAD-INVALID-HIERARCHY") == []
    target = repo.records("properties")[1]
    target["z_min"], target["z_max"] = 40.0, 43.2
    results = results_for(repo, "CAD-INVALID-HIERARCHY")
    assert any(r.object_a == target["id"] for r in results)
    assert results[0].evidence["declared_floor_band"] == [0.0, 3.2]
    assert results[0].evidence["floor_number"] == 1


def test_cadastral_invalid_hierarchy_allows_a_basement(repo):
    """PV-B001 sits below the lowest storey, which is correct, not a violation."""
    assert not any(
        r.object_a == "PV-B001" for r in results_for(repo, "CAD-INVALID-HIERARCHY")
    )


# ==========================================================================
# INFRASTRUCTURE
# ==========================================================================


def test_infra_underground_utility_collision(repo):
    results = results_for(repo, "INFRA-UTILITY-COLLISION")
    assert any(r.object_a == "PV-B001" for r in results)
    finding = next(r for r in results if r.object_a == "PV-B001")
    assert finding.evidence["overlap_volume_m3"] == pytest.approx(387.43, abs=0.01)
    assert finding.evidence["infrastructure_type"] == "UNDERGROUND_UTILITY_CORRIDOR"


@pytest.mark.parametrize(
    ("rule_id", "infra_type"),
    [
        ("INFRA-TUNNEL", "TUNNEL"),
        ("INFRA-BASEMENT", "BASEMENT"),
        ("INFRA-FOUNDATION", "FOUNDATION"),
        ("INFRA-ELEVATED_STRUCTURE", "ELEVATED_STRUCTURE"),
    ],
)
def test_typed_infrastructure_collisions(repo, rule_id, infra_type):
    infra = dict(repo.records("infrastructure")[0])
    infra["id"] = f"INF-{infra_type}"
    infra["type"] = infra_type
    repo.add("infrastructure", infra)
    results = results_for(repo, rule_id)
    assert any(r.object_a == "PV-B001" for r in results), rule_id
    assert results[0].evidence["infrastructure_type"] == infra_type
    assert results[0].evidence["overlap_volume_m3"] > 0.01


# ==========================================================================
# CHANGE
# ==========================================================================


def _add_version(repo, object_id, *, geometry=None, z_min=None, z_max=None,
                 version=1, geometry_hash="OLDHASH"):
    from app.services.geometry import create_rectangle, polygon_to_geojson

    repo.add(
        "geometry_versions",
        {
            "id": f"GV-{object_id}-{version}",
            "object_id": object_id,
            "object_type": "property",
            "version": version,
            "z_min": z_min,
            "z_max": z_max,
            "geometry_hash": geometry_hash,
            "geometry": geometry
            or polygon_to_geojson(create_rectangle(10, 8, 40, 44)),
            "created_at": "2026-01-01T00:00:00+00:00",
        },
    )


def test_change_footprint(repo):
    _add_version(repo, "PV-101", geometry_hash="DIFFERENT")
    results = results_for(repo, "CHANGE-FOOTPRINT")
    assert any(r.object_a == "PV-101" for r in results)
    assert results[0].evidence["previous_geometry_hash"] == "DIFFERENT"


def test_change_height(repo):
    _add_version(repo, "PV-101", z_min=0.0, z_max=9.9)
    results = results_for(repo, "CHANGE-HEIGHT")
    assert any(r.object_a == "PV-101" for r in results)
    assert results[0].evidence["previous_height_m"] == pytest.approx(9.9)
    assert results[0].evidence["delta_m"] != 0


def test_change_volume(repo):
    _add_version(repo, "PV-101", z_min=0.0, z_max=0.5)
    results = results_for(repo, "CHANGE-VOLUME")
    assert any(r.object_a == "PV-101" for r in results)
    assert results[0].evidence["previous_volume_m3"] < results[0].evidence[
        "current_volume_m3"
    ]


def test_change_new_floor(repo):
    repo.records("buildings")[0]["floor_count"] = 1
    results = results_for(repo, "CHANGE-NEW-FLOOR")
    assert results
    assert results[0].evidence["declared_floor_count"] == 1
    assert results[0].evidence["floor_number"] > 1


def test_change_removed_floor(repo):
    repo.records("buildings")[0]["floor_count"] = 20
    results = results_for(repo, "CHANGE-REMOVED-FLOOR")
    assert results
    assert all(r.severity == "INFO" for r in results)
    assert results[0].evidence["declared_floor_count"] == 20


def test_change_rules_are_info_not_critical(repo):
    """A revision is a note for a reviewer, not a defect in the cadastre."""
    _add_version(repo, "PV-101", geometry_hash="X", z_min=0.0, z_max=1.0)
    for rule_id in ("CHANGE-FOOTPRINT", "CHANGE-HEIGHT", "CHANGE-VOLUME"):
        results = results_for(repo, rule_id)
        assert results, rule_id
        assert all(r.severity == "INFO" for r in results), rule_id


# ==========================================================================
# The issue record
# ==========================================================================


def test_issue_record_carries_every_required_field(repo):
    record = results_for(repo, "PARCEL-PROPERTY-OUTSIDE")[0].to_issue_record()
    for field in (
        "rule_id",
        "issue_type",
        "severity",
        "object_a",
        "object_b",
        "geometry",
        "evidence",
        "description",
        "status",
    ):
        assert field in record, field
    assert record["rule_id"] == "PARCEL-PROPERTY-OUTSIDE"
    assert record["geometry"]["type"] == "Polygon"
    assert record["evidence"]["outside_area_m2"] > 0


def test_issue_record_keeps_the_legacy_fields(repo):
    """The original eight fields are the stored-record contract."""
    record = results_for(repo, "PARCEL-PROPERTY-OUTSIDE")[0].to_issue_record()
    for field in (
        "id",
        "object_a",
        "object_b",
        "issue_type",
        "severity",
        "overlap_volume",
        "gap_m",
        "description",
        "geometry",
        "status",
        "location",
    ):
        assert field in record, field
    assert record["gap_m"] is None
    assert record["overlap_volume"] == 0.0


def test_gap_is_carried_in_both_places(repo):
    for p in repo.records("properties"):
        if p["floor_number"] == 3:
            p["z_min"] += 2.0
            p["z_max"] += 2.0
    record = results_for(repo, "VERT-FLOOR-GAP")[0].to_issue_record()
    assert record["gap_m"] == pytest.approx(2.0, abs=0.05)
    assert record["evidence"]["gap_m"] == record["gap_m"]


def test_overlap_is_carried_in_both_places(repo):
    record = results_for(repo, "VERT-PROPERTY-OVERLAP")[0].to_issue_record()
    assert record["overlap_volume"] == pytest.approx(575.83, abs=0.01)
    assert record["evidence"]["overlap_volume_m3"] == record["overlap_volume"]


def test_status_defaults_to_open():
    result = ValidationResult(
        rule_id="X",
        issue_type="Y",
        severity="WARNING",
        category="GEOMETRY",
        object_a="A",
        description="d",
    )
    assert result.status == "OPEN"
    assert result.to_issue_record()["status"] == "OPEN"


def test_persist_results_replaces_the_stored_findings(repo):
    """The batch path a caller gets when it chooses what to store."""
    stored = persist_results(repo, run_all_rules(repo))
    assert len(stored) == 3
    assert {i["id"] for i in repo.records("issues")} == {
        "VAL-OUT-PV-502",
        "VAL-OVR-PV-201-PV-202",
        "VAL-INF-PV-B001",
    }


def test_validate_with_results_does_not_persist(repo):
    findings = validation.validate_with_results(repo)
    assert len(findings) == 3
    assert all(isinstance(f, ValidationResult) for f in findings)
    assert all(f.evidence for f in findings)
    assert len(repo.records("issues")) == 3  # the seed's, not a second set
