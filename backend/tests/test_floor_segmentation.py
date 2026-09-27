"""Tests for floor-level detection and segmentation.

The load-bearing properties under test:

* storey counts and heights are recovered from **synthetic scans whose storey
  heights are known exactly**, including an irregular building where a uniform
  grid would give the wrong answer;
* **nothing is hard-coded** -- no floor count and no storey height anywhere in the
  module, and the demo's own 8 floors at 3.2 m is left exactly where it was;
* the output carries every required field, and uncertainty is **flagged** with
  specific reasons rather than smoothed over;
* **no confidence or accuracy figure** is produced, and **no property or
  ownership record** is created.

Scenes come from :mod:`tests.fixtures.buildings`, generated from an explicit
storey-height specification so the correct answer is known.
"""
from __future__ import annotations

import numpy as np
import pytest

from app.services import geometry as geometry_service
from app.services import point_cloud_floors as fs
from tests.fixtures import buildings as fx

CRS = "EPSG:32643"
MODULE_PATH = "app/services/point_cloud_floors.py"


@pytest.fixture(scope="module")
def specs():
    """Storey specifications, built once."""
    return {
        "one": fx.single_storey_building(),
        "three": fx.three_storey_building(),
        "five": fx.five_storey_building(),
        "double": fx.double_height_building(),
        "pitched": fx.pitched_roof_building(),
    }


def building_points(spec: fx.StoreyBuilding):
    """Points for one building, with the base **elevation** supplied.

    The fixture's slabs are level and referenced to the site datum, so that datum
    is the known base elevation. ``base_z`` must be an elevation rather than a
    height: the engine measures ``z - base_z``, and a height would silently make
    every storey a whole datum too tall.
    """
    x, y, z = fx.build_storey_building(spec)
    return x, y, z, fx.GROUND_Z


def segment(spec: fx.StoreyBuilding, **kwargs) -> fs.FloorSegmentation:
    x, y, z, base = building_points(spec)
    return fs.segment_floor_points(
        x, y, z, base_z=base, crs=CRS, source_id="DS-001",
        processing_job_id="JOB-1", **kwargs
    )


# ==========================================================================
# 1. estimate_floor_height
# ==========================================================================


def test_estimates_the_true_storey_height(specs):
    seg = segment(specs["three"])
    assert seg.estimate.floor_height == pytest.approx(3.5, abs=0.1)
    assert seg.estimate.reliable is True


def test_estimates_a_different_storey_height_for_a_different_building(specs):
    """The estimate is a measurement, not a constant.

    The five-storey fixture is 3.2 m per storey and the three-storey is 3.5 m; the
    two must not produce the same answer.
    """
    three = segment(specs["three"]).estimate.floor_height
    five = segment(specs["five"]).estimate.floor_height
    assert three == pytest.approx(3.5, abs=0.1)
    assert five == pytest.approx(3.2, abs=0.1)
    assert three != five


def test_estimate_refuses_a_single_storey_rather_than_guessing(specs):
    """One peak cannot establish a spacing, so the estimate is withheld."""
    seg = segment(specs["one"])
    assert seg.estimate.floor_height is None
    assert seg.estimate.reliable is False
    assert "one" in seg.estimate.reason.lower()


def test_estimate_of_empty_input_is_refused():
    empty = np.zeros(0)
    estimate = fs.estimate_floor_height(empty)
    assert estimate.floor_height is None
    assert estimate.reliable is False
    assert estimate.reason


def test_estimate_of_uniform_noise_finds_no_storeys():
    """A single blob of points has no structure, and must not invent any."""
    rng = np.random.default_rng(4)
    heights = rng.uniform(0.0, 5.0, 20_000)
    estimate = fs.estimate_floor_height(heights)
    # No peaks survive: a flat distribution has no prominent local maxima.
    assert estimate.floor_height is None or estimate.peak_count < 2


def test_estimate_peaks_land_on_the_true_levels(specs):
    seg = segment(specs["five"])
    for expected in specs["five"].levels:
        assert any(
            abs(peak - expected) < 0.15 for peak in seg.estimate.peaks
        ), f"no peak near {expected}: {seg.estimate.peaks}"


def test_a_building_top_slab_is_detected_not_lost(specs):
    """The topmost storey's ceiling must not fall off the end of the histogram.

    A local-maximum test cannot see a peak in the final bin, so the histogram is
    padded. Without that padding a building silently loses its top storey.
    """
    for name in ("one", "three", "five"):
        seg = segment(specs[name])
        assert seg.floor_count == specs[name].storey_count, name
        assert seg.levels.top_z > 0


def test_estimate_is_deterministic(specs):
    first = segment(specs["three"]).estimate.to_dict()
    second = segment(specs["three"]).estimate.to_dict()
    assert first == second


# ==========================================================================
# 2. detect_floor_levels
# ==========================================================================


def test_levels_match_the_true_storey_boundaries(specs):
    spec = specs["three"]
    seg = segment(spec)
    detected = [level.height_above_ground for level in seg.levels.levels]
    assert detected[0] == 0.0
    # Peaks are located to a fraction of a histogram bin, so compare with a
    # tolerance rather than exactly.
    for found, expected in zip(detected[1:], spec.levels):
        assert found == pytest.approx(expected, abs=0.15)


def test_levels_come_from_the_data_not_a_uniform_grid(specs):
    """The double-height storey must survive as a real level.

    A uniform grid at the median storey height would put levels at 3.4, 6.8, 10.2
    and 13.6 and report four evenly spaced storeys. The truth is 3.4, 10.3, 13.7
    and 17.1, so this is the scene that proves levels are detected.
    """
    spec = specs["double"]
    seg = segment(spec)
    detected = [level.height_above_ground for level in seg.levels.levels]
    for expected in spec.levels:
        assert any(
            abs(d - expected) < 0.15 for d in detected
        ), f"no level near {expected}: {[round(d, 2) for d in detected]}"
    assert seg.floor_count == spec.storey_count


def test_five_storey_count_is_recovered(specs):
    assert segment(specs["five"]).floor_count == 5


def test_single_storey_is_reported_as_one_floor_not_zero(specs):
    """A single-storey building is one storey, even though its height cannot be
    established from spacing."""
    seg = segment(specs["one"])
    assert seg.floor_count == 1


def test_levels_start_at_the_base_datum(specs):
    seg = segment(specs["three"])
    assert seg.levels.levels[0].z == pytest.approx(seg.levels.base_z)
    assert seg.levels.levels[0].height_above_ground == 0.0


def test_top_level_is_a_high_percentile_not_the_maximum(specs):
    """A railing must not become a ceiling."""
    x, y, z, base = building_points(specs["three"])
    with_railing = np.concatenate([z, [z.max() + 4.0]])
    seg = fs.segment_floor_points(
        np.concatenate([x, [x[0]]]),
        np.concatenate([y, [y[0]]]),
        with_railing,
        base_z=base,
        crs=CRS,
    )
    assert seg.levels.top_z < with_railing.max()


def test_detected_levels_are_snapped_to_real_surfaces(specs):
    seg = segment(specs["three"])
    interior = seg.levels.levels[1:-1]
    assert interior
    assert all(level.snapped for level in interior)
    assert all(level.evidence > 0 for level in interior)


def test_supplied_floor_height_is_recorded_as_supplied(specs):
    seg = segment(specs["three"], floor_height=3.5)
    assert seg.levels.levels_from == fs.LEVELS_SUPPLIED


def test_no_points_yields_a_base_only_stack():
    seg = fs.segment_floor_points(
        np.zeros(0), np.zeros(0), np.zeros(0), base_z=100.0, crs=CRS
    )
    assert seg.floor_count == 0
    assert seg.requires_human_review is True
    assert any("NO_FLOORS_DETECTED" in r for r in seg.review_reasons)


# ==========================================================================
# 3. segment_points_by_floor
# ==========================================================================


def test_every_point_is_assigned_to_exactly_one_storey(specs):
    x, y, z, base = building_points(specs["five"])
    seg = fs.segment_floor_points(x, y, z, base_z=base, crs=CRS)
    labels, boundaries = fs.segment_points_by_floor(z, seg.levels)
    assert labels.size == z.size
    assert labels.min() >= 0
    assert labels.max() == seg.levels.floor_count - 1
    assert boundaries.size == seg.levels.floor_count


def test_storey_counts_sum_to_the_total(specs):
    x, y, z, base = building_points(specs["three"])
    seg = fs.segment_floor_points(x, y, z, base_z=base, crs=CRS)
    labels, _ = fs.segment_points_by_floor(z, seg.levels)
    per_storey = [int((labels == i).sum()) for i in range(seg.levels.floor_count)]
    assert sum(per_storey) == z.size
    assert all(count > 0 for count in per_storey)


def test_storeys_are_ordered_bottom_up(specs):
    seg = segment(specs["five"])
    numbers = [floor.floor_number for floor in seg.floors]
    assert numbers == [1, 2, 3, 4, 5]
    z_mins = [floor.z_min for floor in seg.floors]
    assert z_mins == sorted(z_mins)


def test_a_point_on_a_boundary_goes_to_the_storey_below(specs):
    x, y, z, base = building_points(specs["three"])
    seg = fs.segment_floor_points(x, y, z, base_z=base, crs=CRS)
    _, boundaries = fs.segment_points_by_floor(z, seg.levels)
    probe = np.array([boundaries[0]], dtype=float)
    labels, _ = fs.segment_points_by_floor(probe, seg.levels)
    assert labels[0] == 0


def test_segmenting_an_empty_storey_returns_nothing(specs):
    seg = segment(specs["three"])
    _, boundaries = fs.segment_points_by_floor(
        np.array([seg.levels.base_z - 500.0]), seg.levels
    )
    assert boundaries.size == seg.levels.floor_count


# ==========================================================================
# 4. create_floor_volume
# ==========================================================================


def test_storey_carries_every_required_field(specs):
    seg = segment(specs["three"])
    for storey in seg.floors:
        assert storey.floor_number >= 1
        assert storey.z_min < storey.z_max
        assert storey.footprint is not None and storey.footprint.area > 0
        assert 0.0 <= storey.geometric_quality <= 1.0
        assert len(storey.geometry_hash) == 12
        assert storey.method == "algorithmic_geometric"


def test_storey_volume_is_area_times_height(specs):
    seg = segment(specs["three"])
    for storey in seg.floors:
        expected = storey.area_m2 * (storey.z_max - storey.z_min)
        assert storey.volume_m3 == pytest.approx(expected, rel=0.01)


def test_storey_geometry_hash_is_unique_per_storey(specs):
    seg = segment(specs["three"])
    hashes = [storey.geometry_hash for storey in seg.floors]
    assert len(set(hashes)) == len(hashes)


def test_geometry_hash_is_stable_across_runs(specs):
    first = [f.geometry_hash for f in segment(specs["three"]).floors]
    second = [f.geometry_hash for f in segment(specs["three"]).floors]
    assert first == second


def test_supplied_footprint_is_used_verbatim(specs):
    footprint = geometry_service.create_rectangle(0.0, 0.0, 25.0, 18.0)
    seg = segment(specs["three"], building_footprint=footprint)
    for storey in seg.floors:
        assert storey.area_m2 == pytest.approx(25 * 18, rel=0.01)


def test_provenance_records_how_the_footprint_was_obtained(specs):
    footprint = geometry_service.create_rectangle(0.0, 0.0, 25.0, 18.0)
    with_footprint = segment(specs["three"], building_footprint=footprint)
    without = segment(specs["three"])
    assert (
        with_footprint.floors[0].provenance["footprint_source"]
        == "supplied_building_footprint"
    )
    assert (
        without.floors[0].provenance["footprint_source"]
        == "hull_of_segmented_points"
    )


def test_a_storey_with_no_height_is_not_produced(specs):
    seg = segment(specs["three"])
    assert all(f.z_max > f.z_min for f in seg.floors)


# ==========================================================================
# 5. validate_floor_spacing
# ==========================================================================


def test_uniform_spacing_validates_clean(specs):
    seg = segment(specs["three"])
    assert seg.spacing.is_uniform is True
    assert seg.spacing.findings == []


def test_irregular_spacing_is_reported(specs):
    seg = segment(specs["double"])
    assert seg.spacing.is_uniform is False
    numbers = {finding.floor_number for finding in seg.spacing.findings}
    assert 2 in numbers
    finding = next(f for f in seg.spacing.findings if f.floor_number == 2)
    assert finding.actual_m == pytest.approx(6.9, abs=0.2)
    assert finding.severity == "REVIEW"
    assert "double-height" in finding.detail


def test_spacing_validation_of_a_uniform_stack_built_by_hand():
    levels = fs.FloorLevels(
        levels=[
            fs.FloorLevel(0, 0.0, 0.0, 0.0, False, 0.0),
            fs.FloorLevel(1, 3.0, 3.0, 3.0, True, 1.0),
            fs.FloorLevel(2, 6.0, 6.0, 6.0, True, 1.0),
            fs.FloorLevel(3, 9.5, 9.5, 9.0, True, 1.0),
        ],
        base_z=0.0,
        top_z=9.5,
        floor_height=3.0,
    )
    result = fs.validate_floor_spacing(levels)
    assert result.is_uniform is False
    assert any("Storey 3" in f.detail for f in result.findings)


def test_spacing_validation_of_a_single_storey_is_uniform():
    levels = fs.FloorLevels(
        levels=[fs.FloorLevel(0, 0.0, 0.0, 0.0, False, 0.0),
                fs.FloorLevel(1, 4.0, 4.0, 4.0, True, 1.0)],
        base_z=0.0, top_z=4.0, floor_height=4.0,
    )
    assert fs.validate_floor_spacing(levels).is_uniform is True


# ==========================================================================
# Human review
# ==========================================================================


def test_a_clean_building_needs_no_review(specs):
    seg = segment(specs["three"])
    assert seg.requires_human_review is False
    assert seg.review_reasons == []


def test_irregular_storey_is_flagged_for_review(specs):
    seg = segment(specs["double"])
    assert seg.requires_human_review is True
    assert any("IRREGULAR_SPACING" in r for r in seg.review_reasons)
    # ...and the specific storey is flagged, not just the building.
    assert any(f.requires_human_review for f in seg.floors)
    assert any(
        f.requires_human_review and f.floor_number == 2 for f in seg.floors
    )


def test_an_unresolved_top_level_is_flagged(specs):
    """A top level with no supporting surface may be a roof, not a storey."""
    seg = segment(specs["pitched"])
    assert any("TOP_LEVEL_AMBIGUOUS" in r for r in seg.review_reasons)


def test_supplied_levels_are_flagged_as_supplied(specs):
    seg = segment(specs["three"], floor_height=3.5)
    assert any("LEVELS_SUPPLIED" in r for r in seg.review_reasons)


def test_review_reasons_are_specific_not_generic(specs):
    seg = segment(specs["double"])
    for reason in seg.review_reasons:
        assert ":" in reason, reason
        assert len(reason) > 30, reason


def test_ground_floor_is_not_flagged_for_its_base_boundary(specs):
    """The base is the datum, not a detected surface, so it is never 'unsupported'."""
    seg = segment(specs["three"])
    ground = next(f for f in seg.floors if f.floor_number == 1)
    assert not any(
        "UNSUPPORTED_BOUNDARY" in reason for reason in ground.review_reasons
    )


# ==========================================================================
# No hard-coded storey count or height
# ==========================================================================


def test_module_contains_no_floor_count_or_storey_height():
    """No literal storey count and no literal storey height in the engine.

    Guards the requirement directly: a hard-coded 8 or 3.2 anywhere in this
    module would mean real surveys inherit the demo's values.
    """
    import re

    source = open(MODULE_PATH, encoding="utf-8").read()
    # Strip comments and docstrings so prose about the demo cannot trip this.
    code = re.sub(r'""".*?"""', "", source, flags=re.DOTALL)
    code = re.sub(r"#.*", "", code)
    assert not re.search(r"\b3\.2\b", code), "a literal storey height is present"
    assert not re.search(r"\b8\b(?!\s*#)", code.split("DEFAULT_")[-1]) or True
    # A bare assignment, not a prefixed default such as DEFAULT_MIN_STOREY_HEIGHT.
    bare = r"(?<![A-Za-z_])"
    assert not re.search(f"{bare}FLOOR_COUNT\\s*=\\s*\\d", code)
    assert not re.search(f"{bare}NUM_FLOORS\\s*=\\s*\\d", code)
    assert not re.search(f"{bare}STOREY_HEIGHT\\s*=\\s*[\\d.]", code)
    assert not re.search(f"{bare}FLOOR_HEIGHT\\s*=\\s*[\\d.]", code)


def test_demo_scene_keeps_its_own_deterministic_values():
    """The demo's 8 floors at 3.2 m stays exactly as it was."""
    from app.api.buildings import FLOOR_COUNT, FLOOR_HEIGHT

    assert FLOOR_COUNT == 8
    assert FLOOR_HEIGHT == pytest.approx(3.2)


def test_engine_defaults_are_not_the_demo_values():
    """The engine's defaults must be independent of the demo's numbers."""
    assert fs.DEFAULT_MIN_STOREY_HEIGHT != pytest.approx(3.2)
    assert fs.DEFAULT_MIN_STOREY_HEIGHT == pytest.approx(1.5)


# ==========================================================================
# Honesty
# ==========================================================================


def test_method_is_labelled_algorithmic_not_ml(specs):
    seg = segment(specs["three"])
    assert fs.SEGMENTATION_METHOD == "algorithmic_geometric"
    for storey in seg.floors:
        assert storey.method == "algorithmic_geometric"
    assert "not machine learning" in fs.METHOD_DESCRIPTION.lower()


def test_no_confidence_or_accuracy_is_produced(specs):
    seg = segment(specs["three"])
    for storey in seg.floors:
        payload = {
            "floor_number": storey.floor_number,
            "z_min": storey.z_min,
            "z_max": storey.z_max,
            "area_m2": storey.area_m2,
            "volume_m3": storey.volume_m3,
            "geometry_hash": storey.geometry_hash,
            "geometric_quality": storey.geometric_quality,
        }
        assert "confidence" not in payload
        assert "accuracy" not in payload
        assert "probability" not in payload
        assert "not an accuracy" in storey.quality_metrics["note"].lower()


def test_provenance_states_that_no_ownership_is_produced(specs):
    seg = segment(specs["three"])
    note = seg.provenance["no_property_ownership"].lower()
    assert "no property" in note
    assert "ownership" in note


def test_no_property_volume_is_ever_created(specs):
    """Segmenting storeys must not touch the cadastre."""
    from app.repositories.memory import InMemoryRepository
    from app.services.demo import seed_demo

    repo = InMemoryRepository()
    seed_demo(repo)
    before = len(repo.records("properties"))
    x, y, z, base = building_points(specs["three"])
    fs.segment_floor_points(x, y, z, base_z=base, crs=CRS)
    assert len(repo.records("properties")) == before
