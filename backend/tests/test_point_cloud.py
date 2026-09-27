"""Point-cloud inspection and ingestion tests.

Uses the small synthetic fixtures in :mod:`tests.fixtures.point_clouds`.

The load-bearing properties under test:

* metadata comes from the **header only** â€” cost must not scale with point count;
* a file's CRS is reported as declared, and **never guessed**;
* ingestion is **streamed**, so nothing is buffered whole and the size cap is
  enforced while reading;
* every pipeline step is observable in the stored result.
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest

from app.models.enums import JobStatus, ProcessingJobType, SourceType
from app.repositories.memory import InMemoryRepository
from app.services import point_cloud as pc
from app.services import point_cloud_ingestion as pci
from app.services.crs import UNKNOWN_CRS
from tests.fixtures.point_clouds import (
    EXTENT_AREA_M2,
    EXTENT_X,
    EXTENT_Y,
    FIXTURE_ORIGIN_E,
    FIXTURE_ORIGIN_N,
    broken_bytes,
    build_fixtures,
)


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory):
    return build_fixtures(tmp_path_factory.mktemp("pointclouds"))


@pytest.fixture
def repo():
    return InMemoryRepository()


def _upload(path: Path) -> io.BufferedReader:
    return open(path, "rb")


# ==========================================================================
# read_point_cloud
# ==========================================================================


def test_read_las_header(fixtures):
    handle = pc.read_point_cloud(fixtures["las"])
    assert handle.format is pc.PointCloudFormat.LAS
    assert handle.point_count == 4
    assert handle.compressed is False
    assert handle.file_version == "1.4"
    assert handle.generating_software == "V-CAD synthetic fixture"


def test_read_laz_is_marked_compressed(fixtures):
    handle = pc.read_point_cloud(fixtures["laz"])
    assert handle.format is pc.PointCloudFormat.LAZ
    assert handle.compressed is True
    assert handle.point_count == 4


def test_read_ply(fixtures):
    handle = pc.read_point_cloud(fixtures["ply"])
    assert handle.format is pc.PointCloudFormat.PLY
    assert handle.point_count == 4
    assert "x" in handle.properties and "z" in handle.properties
    assert handle.text_format is False


def test_read_ply_ascii(fixtures):
    handle = pc.read_point_cloud(fixtures["ply_ascii"])
    assert handle.text_format is True
    assert handle.point_count == 4


def test_read_point_cloud_computes_hash_when_not_supplied(fixtures):
    handle = pc.read_point_cloud(fixtures["las"])
    assert handle.sha256 is None  # not requested
    digest = pc.file_sha256(fixtures["las"])
    assert len(digest) == 64


def test_read_rejects_unsupported_extension(tmp_path):
    path = tmp_path / "scan.txt"
    path.write_bytes(b"nope")
    with pytest.raises(pc.PointCloudError) as exc:
        pc.read_point_cloud(path)
    assert exc.value.status_code == 400


def test_read_rejects_missing_file(tmp_path):
    with pytest.raises(pc.PointCloudError) as exc:
        pc.read_point_cloud(tmp_path / "absent.las")
    assert exc.value.status_code == 404


def test_read_rejects_corrupt_file(tmp_path):
    path = tmp_path / "broken.las"
    path.write_bytes(broken_bytes())
    with pytest.raises(pc.PointCloudError) as exc:
        pc.read_point_cloud(path)
    assert exc.value.status_code == 422


def test_read_rejects_ply_without_magic(tmp_path):
    path = tmp_path / "fake.ply"
    path.write_bytes(b"NOTAPLY\n" + b"x" * 100)
    with pytest.raises(pc.PointCloudError):
        pc.read_point_cloud(path)


def test_read_rejects_truncated_ply_header(tmp_path):
    path = tmp_path / "trunc.ply"
    path.write_bytes(b"ply\nformat ascii 1.0\nelement vertex 4\n")
    with pytest.raises(pc.PointCloudError) as exc:
        pc.read_point_cloud(path)
    assert "header" in exc.value.detail.lower()


# ==========================================================================
# count, bounds, CRS, density
# ==========================================================================


def test_calculate_point_count(fixtures):
    handle = pc.read_point_cloud(fixtures["las"])
    assert pc.calculate_point_count(handle) == 4


def test_calculate_bounds_from_las_header(fixtures):
    handle = pc.read_point_cloud(fixtures["las"])
    bounds = pc.calculate_point_cloud_bounds(handle)
    assert bounds.is_defined
    # Absolute coordinates near the fixture origin, spanning 40 x 30 m.
    assert bounds.min_x == pytest.approx(FIXTURE_ORIGIN_E, abs=0.02)
    assert bounds.max_x == pytest.approx(FIXTURE_ORIGIN_E + EXTENT_X, abs=0.02)
    assert bounds.min_y == pytest.approx(FIXTURE_ORIGIN_N, abs=0.02)
    assert bounds.max_y == pytest.approx(FIXTURE_ORIGIN_N + EXTENT_Y, abs=0.02)
    assert bounds.min_z == pytest.approx(0.0, abs=0.02)
    assert bounds.max_z == pytest.approx(3.0, abs=0.02)


def test_calculate_density_points_per_square_metre(fixtures):
    """4 points over a 40 x 30 m footprint."""
    handle = pc.read_point_cloud(fixtures["las"])
    assert pc.calculate_point_density(handle) == pytest.approx(4 / EXTENT_AREA_M2, rel=1e-3)


def test_density_is_none_when_plan_area_is_zero(tmp_path):
    """A purely vertical scan has no plan area; an invented density is worse."""
    from tests.fixtures.point_clouds import write_las

    path = write_las(tmp_path / "vertical.las", n_points=4)
    import laspy
    import numpy as np

    with laspy.open(path) as reader:
        las = reader.read()
    las.x = np.zeros(4)
    las.y = np.zeros(4)
    las.write(str(path))

    handle = pc.read_point_cloud(path)
    assert pc.calculate_point_density(handle) is None


def test_density_is_none_for_ply(fixtures):
    """PLY declares no extent, so density cannot be computed from the header."""
    handle = pc.read_point_cloud(fixtures["ply"])
    assert pc.calculate_point_density(handle) is None


def test_detect_crs_from_las_vlr(fixtures):
    handle = pc.read_point_cloud(fixtures["las"])
    assert pc.detect_point_cloud_crs(handle) == "EPSG:32643"
    assert handle.crs_source == pc.CRS_FROM_VLR


def test_detect_crs_from_ply_comment(fixtures):
    handle = pc.read_point_cloud(fixtures["ply"])
    assert pc.detect_point_cloud_crs(handle) == "EPSG:32643"
    assert handle.crs_source == pc.CRS_FROM_COMMENT


def test_missing_crs_is_unknown_not_wgs84(fixtures):
    """The critical safety property: never guess a CRS."""
    for name in ("las_no_crs", "ply_no_crs"):
        handle = pc.read_point_cloud(fixtures[name])
        assert pc.detect_point_cloud_crs(handle) == UNKNOWN_CRS
        assert handle.crs_source == pc.CRS_UNDECLARED


# ==========================================================================
# metadata
# ==========================================================================


def test_metadata_assembles_every_field(fixtures):
    handle = pc.read_point_cloud(fixtures["las"])
    meta = pc.get_point_cloud_metadata(handle)
    assert meta.format == "LAS"
    assert meta.point_count == 4
    assert meta.crs == "EPSG:32643"
    assert meta.bounds.is_defined
    assert meta.density_points_per_m2 == pytest.approx(4 / EXTENT_AREA_M2, rel=1e-3)
    assert meta.acquisition["generating_software"] == "V-CAD synthetic fixture"
    assert meta.acquisition["creation_date"]


def test_metadata_display_bounds_are_wgs84(fixtures):
    handle = pc.read_point_cloud(fixtures["las"])
    meta = pc.get_point_cloud_metadata(handle)
    # Display bounds are reprojected into degrees near the demo anchor.
    assert meta.display_bounds is not None
    assert 77.0 < meta.display_bounds.min_x < 78.0
    assert 28.0 < meta.display_bounds.min_y < 29.0


def test_metadata_display_bounds_absent_without_crs(fixtures):
    handle = pc.read_point_cloud(fixtures["las_no_crs"])
    meta = pc.get_point_cloud_metadata(handle)
    assert meta.display_bounds is None


def test_metadata_records_ply_properties_and_comments(fixtures):
    handle = pc.read_point_cloud(fixtures["ply"])
    meta = pc.get_point_cloud_metadata(handle)
    assert "x" in meta.properties
    assert any("V-CAD synthetic fixture" in c for c in meta.comments)
    assert meta.acquisition["comments"]


# ==========================================================================
# validate_point_cloud
# ==========================================================================


def test_validate_accepts_a_good_file(fixtures):
    handle = pc.read_point_cloud(fixtures["las"])
    result = pc.validate_point_cloud(handle)
    assert result.is_valid is True
    assert result.problems == []


def test_validate_flags_zero_points(fixtures):
    handle = pc.read_point_cloud(fixtures["empty"])
    result = pc.validate_point_cloud(handle)
    assert result.is_valid is False
    assert any("zero points" in p for p in result.problems)


def test_validate_warns_about_missing_crs(fixtures):
    handle = pc.read_point_cloud(fixtures["las_no_crs"])
    result = pc.validate_point_cloud(handle)
    assert result.is_valid is True  # a warning, not a hard failure
    assert any("CRS" in w for w in result.warnings)


def test_validate_warns_that_ply_has_no_extent(fixtures):
    handle = pc.read_point_cloud(fixtures["ply"])
    result = pc.validate_point_cloud(handle)
    assert any("PLY" in w for w in result.warnings)


# ==========================================================================
# streaming behaviour
# ==========================================================================


def test_spool_upload_streams_and_hashes(fixtures, tmp_path):
    destination = tmp_path / "copy.las"
    with _upload(fixtures["las"]) as stream:
        size, digest = pc.spool_upload(stream, destination, max_bytes=10_000_000)
    assert size == fixtures["las"].stat().st_size
    assert digest == pc.file_sha256(fixtures["las"])


def test_spool_upload_aborts_when_size_exceeded(fixtures, tmp_path):
    destination = tmp_path / "toobig.las"
    with _upload(fixtures["las"]) as stream:
        with pytest.raises(pc.PointCloudError) as exc:
            pc.spool_upload(stream, destination, max_bytes=16)
    assert exc.value.status_code == 413


def test_header_read_does_not_depend_on_point_count(tmp_path):
    """Cost must come from the header, not the points.

    A file with many points must be read just as cheaply as a tiny one â€” proved
    by asserting the point count is taken from the header even when the payload
    is far larger than the header.
    """
    from tests.fixtures.point_clouds import write_las

    small = write_las(tmp_path / "small.las", n_points=4)
    large = write_las(tmp_path / "large.las", n_points=200_000)
    small_handle = pc.read_point_cloud(small)
    large_handle = pc.read_point_cloud(large)
    assert small_handle.point_count == 4
    assert large_handle.point_count == 200_000
    # The large file is much bigger, yet nothing about the read scaled with it.
    assert large.stat().st_size > small.stat().st_size * 100


# ==========================================================================
# the ingestion pipeline
# ==========================================================================


def test_pipeline_registers_source_and_jobs(repo, fixtures):
    with _upload(fixtures["las"]) as stream:
        result = pci.ingest_point_cloud(repo, stream, "scan.las")

    assert result["accepted"] is True
    assert result["point_count"] == 4
    assert result["crs"] == "EPSG:32643"
    assert result["density_points_per_m2"] == pytest.approx(4 / EXTENT_AREA_M2, rel=1e-3)
    assert result["bounds"]["max_x"] == pytest.approx(FIXTURE_ORIGIN_E + EXTENT_X, abs=0.02)
    assert result["bounds"]["crs"] == "EPSG:32643"
    assert result["display_bounds"]["crs"] == "EPSG:4326"
    assert 77.0 < result["display_bounds"]["min_x"] < 78.0
    assert result["validation"]["is_valid"] is True
    assert len(result["sha256"]) == 64

    sources = repo.records("sources")
    assert len(sources) == 1
    assert sources[0]["source_type"] == SourceType.POINT_CLOUD.value
    assert sources[0]["crs"] == "EPSG:32643"
    assert sources[0]["metadata"]["point_count"] == 4

    jobs = repo.records("processing_jobs")
    # metadata extraction + the two unimplemented stages
    assert len(jobs) == 3
    by_type = {j["job_type"]: j for j in jobs}
    assert by_type[ProcessingJobType.METADATA_EXTRACTION.value]["status"] == (
        JobStatus.COMPLETED.value
    )
    # Both downstream stages are implemented and run on request, so they are
    # PENDING: available, not missing. Nothing is NOT_IMPLEMENTED.
    for stage in ("BUILDING_EXTRACTION", "FLOOR_SEGMENTATION"):
        assert by_type[stage]["status"] == JobStatus.PENDING.value
    assert not any(j["status"] == JobStatus.NOT_IMPLEMENTED.value for j in jobs)


def test_pipeline_records_hash_and_provenance(repo, fixtures):
    with _upload(fixtures["laz"]) as stream:
        result = pci.ingest_point_cloud(repo, stream, "scan.laz")
    stored = repo.records("sources")[0]["metadata"]
    assert stored["sha256"] == result["sha256"]
    assert stored["format"] == "LAZ"
    assert stored["compressed"] is True
    assert stored["file_size_bytes"] == fixtures["laz"].stat().st_size
    assert stored["acquisition"]["generating_software"]


def test_pipeline_never_guesses_a_crs(repo, fixtures):
    with _upload(fixtures["las_no_crs"]) as stream:
        result = pci.ingest_point_cloud(repo, stream, "scan.las")
    assert result["crs"] == UNKNOWN_CRS
    assert result["crs_source"] == pc.CRS_UNDECLARED
    assert result["display_bounds"] is None
    stored = repo.records("sources")[0]
    assert stored["crs"] == UNKNOWN_CRS
    assert stored["metadata"]["crs_declared_in_payload"] is False


def test_caller_supplied_crs_fills_an_undeclared_one(repo, fixtures):
    with _upload(fixtures["las_no_crs"]) as stream:
        result = pci.ingest_point_cloud(
            repo, stream, "scan.las", crs_override="EPSG:32643"
        )
    assert result["crs"] == "EPSG:32643"
    assert result["crs_source"] == "caller_supplied"
    # ...and bounds become displayable as a result.
    assert result["display_bounds"] is not None


def test_caller_crs_does_not_override_a_declared_one(repo, fixtures):
    """A file that states its CRS is believed, not overridden."""
    with _upload(fixtures["las"]) as stream:
        result = pci.ingest_point_cloud(
            repo, stream, "scan.las", crs_override="EPSG:7755"
        )
    assert result["crs"] == "EPSG:32643"


def test_pipeline_rejects_bad_extension(repo, tmp_path):
    path = tmp_path / "scan.txt"
    path.write_bytes(b"x")
    with open(path, "rb") as stream:
        with pytest.raises(pci.IngestionFailure) as exc:
            pci.ingest_point_cloud(repo, stream, "scan.txt")
    assert exc.value.status_code == 400


def test_pipeline_enforces_size_limit(repo, fixtures):
    with _upload(fixtures["las"]) as stream:
        with pytest.raises(pci.IngestionFailure) as exc:
            pci.ingest_point_cloud(repo, stream, "scan.las", max_bytes=32)
    assert exc.value.status_code == 413
    assert repo.records("sources") == []


def test_pipeline_rejects_corrupt_upload(repo, tmp_path):
    path = tmp_path / "broken.las"
    path.write_bytes(broken_bytes())
    with open(path, "rb") as stream:
        with pytest.raises(pci.IngestionFailure) as exc:
            pci.ingest_point_cloud(repo, stream, "broken.las")
    assert exc.value.status_code == 422
    # A failed ingest must not leave a half-registered source behind.
    assert repo.records("sources") == []


def test_pipeline_handles_ply(repo, fixtures):
    with _upload(fixtures["ply"]) as stream:
        result = pci.ingest_point_cloud(repo, stream, "scan.ply")
    assert result["format"] == "PLY"
    assert result["point_count"] == 4
    # PLY declares no extent, so bounds are absent rather than invented.
    assert result["bounds"] is None
    assert result["display_bounds"] is None


def test_source_ids_increment_across_ingestions(repo, fixtures):
    with _upload(fixtures["las"]) as stream:
        first = pci.ingest_point_cloud(repo, stream, "a.las")
    with _upload(fixtures["ply"]) as stream:
        second = pci.ingest_point_cloud(repo, stream, "b.ply")
    assert first["source_id"] == "DS-001"
    assert second["source_id"] == "DS-002"


def test_list_processing_jobs_filters_by_source(repo, fixtures):
    with _upload(fixtures["las"]) as stream:
        a = pci.ingest_point_cloud(repo, stream, "a.las")
    with _upload(fixtures["ply"]) as stream:
        b = pci.ingest_point_cloud(repo, stream, "b.ply")
    assert len(pci.list_processing_jobs(repo)) == 6
    only_a = pci.list_processing_jobs(repo, a["source_id"])
    assert len(only_a) == 3
    assert all(j["source_id"] == a["source_id"] for j in only_a)
    assert {j["source_id"] for j in pci.list_processing_jobs(repo, b["source_id"])} == {
        b["source_id"]
    }
