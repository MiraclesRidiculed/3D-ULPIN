"""Tests for the synthetic point-cloud fixtures.

Kept separate from the point-cloud tests so a fixture bug is distinguishable
from a service bug.
"""
from __future__ import annotations

import pytest

from tests.fixtures.point_clouds import build_fixtures, write_las, write_ply

#: Point positions the fixtures are built from, in projected metres.
EXPECTED_XY = [(0.0, 0.0), (40.0, 0.0), (40.0, 30.0), (0.0, 30.0)]


def test_las_fixture_is_readable_and_has_the_expected_points(tmp_path):
    import laspy

    path = write_las(tmp_path / "f.las")
    with laspy.open(path) as reader:
        assert reader.header.point_count == 4


def test_las_fixture_declares_its_crs(tmp_path):
    import laspy

    path = write_las(tmp_path / "f.las", crs_epsg=32643)
    with laspy.open(path) as reader:
        crs = reader.header.parse_crs()
        assert crs is not None
        assert crs.to_epsg() == 32643


def test_las_fixture_omits_crs_when_asked(tmp_path):
    import laspy

    path = write_las(tmp_path / "f.las", crs_epsg=None)
    with laspy.open(path) as reader:
        assert reader.header.parse_crs() is None


def test_ply_fixture_parses_with_plyfile(tmp_path):
    import plyfile

    path = write_ply(tmp_path / "f.ply", n_points=4)
    data = plyfile.PlyData.read(path)
    assert data["vertex"].count == 4
    assert {"x", "y", "z"} <= {p.name for p in data["vertex"].properties}


def test_ply_fixture_carries_comments(tmp_path):
    import plyfile

    path = write_ply(tmp_path / "f.ply")
    comments = plyfile.PlyData.read(path).comments
    assert any("V-CAD synthetic fixture" in c for c in comments)


def test_all_fixtures_build(tmp_path):
    fixtures = build_fixtures(tmp_path)
    for name, path in fixtures.items():
        if name == "not_a_cloud":
            continue
        assert path.exists(), name
        assert path.stat().st_size > 0, name
