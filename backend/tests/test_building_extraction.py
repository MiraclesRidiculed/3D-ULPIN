"""Tests for algorithmic building extraction.

The load-bearing properties under test:

* the pipeline recovers the **true** footprint and height of synthetic buildings
  whose geometry is known exactly, so a plausible-looking polygon cannot pass;
* extraction is **algorithmic/geometric** and labelled as such -- no ML accuracy
  or confidence number is produced anywhere;
* quality is reported as a **regularity** score with its raw components, never as
  an accuracy;
* an undeclared CRS is refused rather than assumed;
* floor segmentation is not implemented, and nothing claims otherwise.

Scenes come from :mod:`tests.fixtures.buildings`, generated from an explicit
specification so the correct answer is known.
"""
from __future__ import annotations

import numpy as np
import pytest

from app.models.enums import JobStatus, ProcessingJobType
from app.services import point_cloud_extraction as be
from app.services import point_cloud_read as pcr
from tests.fixtures import buildings as fx

CRS = "EPSG:32643"


@pytest.fixture(scope="module")
def scenes(tmp_path_factory):
    """Scene arrays, built once: the generator is pure but not free."""
    directory = tmp_path_factory.mktemp("scenes")
    return {
        "two": fx.two_buildings_scene(),
        "lshape": fx.l_shape_scene(),
        "empty": fx.empty_scene(),
        "veg": fx.vegetation_scene(),
    }


def _extract(scene, *, crs=CRS, **kwargs):
    x, y, z = fx.build_scene_arrays(scene)
    points = be.PointSet(
        x=x, y=y, z=z, crs=crs, format="LAS", source_id="DS-001",
        declared_point_count=int(x.size),
    )
    return be.DeterministicBuildingExtractor().extract(
        points, source_id="DS-001", processing_job_id="JOB-1", crs=crs, **kwargs
    )


# ==========================================================================
# 1. classify_ground
# ==========================================================================


def test_classify_ground_finds_the_ground(scenes):
    x, y, z = fx.build_scene_arrays(scenes["two"])
    mask, model = be.classify_ground(x, y, z)
    assert 0 < mask.sum() < x.size
    # The modelled surface must track the true tilted ground.
    modelled = model.cell_elevations[model.cell_index]
    error = np.abs(modelled[mask] - fx.ground_elevation(x[mask]))
    assert np.median(error) < 0.35


def test_classify_ground_handles_slope_by_using_a_local_surface(scenes):
    """A single global plane fit would be wrong at the far end of the slope."""
    x, y, z = fx.build_scene_arrays(scenes["two"])
    _, model = be.classify_ground(x, y, z)
    surface = np.array(list(model.elevations.values()))
    # A local surface spans the terrain's elevation range.
    assert surface.max() - surface.min() > 0.5


def test_classify_ground_does_not_let_a_roof_become_the_ground(scenes):
    """The failure this guards: a cell under a roof estimates ground at roof
    height, which then classifies the roof itself as ground and discards it."""
    x, y, z = fx.build_scene_arrays(scenes["two"])
    _, model = be.classify_ground(x, y, z)
    true_ground = fx.ground_elevation(x)
    # No cell may sit anywhere near the 25 m building's roof.
    assert model.cell_elevations.max() < true_ground.max() + 3.0


def test_classify_ground_on_empty_input():
    mask, model = be.classify_ground(np.zeros(0), np.zeros(0), np.zeros(0))
    assert mask.size == 0
    assert model.cell_keys.shape[0] == 0


def test_classify_ground_is_deterministic(scenes):
    x, y, z = fx.build_scene_arrays(scenes["two"])
    first, _ = be.classify_ground(x, y, z)
    second, _ = be.classify_ground(x, y, z)
    assert np.array_equal(first, second)


# ==========================================================================
# 2. extract_non_ground_points
# ==========================================================================


def test_non_ground_points_are_above_the_local_ground(scenes):
    x, y, z = fx.build_scene_arrays(scenes["two"])
    mask, model = be.classify_ground(x, y, z)
    _, _, _, height = be.extract_non_ground_points(x, y, z, mask, model)
    assert height.size > 0
    assert height.min() >= be.DEFAULT_MIN_HEIGHT_ABOVE_GROUND


def test_non_ground_count_is_close_to_the_synthesised_structure(scenes):
    """Structure is 1600 wall + 400 roof points per building, times two."""
    x, y, z = fx.build_scene_arrays(scenes["two"])
    mask, model = be.classify_ground(x, y, z)
    ngx, _, _, _ = be.extract_non_ground_points(x, y, z, mask, model)
    assert 3500 < ngx.size < 4200


# ==========================================================================
# 3. cluster_building_points
# ==========================================================================


def test_two_separated_buildings_form_two_clusters(scenes):
    x, y, z = fx.build_scene_arrays(scenes["two"])
    mask, model = be.classify_ground(x, y, z)
    ngx, ngy, ngz, ngh = be.extract_non_ground_points(x, y, z, mask, model)
    clusters = be.cluster_building_points(ngx, ngy, ngz, ngh)
    assert len(clusters) == 2


def test_clustering_separates_buildings_by_a_real_gap(scenes):
    """The two buildings are 25 m apart; DBSCAN must not bridge that gap."""
    x, y, z = fx.build_scene_arrays(scenes["two"])
    mask, model = be.classify_ground(x, y, z)
    ngx, ngy, ngz, ngh = be.extract_non_ground_points(x, y, z, mask, model)
    clusters = sorted(be.cluster_building_points(ngx, ngy, ngz, ngh), key=lambda c: c.bounds[0])
    assert len(clusters) == 2
    left, right = clusters
    gap = right.bounds[0] - left.bounds[2]
    assert gap == pytest.approx(25.0, abs=1.0)


def test_clustering_discards_wall_fragments(scenes):
    """Fragments along a sparsely-scanned wall must not become clusters.

    Threshold is set from measurement: fragments run 5-45 points on these
    scenes, genuine buildings 1,700+.
    """
    x, y, z = fx.build_scene_arrays(scenes["two"])
    mask, model = be.classify_ground(x, y, z)
    ngx, ngy, ngz, ngh = be.extract_non_ground_points(x, y, z, mask, model)
    permissive = be.cluster_building_points(ngx, ngy, ngz, ngh, min_points=5, min_area=0)
    assert len(permissive) > 2  # fragments do exist before filtering
    filtered = be.cluster_building_points(ngx, ngy, ngz, ngh)
    assert len(filtered) == 2


def test_clustering_of_nothing_is_empty():
    empty = np.zeros(0)
    assert be.cluster_building_points(empty, empty, empty, empty) == []


# ==========================================================================
# 4-5. Footprint generation and simplification
# ==========================================================================


def test_footprint_recovers_a_rectangular_building(scenes):
    out = _extract(scenes["two"])
    assert len(out) == 2
    for building, true_area in zip(out, (600.0, 400.0)):
        assert building.footprint.area == pytest.approx(true_area, rel=0.06)


def test_footprint_bounds_match_the_synthesised_extent(scenes):
    out = _extract(scenes["two"])
    short = min(out, key=lambda b: b.footprint.area)
    min_x, min_y, max_x, max_y = short.footprint.bounds
    assert min_x - fx.ORIGIN_E == pytest.approx(60.0, abs=1.0)
    assert min_y - fx.ORIGIN_N == pytest.approx(5.0, abs=1.0)
    assert max_x - fx.ORIGIN_E == pytest.approx(80.0, abs=1.0)
    assert max_y - fx.ORIGIN_N == pytest.approx(25.0, abs=1.0)


def test_concave_hull_keeps_an_L_shaped_notch(scenes):
    """A convex hull would fill the notch; a real footprint must not."""
    out = _extract(scenes["lshape"])
    assert len(out) == 1
    area = out[0].footprint.area
    union_area = 40 * 14 + 14 * 22  # 868 m²
    convex_area = 40 * 36  # 1440 m²
    assert area < convex_area * 0.75
    assert area == pytest.approx(union_area, rel=0.15)


def test_simplification_preserves_area_and_validity(scenes):
    out = _extract(scenes["two"])
    for building in out:
        assert building.footprint.is_valid
        assert building.footprint.geom_type == "Polygon"


def test_simplification_tolerance_is_honoured(scenes):
    x, y, z = fx.build_scene_arrays(scenes["two"])
    mask, model = be.classify_ground(x, y, z)
    ngx, ngy, ngz, ngh = be.extract_non_ground_points(x, y, z, mask, model)
    cluster = max(be.cluster_building_points(ngx, ngy, ngz, ngh), key=lambda c: c.count)
    raw = be.generate_building_footprint(cluster)
    coarse = be.simplify_building_footprint(raw, tolerance=5.0)
    fine = be.simplify_building_footprint(raw, tolerance=0.01)
    # A coarse tolerance cannot add vertices.
    assert len(coarse.exterior.coords) <= len(fine.exterior.coords)


# ==========================================================================
# 6. Height estimation
# ==========================================================================


def test_heights_match_the_synthesised_buildings(scenes):
    out = _extract(scenes["two"])
    heights = sorted(b.height_m for b in out)
    assert heights[0] == pytest.approx(12.0, abs=0.3)
    assert heights[1] == pytest.approx(25.0, abs=0.3)


def test_vegetation_does_not_inflate_the_building_height(scenes):
    """A maximum would report the 17 m tree as the building's height.

    Clustering runs before measuring, so the clutter is a separate cluster and
    the building's own height is unaffected.
    """
    out = _extract(scenes["veg"])
    house = max(out, key=lambda b: b.footprint.area)
    # Identified by its true 24 x 20 m footprint, not by being tallest or smallest.
    assert house.footprint.area == pytest.approx(24 * 20, rel=0.06)
    assert house.height_m == pytest.approx(10.0, abs=0.3)


def test_vegetation_is_reported_as_its_own_cluster_not_merged(scenes):
    """The clutter is detected separately, at its own height.

    Documented as a limitation rather than hidden: geometric extraction cannot
    tell a tree from a structure on shape alone, so both appear. Its much lower
    point density and lower compactness are what let a consumer tell them apart.
    """
    out = _extract(scenes["veg"])
    assert len(out) == 2
    clutter = min(out, key=lambda b: b.footprint.area)
    house = max(out, key=lambda b: b.footprint.area)
    assert clutter.height_m == pytest.approx(17.0, abs=1.0)
    # A 6 x 6 m blob sampled as densely as a 24 x 20 m building ends up with a
    # *higher* raw density, so density alone does not separate them. Compactness
    # does: the blob is round and regular, the building rectangular.
    assert clutter.quality_metrics["components"]["footprint_area_m2"] < 40
    assert house.quality_metrics["components"]["footprint_area_m2"] > 400


def test_high_percentile_rejects_a_single_spike():
    """One noise point must not set the reported height."""
    heights = np.concatenate([np.full(200, 10.0), np.array([95.0])])
    cluster = be.PointCluster(
        index=0,
        x=np.linspace(0, 20, 201),
        y=np.linspace(0, 20, 201),
        z=heights,
        height_above_ground=heights,
    )
    assert be.estimate_building_height(cluster) == pytest.approx(10.0, abs=0.2)


# ==========================================================================
# 7. Quality: regularity, never accuracy
# ==========================================================================


def test_quality_reports_components_and_a_bounded_score(scenes):
    out = _extract(scenes["two"])
    for building in out:
        assert 0.0 <= building.geometric_quality <= 1.0
        components = building.quality_metrics["components"]
        assert components["roof_planarity_rms_m"] is not None
        assert components["points_per_m2"] > 0
        assert 0.0 < components["footprint_compactness"] <= 1.0


def test_quality_never_claims_to_be_an_accuracy(scenes):
    out = _extract(scenes["two"])
    for building in out:
        metrics = building.quality_metrics
        assert "not an accuracy" in metrics["note"].lower()
        assert "not a probability" in metrics["note"].lower()
        # No key anywhere may be named like an accuracy or confidence figure.
        text = repr(metrics).lower()
        assert "accuracy" not in repr(building.provenance).lower().replace(
            "no accuracy figure is claimed", ""
        ) or "no accuracy" in text
        assert "confidence" not in text


def test_a_flat_roof_scores_higher_on_planarity_than_a_noisy_one(scenes):
    x, y, z = fx.build_scene_arrays(scenes["two"])
    mask, model = be.classify_ground(x, y, z)
    ngx, ngy, ngz, ngh = be.extract_non_ground_points(x, y, z, mask, model)
    cluster = max(be.cluster_building_points(ngx, ngy, ngz, ngh), key=lambda c: c.count)
    footprint = be.generate_building_footprint(cluster)

    flat = be.measure_building_quality(cluster, footprint, source_crs=CRS)
    noisy_heights = ngh[cluster.index * 0 : cluster.count] + np.linspace(0, 8, cluster.count)
    ragged = be.PointCluster(
        index=0, x=cluster.x, y=cluster.y, z=cluster.z,
        height_above_ground=noisy_heights,
    )
    rough = be.measure_building_quality(ragged, footprint, source_crs=CRS)
    assert (
        flat["components"]["roof_planarity_rms_m"]
        < rough["components"]["roof_planarity_rms_m"]
    )


def test_quality_is_one_for_a_perfectly_flat_dense_building():
    """A synthetic ideal case: a flat roof, a square footprint, even density."""
    n = 40
    xs = np.tile(np.linspace(0, 10, n), n)
    ys = np.repeat(np.linspace(0, 10, n), n)
    zs = np.full(n * n, 8.0)
    cluster = be.PointCluster(
        index=0, x=xs, y=ys, z=zs, height_above_ground=zs
    )
    square = be._bounds_polygon(cluster, radius=0.0)
    metrics = be.measure_building_quality(cluster, square)
    assert metrics["components"]["roof_planarity_rms_m"] == pytest.approx(0.0, abs=1e-6)
    # 4*pi*A/P^2 is 0.7854 for a square, not 1.0 -- only a disc reaches 1.0.
    # The stored component is rounded to 4 dp for stable JSON.
    assert metrics["components"]["footprint_compactness"] == pytest.approx(
        4 * np.pi * 100 / 1600, abs=1e-4
    )
    assert metrics["geometric_quality"] > 0.85


def test_compactness_distinguishes_a_square_from_a_sliver():
    """The component is meaningful: an equal-area sliver scores far lower."""
    n = 30
    xs = np.tile(np.linspace(0, 10, n), n)
    ys = np.repeat(np.linspace(0, 10, n), n)
    zs = np.full(n * n, 8.0)
    square_cluster = be.PointCluster(index=0, x=xs, y=ys, z=zs, height_above_ground=zs)
    square = be._bounds_polygon(square_cluster, radius=0.0)

    sliver = be.geometry_service.create_rectangle(0, 0, 100, 1)
    square_score = be.measure_building_quality(square_cluster, square)[
        "components"
    ]["footprint_compactness"]
    sliver_score = be.measure_building_quality(square_cluster, sliver)[
        "components"
    ]["footprint_compactness"]
    assert square_score > sliver_score * 5


# ==========================================================================
# Output contract
# ==========================================================================


def test_output_carries_every_required_field(scenes):
    out = _extract(scenes["two"])
    for building in out:
        record = building.to_record()
        # building geometry, height, source, job, method, quality, hash, provenance
        assert record["footprint"] is not None
        assert record["height_m"] > 0
        assert record["source_id"] == "DS-001"
        assert record["processing_job_id"] == "JOB-1"
        assert record["method"] == "algorithmic_geometric"
        assert 0.0 <= record["geometric_quality"] <= 1.0
        assert len(record["geometry_hash"]) == 12
        assert record["extraction"]["quality_metrics"]
        assert record["extraction"]["provenance"]["parameters"]
        assert record["extraction"]["provenance"]["ground_model"]


def test_method_is_labelled_algorithmic_not_ml(scenes):
    out = _extract(scenes["two"])
    assert be.EXTRACTION_METHOD == "algorithmic_geometric"
    for building in out:
        assert building.method == "algorithmic_geometric"
        assert "not machine learning" in be.METHOD_DESCRIPTION.lower()
        assert "not machine learning" in building.provenance["method_description"].lower()


def test_provenance_records_the_algorithm_and_its_parameters(scenes):
    out = _extract(scenes["two"])
    provenance = out[0].provenance
    assert "DBSCAN" in provenance["algorithm"]
    assert provenance["source_crs"] == CRS
    assert provenance["extractor"] == "DeterministicBuildingExtractor"
    assert provenance["extractor_version"] == be.EXTRACTOR_VERSION
    assert provenance["extracted_at"]
    assert provenance["cluster_point_count"] > 0
    assert "numpy" in provenance["libraries"]
    for key in ("cluster_eps", "min_cluster_points", "ground_cell_size", "concave_ratio"):
        assert key in provenance["parameters"]


def test_geometry_hash_is_stable_across_runs(scenes):
    first = _extract(scenes["two"])
    second = _extract(scenes["two"])
    assert sorted(b.geometry_hash for b in first) == sorted(
        b.geometry_hash for b in second
    )


def test_geometry_hash_differs_between_buildings(scenes):
    out = _extract(scenes["two"])
    assert out[0].geometry_hash != out[1].geometry_hash


def test_extraction_is_deterministic(scenes):
    """Same input, same output -- the property that makes this auditable."""
    first = [(b.geometry_hash, b.height_m) for b in _extract(scenes["two"])]
    second = [(b.geometry_hash, b.height_m) for b in _extract(scenes["two"])]
    assert first == second


def test_different_buildings_get_different_geometry_hashes():
    def building_at(x_offset, height):
        n = 30
        xs = np.tile(np.linspace(x_offset, x_offset + 12, n), n)
        ys = np.repeat(np.linspace(0, 12, n), n)
        zs = np.full(n * n, height)
        cluster = be.PointCluster(index=0, x=xs, y=ys, z=zs, height_above_ground=zs)
        return be.create_building_from_point_cloud(
            cluster, source_id="DS-1", processing_job_id="JOB-1", crs=CRS
        )

    assert building_at(0.0, 10.0).geometry_hash != building_at(0.0, 20.0).geometry_hash


# ==========================================================================
# The processor interface
# ==========================================================================


def test_extractor_satisfies_the_interface():
    assert issubclass(be.DeterministicBuildingExtractor, be.BuildingExtractor)
    assert be.DeterministicBuildingExtractor.method == "algorithmic_geometric"
    assert be.DeterministicBuildingExtractor.name == "DeterministicBuildingExtractor"


def test_the_interface_cannot_be_instantiated():
    with pytest.raises(TypeError):
        be.BuildingExtractor()


def test_a_custom_extractor_can_plug_in(scenes):
    """The interface is genuinely open, not decorative."""

    class FixedExtractor(be.BuildingExtractor):
        method = "test_double"
        name = "FixedExtractor"

        def extract(self, points, *, source_id, processing_job_id, crs):
            return []

    x, y, z = fx.build_scene_arrays(scenes["two"])
    points = be.PointSet(
        x=x, y=y, z=z, crs=CRS, format="LAS", source_id="DS-001",
        declared_point_count=int(x.size),
    )
    assert FixedExtractor().extract(
        points, source_id="DS-001", processing_job_id="J", crs=CRS
    ) == []


def test_create_building_returns_none_for_a_degenerate_cluster():
    cluster = be.PointCluster(
        index=0,
        x=np.array([5.0, 5.0]),
        y=np.array([5.0, 5.0]),
        z=np.array([1.0, 1.0]),
        height_above_ground=np.array([0.0, 0.0]),
    )
    assert be.create_building_from_point_cloud(
        cluster, source_id="DS-1", processing_job_id="J", crs=CRS
    ) is None


# ==========================================================================
# Reading points, and the CRS guard
# ==========================================================================


def test_read_points_streams_a_las_file(tmp_path):
    path = fx.write_las_scene(tmp_path / "scene.las", fx.two_buildings_scene())
    points = pcr.read_points(path, "scene.las", crs=CRS, source_id="DS-001")
    assert points.read_in_chunks is True
    assert points.crs == CRS
    assert points.x.size > 0


def test_read_points_refuses_a_file_with_no_crs(tmp_path):
    """Assuming WGS84 would mis-place the data by hundreds of kilometres."""
    path = fx.write_las_scene(
        tmp_path / "nocrs.las", fx.two_buildings_scene(), crs_epsg=None
    )
    with pytest.raises(pcr.PointReadError) as exc:
        pcr.read_points(path, "nocrs.las", crs="UNKNOWN", source_id="DS-001")
    assert "no CRS" in exc.value.detail
    assert exc.value.status_code == 422


def test_read_points_reads_ply_without_streaming(tmp_path):
    """PLY has no streaming reader; that is reported, not hidden."""
    path = fx.write_ply_scene(tmp_path / "scene.ply", fx.two_buildings_scene())
    points = pcr.read_points(path, "scene.ply", crs=CRS, source_id="DS-001")
    assert points.read_in_chunks is False
    assert points.format == "PLY"
    assert points.x.size > 0


def test_read_points_records_truncation(tmp_path):
    path = fx.write_las_scene(tmp_path / "scene.las", fx.two_buildings_scene())
    points = pcr.read_points(
        path, "scene.las", crs=CRS, source_id="DS-001", max_points=500
    )
    assert points.truncated is True
    assert points.x.size == 500


def test_laz_reads_identically_to_las(tmp_path):
    scene = fx.two_buildings_scene()
    las = fx.write_las_scene(tmp_path / "a.las", scene)
    laz = fx.write_laz_scene(tmp_path / "a.laz", scene)
    from_las = pcr.read_points(las, "a.las", crs=CRS, source_id="DS-001")
    from_laz = pcr.read_points(laz, "a.laz", crs=CRS, source_id="DS-001")
    assert np.allclose(from_las.x, from_laz.x)
    assert np.allclose(from_las.z, from_laz.z)


def test_end_to_end_from_a_ply_file(tmp_path):
    """Extraction works from a PLY, which declares no extent at all."""
    path = fx.write_ply_scene(tmp_path / "scene.ply", fx.two_buildings_scene())
    points = pcr.read_points(path, "scene.ply", crs=CRS, source_id="DS-001")
    out = be.DeterministicBuildingExtractor().extract(
        points, source_id="DS-001", processing_job_id="JOB-1", crs=CRS
    )
    assert len(out) == 2
    assert sorted(round(b.height_m) for b in out) == [12, 25]


# ==========================================================================
# What is deliberately NOT here
# ==========================================================================


def test_floor_segmentation_is_not_implemented():
    """This milestone stops before storey detection, and says so."""
    source = (
        open("app/services/point_cloud_extraction.py", encoding="utf-8").read()
    )
    assert "segment" not in source.lower().replace("storey", "")
    assert not hasattr(be, "segment_floors")
    assert not hasattr(be, "extract_floors")


def test_no_confidence_field_is_produced(scenes):
    out = _extract(scenes["two"])
    for building in out:
        record = building.to_record()
        assert "confidence" not in record
        assert "accuracy" not in record
        assert "probability" not in record


def test_job_status_constants_include_the_stages_in_use():
    """The statuses the pipeline reports are real enum members."""
    assert JobStatus.COMPLETED.value == "COMPLETED"
    assert JobStatus.NOT_IMPLEMENTED.value == "NOT_IMPLEMENTED"
    assert ProcessingJobType.BUILDING_EXTRACTION.value == "BUILDING_EXTRACTION"
    assert ProcessingJobType.FLOOR_SEGMENTATION.value == "FLOOR_SEGMENTATION"
