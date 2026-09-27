"""Synthetic point-cloud fixtures.

Deliberately tiny (a handful of points) so tests stay fast and the files can be
committed. Generated at import time by :func:`build_fixtures` and cached in a
temporary directory for the session.

Real-world point clouds are millions of points; the point of these fixtures is to
exercise header parsing, CRS resolution and extent handling, not volume.
"""
from __future__ import annotations

import io
import struct
from pathlib import Path

import numpy as np

#: The fixture sits at the demo city's projected origin so its coordinates
#: genuinely agree with the CRS it declares. Points in the wrong UTM zone would
#: make display-bounds assertions meaningless.
FIXTURE_ORIGIN_E = 715_972.0
FIXTURE_ORIGIN_N = 3_167_100.0

#: Deterministic point positions as absolute eastings/northings. The extent is
#: 40 m x 30 m, so density assertions stay readable.
LAS_POINTS_XY = [
    (FIXTURE_ORIGIN_E, FIXTURE_ORIGIN_N),
    (FIXTURE_ORIGIN_E + 40.0, FIXTURE_ORIGIN_N),
    (FIXTURE_ORIGIN_E + 40.0, FIXTURE_ORIGIN_N + 30.0),
    (FIXTURE_ORIGIN_E, FIXTURE_ORIGIN_N + 30.0),
]
LAS_POINTS_Z = [0.0, 1.0, 2.0, 3.0]
LAS_POINT_COUNT = len(LAS_POINTS_XY)

#: The 40 m x 30 m plan extent, for density and bounds assertions.
EXTENT_X = 40.0
EXTENT_Y = 30.0
EXTENT_AREA_M2 = EXTENT_X * EXTENT_Y


def _offsets() -> list[float]:
    return [FIXTURE_ORIGIN_E, FIXTURE_ORIGIN_N, 0.0]


def write_las(
    path: Path,
    *,
    crs_epsg: int | None = 32643,
    point_format: int = 3,
    version: str = "1.4",
    generating_software: str = "V-CAD synthetic fixture",
    n_points: int = LAS_POINT_COUNT,
) -> Path:
    """Write a small LAS file. ``crs_epsg=None`` omits the CRS VLR."""
    import laspy
    from pyproj import CRS as PyCRS

    header = laspy.LasHeader(point_format=point_format, version=version)
    header.offsets = _offsets()
    header.scales = [0.01, 0.01, 0.01]
    # laspy overwrites LasData.generating_software on write, so it must be set on
    # the header to survive.
    header.generating_software = generating_software
    if crs_epsg is not None:
        header.add_crs(PyCRS.from_epsg(crs_epsg))

    las = laspy.LasData(header)
    las.x = np.array([LAS_POINTS_XY[i % 4][0] for i in range(n_points)])
    las.y = np.array([LAS_POINTS_XY[i % 4][1] for i in range(n_points)])
    las.z = np.array([LAS_POINTS_Z[i % 4] for i in range(n_points)])
    las.intensity = np.arange(n_points, dtype=np.uint16)
    las.classification = np.zeros(n_points, dtype=np.uint8)
    las.write(str(path))
    return path


def write_laz(path: Path, **kwargs) -> Path:
    """Write a small LAZ (LAZ-compressed LAS)."""
    import laspy
    from pyproj import CRS as PyCRS

    n_points = kwargs.get("n_points", LAS_POINT_COUNT)
    crs_epsg = kwargs.get("crs_epsg", 32643)
    header = laspy.LasHeader(point_format=kwargs.get("point_format", 3), version="1.4")
    header.offsets = _offsets()
    header.scales = [0.01, 0.01, 0.01]
    header.generating_software = "V-CAD synthetic fixture"
    if crs_epsg is not None:
        header.add_crs(PyCRS.from_epsg(crs_epsg))
    las = laspy.LasData(header)
    las.x = np.array([LAS_POINTS_XY[i % 4][0] for i in range(n_points)])
    las.y = np.array([LAS_POINTS_XY[i % 4][1] for i in range(n_points)])
    las.z = np.array([LAS_POINTS_Z[i % 4] for i in range(n_points)])
    las.intensity = np.arange(n_points, dtype=np.uint16)
    las.classification = np.zeros(n_points, dtype=np.uint8)
    las.write(str(path), do_compress=True)
    return path


PLY_HEADER_TEMPLATE = """ply
format binary_little_endian 1.0
comment created by V-CAD synthetic fixture
comment acquisition_date 2026-09-26
{crs_comment}element vertex {count}
property float x
property float y
property float z
property uchar red
property uchar green
property uchar blue
element face 0
property list uchar int vertex_indices
end_header
"""


def write_ply(
    path: Path,
    *,
    n_points: int = LAS_POINT_COUNT,
    crs_comment: str | None = "comment crs EPSG:32643\n",
    text_format: bool = False,
) -> Path:
    """Write a small PLY file. ``crs_comment=None`` declares no CRS."""
    header = PLY_HEADER_TEMPLATE.format(
        crs_comment=crs_comment or "", count=n_points
    )
    if text_format:
        header = header.replace("binary_little_endian", "ascii")
        body = "".join(
            f"{LAS_POINTS_XY[i % 4][0]} {LAS_POINTS_XY[i % 4][1]} "
            f"{LAS_POINTS_Z[i % 4]} 255 0 0\n"
            for i in range(n_points)
        )
        path.write_text(header + body, encoding="ascii")
        return path

    chunks = []
    for i in range(n_points):
        x, y = LAS_POINTS_XY[i % 4]
        z = LAS_POINTS_Z[i % 4]
        chunks.append(struct.pack("<fff", x, y, z))
        chunks.append(struct.pack("<BBB", 255, 0, 0))
    path.write_bytes(header.encode("ascii") + b"".join(chunks))
    return path


def build_fixtures(directory: Path) -> dict[str, Path]:
    """Create every fixture once and return them by name."""
    directory.mkdir(parents=True, exist_ok=True)
    return {
        "las": write_las(directory / "fixture.las"),
        "las_no_crs": write_las(directory / "fixture_nocrs.las", crs_epsg=None),
        "laz": write_laz(directory / "fixture.laz"),
        "ply": write_ply(directory / "fixture.ply"),
        "ply_no_crs": write_ply(directory / "fixture_nocrs.ply", crs_comment=None),
        "ply_ascii": write_ply(directory / "fixture_ascii.ply", text_format=True),
        "empty": write_las(directory / "empty.las", n_points=0),
        "not_a_cloud": directory / "broken.las",
    }


def broken_bytes() -> bytes:
    """Content that is not a readable point cloud."""
    return b"this is definitely not a LAS file" * 8
