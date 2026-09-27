"""Point reading for extraction.

Metadata inspection reads headers only. **Extraction genuinely needs points**,
so this module is where that trade-off is made explicitly and bounded.

Memory
------
LAS/LAZ are read through :meth:`laspy.LasReader.chunk_iterator`, so only one
block of points is resident at a time. The default block is 1,000,000 points,
about 30 MB, regardless of whether the file holds ten thousand points or ten
billion.

PLY has no streaming reader: ``plyfile`` materialises the whole vertex block, so
a PLY is loaded in full. That is a property of the format and its library, not a
choice made here, so it is reported in the returned provenance rather than
hidden. :data:`DEFAULT_MAX_POINTS` caps total points either way, and whether the
cap was hit is recorded -- a truncated extraction must not be mistaken for a
complete one.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from app.models.enums import PointCloudFormat
from app.services import point_cloud as pc_service
from app.services.point_cloud_extraction import (
    DEFAULT_MAX_POINTS,
    PointSet,
)

#: Points per read block. ~30 MB at LAS point format 3.
DEFAULT_CHUNK_POINTS = 1_000_000


class PointReadError(ValueError):
    """Points could not be read. Carries an HTTP-facing status code."""

    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = message


def read_points(
    path: Path | str,
    filename: str | None = None,
    *,
    crs: str | None = None,
    source_id: str = "",
    chunk_points: int = DEFAULT_CHUNK_POINTS,
    max_points: int = DEFAULT_MAX_POINTS,
) -> PointSet:
    """Read a point cloud's coordinates into arrays for extraction.

    ``crs`` must be the effective CRS resolved during ingestion, including a
    caller-supplied one. A cloud with no CRS cannot be interpreted at all, so it
    is rejected rather than assumed to be WGS84 -- assuming would silently place
    the data hundreds of kilometres away.
    """
    path = Path(path)
    name = filename or path.name

    try:
        fmt = pc_service.format_for_filename(name)
    except pc_service.PointCloudError as exc:
        raise PointReadError(exc.detail, exc.status_code) from None

    effective_crs = crs or pc_service.UNKNOWN_CRS
    if effective_crs == pc_service.UNKNOWN_CRS:
        raise PointReadError(
            f"{name} declares no CRS, so its coordinates cannot be interpreted. "
            "Re-upload with ?crs=EPSG:<code>.",
            422,
        )

    if fmt is PointCloudFormat.PLY:
        xs, ys, zs, read_in_chunks, truncated = _read_ply(path, name, max_points)
    else:
        xs, ys, zs, read_in_chunks, truncated = _read_las(
            path, name, chunk_points, max_points
        )

    if xs.size == 0:
        raise PointReadError(f"{name} yielded no points to process", 422)

    return PointSet(
        x=xs,
        y=ys,
        z=zs,
        crs=effective_crs,
        format=fmt.value,
        source_id=source_id,
        declared_point_count=int(xs.size),
        read_in_chunks=read_in_chunks,
        truncated=truncated,
    )


def _read_las(
    path: Path, filename: str, chunk_points: int, max_points: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray, bool, bool]:
    """Stream LAS/LAZ coordinates block by block. Only one block is resident."""
    import laspy

    try:
        reader = laspy.open(path)
    except Exception as exc:  # noqa: BLE001 - normalise library errors
        raise PointReadError(f"Could not open {filename}: {exc}", 422) from exc

    try:
        declared = int(reader.header.point_count)
        xs: list[np.ndarray] = []
        ys: list[np.ndarray] = []
        zs: list[np.ndarray] = []
        total = 0
        truncated = False
        try:
            for points in reader.chunk_iterator(chunk_points):
                if total >= max_points:
                    truncated = True
                    break
                take = min(len(points), max_points - total)
                xs.append(np.asarray(points.x[:take], dtype=np.float64))
                ys.append(np.asarray(points.y[:take], dtype=np.float64))
                zs.append(np.asarray(points.z[:take], dtype=np.float64))
                total += take
        except Exception as exc:  # noqa: BLE001
            raise PointReadError(
                f"Could not read points from {filename}: {exc}", 422
            ) from exc
    finally:
        reader.close()

    if not xs:
        empty = np.zeros(0, dtype=np.float64)
        return empty, empty.copy(), empty.copy(), True, False

    return (
        np.concatenate(xs),
        np.concatenate(ys),
        np.concatenate(zs),
        True,
        truncated or total < declared,
    )


def _read_ply(
    path: Path, filename: str, max_points: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray, bool, bool]:
    """Read PLY vertices. ``plyfile`` has no streaming reader, so this is a full
    load of the vertex block; ``read_in_chunks`` is reported as ``False``."""
    import plyfile

    try:
        data = plyfile.PlyData.read(path)
        vertex = data["vertex"]
        declared = int(vertex.count)
        take = min(declared, max_points)
        xs = np.asarray(vertex["x"][:take], dtype=np.float64)
        ys = np.asarray(vertex["y"][:take], dtype=np.float64)
        zs = np.asarray(vertex["z"][:take], dtype=np.float64)
    except KeyError as exc:
        raise PointReadError(
            f"{filename} has no x/y/z vertex properties ({exc})", 422
        ) from None
    except Exception as exc:  # noqa: BLE001
        raise PointReadError(f"Could not read {filename}: {exc}", 422) from exc

    return xs, ys, zs, False, take < declared


def point_set_summary(points: PointSet) -> dict[str, Any]:
    """Compact description of a read point set, for provenance."""
    return {
        "points_read": int(points.x.size),
        "declared_point_count": points.declared_point_count,
        "read_in_chunks": points.read_in_chunks,
        "truncated": points.truncated,
        "format": points.format,
        "crs": points.crs,
    }


__all__ = [
    "DEFAULT_CHUNK_POINTS",
    "DEFAULT_MAX_POINTS",
    "PointReadError",
    "point_set_summary",
    "read_points",
]
