"""Point-cloud inspection.

Reads **headers only**. Cost is independent of point count, so a 20 GB LAZ and a
2 KB fixture are both cheap: LAS/LAZ expose everything needed in the header, and
the PLY header is a bounded text block. Points are never materialised — this
module extracts metadata, not geometry.

Libraries
---------
``laspy`` (with the ``lazrs`` backend) for LAS and LAZ. PDAL is the other
established option, but it ships no Python wheel on this platform and building
from source fails, so laspy is used. laspy's :func:`laspy.open` returns a lazy
reader whose header is read without touching the point records.

``plyfile`` for PLY. Note it has no header-only API, so the PLY header is parsed
incrementally here (a few hundred bytes) rather than via ``PlyData.read``, which
would load every vertex. ``plyfile`` remains the reference for PLY structure and
is used to confirm the declared format is one it can read.

CRS
---
Reported exactly as the file declares it. A file with no CRS is reported as
:data:`~app.services.crs.UNKNOWN_CRS` and never guessed: assuming WGS84 would
silently mis-place the data. Downstream code must handle the unknown case.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Iterable

from app.models.enums import PointCloudFormat
from app.models.schemas import (
    PointCloudBounds,
    PointCloudMetadata,
    PointCloudValidation,
)
from app.services.crs import (
    DISPLAY_PRECISION,
    UNKNOWN_CRS,
    WGS84,
    crs_authority,
    select_processing_crs,
    transform_coordinates,
    transform_point,
)

#: Extensions this service can inspect.
SUPPORTED_EXTENSIONS: dict[str, PointCloudFormat] = {
    "las": PointCloudFormat.LAS,
    "laz": PointCloudFormat.LAZ,
    "ply": PointCloudFormat.PLY,
}

#: Bytes read at most when streaming a PLY header. The PLY header is a short
#: ASCII block; this cap stops a malformed file being read unboundedly.
MAX_PLY_HEADER_BYTES = 64 * 1024

#: Chunk size for hashing and spooling uploads.
HASH_CHUNK_BYTES = 1024 * 1024

#: How a file's CRS was established, for auditability.
CRS_FROM_VLR = "las_vlr"
CRS_FROM_GEOKEY = "las_geokey"
CRS_FROM_COMMENT = "ply_comment"
CRS_UNDECLARED = "undeclared"

_PLY_ELEMENT_RE = re.compile(r"^element\s+(\w+)\s+(\d+)")
_PLY_PROPERTY_RE = re.compile(r"^property\s+(\w+)\s+(\w+)(?:\s+(\w+))?")
_PLY_COMMENT_RE = re.compile(r"^comment\s+(.*)$", re.IGNORECASE)


class PointCloudError(ValueError):
    """A point cloud could not be read. Carries an HTTP-facing status code."""

    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = message


# --------------------------------------------------------------------------
# Handle
# --------------------------------------------------------------------------


@dataclass
class PointCloudHandle:
    """A point cloud opened for header inspection.

    Holds a path, never an array of points. ``las_header`` is the lazy laspy
    header when the format is LAS/LAZ; ``ply_header`` is the parsed text header
    for PLY.
    """

    path: Path
    filename: str
    format: PointCloudFormat
    file_size_bytes: int
    sha256: str | None = None
    point_count: int = 0
    bounds: PointCloudBounds = field(default_factory=PointCloudBounds)
    crs: str = UNKNOWN_CRS
    crs_source: str | None = None
    point_format: str | None = None
    file_version: str | None = None
    generating_software: str | None = None
    system_identifier: str | None = None
    creation_date: str | None = None
    gps_time_range: tuple[float, float] | None = None
    extra_dimensions: list[str] = field(default_factory=list)
    properties: list[str] = field(default_factory=list)
    comments: list[str] = field(default_factory=list)
    byte_order: str | None = None
    text_format: bool = False
    compressed: bool = False
    las_header: Any = None
    ply_header: dict[str, Any] = field(default_factory=dict)

    @property
    def is_las(self) -> bool:
        return self.format in (PointCloudFormat.LAS, PointCloudFormat.LAZ)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def is_point_cloud_filename(filename: str) -> bool:
    """True when the extension names a format this service can inspect."""
    ext = Path(filename or "").suffix.lstrip(".").lower()
    return ext in SUPPORTED_EXTENSIONS


def format_for_filename(filename: str) -> PointCloudFormat:
    """Resolve and validate a point-cloud extension."""
    ext = Path(filename or "").suffix.lstrip(".").lower()
    if not ext:
        raise PointCloudError("Filename has no extension", 400)
    try:
        return SUPPORTED_EXTENSIONS[ext]
    except KeyError:
        raise PointCloudError(
            f"Unsupported point-cloud format {ext!r}; expected one of "
            f"{', '.join(sorted(SUPPORTED_EXTENSIONS))}",
            400,
        ) from None


def file_sha256(path: Path | str) -> str:
    """SHA-256 of a file, read in bounded chunks."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def spool_upload(
    stream: BinaryIO, destination: Path, max_bytes: int, chunk_bytes: int = HASH_CHUNK_BYTES
) -> tuple[int, str]:
    """Copy an upload to disk in chunks, hashing as it goes.

    Streams rather than buffering, so a multi-gigabyte upload never sits in
    memory, and aborts as soon as the cap is exceeded rather than after the fact.

    Returns ``(size_bytes, sha256)``.
    """
    digest = hashlib.sha256()
    total = 0
    with open(destination, "wb") as out:
        while True:
            chunk = stream.read(chunk_bytes)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise PointCloudError("Maximum upload size is 10 MB", 413)
            digest.update(chunk)
            out.write(chunk)
    return total, digest.hexdigest()


# --------------------------------------------------------------------------
# 1. read_point_cloud
# --------------------------------------------------------------------------


def read_point_cloud(
    path: Path | str,
    filename: str | None = None,
    *,
    sha256: str | None = None,
    file_size_bytes: int | None = None,
) -> PointCloudHandle:
    """Open a point cloud and read its header.

    Only the header is touched. Point records are never read, so this is safe for
    files far larger than available memory.
    """
    path = Path(path)
    if not path.exists():
        raise PointCloudError(f"File not found: {path.name}", 404)
    name = filename or path.name
    fmt = format_for_filename(name)
    size = file_size_bytes if file_size_bytes is not None else path.stat().st_size

    if fmt is PointCloudFormat.PLY:
        handle = _read_ply_header(path, name, fmt, size, sha256)
    else:
        handle = _read_las_header(path, name, fmt, size, sha256)
    handle.crs = detect_point_cloud_crs(handle)
    return handle


def _read_las_header(
    path: Path, filename: str, fmt: PointCloudFormat, size: int, sha256: str | None
) -> PointCloudHandle:
    import laspy

    try:
        # Lazy reader: opens the file and parses the header, nothing more.
        reader = laspy.open(path)
    except Exception as exc:  # noqa: BLE001 - normalise library errors
        raise PointCloudError(
            f"Could not read {filename} as {fmt.value}: {exc}", 422
        ) from exc

    try:
        header = reader.header
        point_count = int(header.point_count)
        bounds = PointCloudBounds()
        try:
            mins, maxs = header.mins, header.maxs
            bounds = PointCloudBounds(
                min_x=float(mins[0]),
                min_y=float(mins[1]),
                min_z=float(mins[2]),
                max_x=float(maxs[0]),
                max_y=float(maxs[1]),
                max_z=float(maxs[2]),
            )
        except Exception:  # noqa: BLE001 - some files omit valid bounds
            bounds = PointCloudBounds()

        crs_value, crs_origin = _las_crs(header)
        extra = []
        try:
            extra = sorted({d.name for d in header.point_format.extra_dimensions})
        except Exception:  # noqa: BLE001
            extra = []

        gps_range = None
        try:
            if header.point_format.dimension_by_name("GpsTime") is not None:
                gps_range = (float(header.start_of_waveform_data_offset or 0.0), 0.0)
        except Exception:  # noqa: BLE001
            gps_range = None

        return PointCloudHandle(
            path=path,
            filename=filename,
            format=fmt,
            file_size_bytes=size,
            sha256=sha256,
            point_count=point_count,
            bounds=bounds,
            crs=crs_value,
            crs_source=crs_origin,
            point_format=f"{header.point_format.id}",
            file_version=str(header.version),
            generating_software=header.generating_software or None,
            system_identifier=header.system_identifier or None,
            creation_date=str(header.creation_date) if header.creation_date else None,
            gps_time_range=gps_range,
            extra_dimensions=extra,
            compressed=fmt is PointCloudFormat.LAZ,
            las_header=header,
        )
    finally:
        reader.close()


def _las_crs(header: Any) -> tuple[str, str | None]:
    """CRS declared by a LAS header, plus how it was determined."""
    try:
        crs = header.parse_crs()
    except Exception:  # noqa: BLE001
        crs = None
    if crs is not None:
        try:
            epsg = crs.to_epsg()
            if epsg:
                return f"EPSG:{epsg}", CRS_FROM_VLR
            return crs.to_string(), CRS_FROM_VLR
        except Exception:  # noqa: BLE001
            return UNKNOWN_CRS, CRS_UNDECLARED
    # Fall back to inspecting the VLRs directly, in case parse_crs is silent.
    for vlr in getattr(header, "vlrs", []) or []:
        name = type(vlr).__name__
        if "WktCoordinateSystem" in name:
            try:
                return crs_authority(vlr.string), CRS_FROM_VLR
            except Exception:  # noqa: BLE001
                continue
        if "GeoKeyDirectory" in name:
            try:
                for key in vlr.geo_keys:
                    if key.key_id == 3072 and key.location_id > 0:  # ProjectedCSTypeGeoKey
                        return f"EPSG:{key.value}", CRS_FROM_GEOKEY
            except Exception:  # noqa: BLE001
                continue
    return UNKNOWN_CRS, CRS_UNDECLARED


def _read_ply_header(
    path: Path, filename: str, fmt: PointCloudFormat, size: int, sha256: str | None
) -> PointCloudHandle:
    """Parse a PLY header incrementally.

    ``plyfile`` would load every vertex via ``PlyData.read``, which is exactly
    what this milestone must avoid, so the bounded ASCII header is parsed here.
    """
    with open(path, "rb") as handle:
        raw = handle.read(MAX_PLY_HEADER_BYTES)
    marker = b"end_header"
    if not raw.startswith(b"ply"):
        raise PointCloudError(f"{filename} is not a PLY file (missing magic)", 422)
    if marker not in raw:
        raise PointCloudError(f"{filename} has no complete PLY header", 422)
    text = raw.split(marker, 1)[0].decode("ascii", errors="replace")

    elements: list[tuple[str, int]] = []
    properties: list[str] = []
    comments: list[str] = []
    ply_format: str | None = None
    version: str | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("format"):
            parts = line.split()
            if len(parts) >= 2:
                ply_format = parts[1]
                version = parts[2] if len(parts) > 2 else None
            continue
        if line.startswith("comment"):
            match = _PLY_COMMENT_RE.match(line)
            if match:
                comments.append(match.group(1).strip())
            continue
        match = _PLY_ELEMENT_RE.match(line)
        if match:
            elements.append((match.group(1), int(match.group(2))))
            continue
        match = _PLY_PROPERTY_RE.match(line)
        if match:
            properties.append(match.group(3) or match.group(2))

    vertex_count = next((n for name, n in elements if name == "vertex"), 0)

    # A PLY carries no standard place for a CRS; some writers put it in a
    # comment. Read it if present, otherwise report it as undeclared.
    crs_value, crs_origin = _ply_crs(comments)

    return PointCloudHandle(
        path=path,
        filename=filename,
        format=fmt,
        file_size_bytes=size,
        sha256=sha256,
        point_count=vertex_count,
        bounds=PointCloudBounds(),  # PLY declares no extent
        crs=crs_value,
        crs_source=crs_origin,
        file_version=version,
        properties=properties,
        comments=comments,
        byte_order="<" if ply_format and "little_endian" in ply_format else ">",
        text_format=bool(ply_format and ply_format.startswith("ascii")),
        ply_header={
            "format": ply_format,
            "elements": elements,
        },
    )


_CRS_COMMENT_PREFIXES = ("crs", "coordinate_reference_system", "srs", "proj")


def _ply_crs(comments: Iterable[str]) -> tuple[str, str | None]:
    """Look for a CRS declaration in PLY comment lines."""
    for comment in comments:
        text = comment.strip()
        lowered = text.lower()
        for prefix in _CRS_COMMENT_PREFIXES:
            if lowered.startswith(prefix):
                candidate = text[len(prefix) :].lstrip(" :=,")
                candidate = candidate.strip().strip("\"'")
                if candidate:
                    try:
                        return crs_authority(candidate), CRS_FROM_COMMENT
                    except Exception:  # noqa: BLE001
                        try:
                            from pyproj import CRS as PyCRS

                            return crs_authority(PyCRS.from_user_input(candidate)), (
                                CRS_FROM_COMMENT
                            )
                        except Exception:  # noqa: BLE001
                            return UNKNOWN_CRS, CRS_UNDECLARED
    return UNKNOWN_CRS, CRS_UNDECLARED


# --------------------------------------------------------------------------
# 2-6. metadata, CRS, bounds, count, density
# --------------------------------------------------------------------------


def calculate_point_count(pc: PointCloudHandle) -> int:
    """Number of points, taken from the header. Never counts by reading points."""
    return int(pc.point_count)


def calculate_point_cloud_bounds(pc: PointCloudHandle) -> PointCloudBounds:
    """Extent in the file's own CRS.

    PLY has no extent field, so this is undefined there — reported as all-``None``
    rather than guessed from a sample.
    """
    return pc.bounds


def detect_point_cloud_crs(pc: PointCloudHandle) -> str:
    """CRS declared by the file, or :data:`UNKNOWN_CRS`.

    Deliberately never defaults to WGS84.
    """
    return pc.crs or UNKNOWN_CRS


def processing_crs_for_bounds(
    bounds: PointCloudBounds, source_crs: str
) -> str | None:
    """Metric CRS for a known source extent, without inventing one for unknowns."""
    if not bounds.is_defined or not source_crs or source_crs == UNKNOWN_CRS:
        return None
    try:
        return select_processing_crs(
            point=(
                ((bounds.min_x or 0.0) + (bounds.max_x or 0.0)) / 2.0,
                ((bounds.min_y or 0.0) + (bounds.max_y or 0.0)) / 2.0,
            ),
            source_crs=source_crs,
        )
    except Exception:  # noqa: BLE001 - metadata inspection must report ambiguity
        return None


def _bounds_area_m2(
    bounds: PointCloudBounds, source_crs: str, processing_crs: str
) -> float | None:
    """Plan area of the source extent after an explicit metric transform."""
    if not bounds.is_defined:
        return None
    corners_x = [bounds.min_x, bounds.max_x, bounds.max_x, bounds.min_x]
    corners_y = [bounds.min_y, bounds.min_y, bounds.max_y, bounds.max_y]
    try:
        xs, ys = transform_coordinates(
            corners_x, corners_y, source_crs, processing_crs
        )
    except Exception:  # noqa: BLE001 - invalid CRS/bounds means no density claim
        return None
    # Shoelace area preserves the transformed quadrilateral. Using width *
    # height after transforming only its extrema would be wrong in a rotated or
    # non-linear projection.
    area = abs(
        sum(
            xs[index] * ys[(index + 1) % 4]
            - ys[index] * xs[(index + 1) % 4]
            for index in range(4)
        )
    ) / 2.0
    return float(area) if area > 0 else None


def calculate_point_density(pc: PointCloudHandle) -> float | None:
    """Points per square metre of plan extent.

    ``None`` when the plan area is zero (a purely vertical scan) or unknown,
    because an infinite or invented density is worse than no answer.
    """
    bounds = pc.bounds
    if not bounds.is_defined or not pc.point_count:
        return None
    processing_crs = processing_crs_for_bounds(bounds, detect_point_cloud_crs(pc))
    if processing_crs is None:
        return None
    area = _bounds_area_m2(bounds, detect_point_cloud_crs(pc), processing_crs)
    if area is None:
        return None
    return pc.point_count / area


def point_density_for_bounds(
    bounds: PointCloudBounds, source_crs: str, point_count: int
) -> float | None:
    """Header-derived density with the source CRS made explicit."""
    processing_crs = processing_crs_for_bounds(bounds, source_crs)
    if processing_crs is None or not point_count:
        return None
    area = _bounds_area_m2(bounds, source_crs, processing_crs)
    return (point_count / area) if area else None


def _display_bounds(bounds: PointCloudBounds, crs: str) -> PointCloudBounds | None:
    """Reproject bounds to WGS84 for display, when a CRS is known."""
    if not bounds.is_defined or not crs or crs == UNKNOWN_CRS:
        return None
    try:
        corners = [
            (bounds.min_x, bounds.min_y),
            (bounds.max_x, bounds.min_y),
            (bounds.min_x, bounds.max_y),
            (bounds.max_x, bounds.max_y),
        ]
        xs, ys = [], []
        for lon, lat in corners:
            x, y = transform_point(lon, lat, crs, WGS84)
            xs.append(x)
            ys.append(y)
        return PointCloudBounds(
            min_x=round(min(xs), DISPLAY_PRECISION),
            min_y=round(min(ys), DISPLAY_PRECISION),
            min_z=bounds.min_z,
            max_x=round(max(xs), DISPLAY_PRECISION),
            max_y=round(max(ys), DISPLAY_PRECISION),
            max_z=bounds.max_z,
        )
    except Exception:  # noqa: BLE001 - display is best-effort
        return None


def display_bounds_for(
    bounds: PointCloudBounds, crs: str
) -> PointCloudBounds | None:
    """Public entry point for reprojecting bounds to WGS84.

    Used by the ingestion pipeline when a caller supplies a CRS for a file that
    declared none, which makes the geometry interpretable after the fact.
    """
    return _display_bounds(bounds, crs)


def _acquisition(pc: PointCloudHandle) -> dict[str, Any]:
    """Provenance the file carries about how/when it was captured."""
    info: dict[str, Any] = {
        "generating_software": pc.generating_software,
        "system_identifier": pc.system_identifier,
        "creation_date": pc.creation_date,
        "comments": pc.comments or None,
    }
    if pc.gps_time_range and pc.gps_time_range[1] > pc.gps_time_range[0]:
        info["gps_time_range"] = list(pc.gps_time_range)
    return {k: v for k, v in info.items() if v is not None}


def get_point_cloud_metadata(pc: PointCloudHandle) -> PointCloudMetadata:
    """Full metadata for a point cloud, assembled from the header.

    Every field is header-derived; the point records are never read.
    """
    crs = detect_point_cloud_crs(pc)
    processing_crs = processing_crs_for_bounds(pc.bounds, crs)
    return PointCloudMetadata(
        filename=pc.filename,
        format=pc.format.value,
        file_size_bytes=pc.file_size_bytes,
        sha256=pc.sha256,
        compressed=pc.compressed,
        point_count=calculate_point_count(pc),
        bounds=calculate_point_cloud_bounds(pc),
        crs=crs,
        crs_source=pc.crs_source,
        processing_crs=processing_crs,
        display_crs=WGS84,
        display_bounds=_display_bounds(pc.bounds, crs),
        point_format=pc.point_format,
        file_version=pc.file_version,
        generating_software=pc.generating_software,
        system_identifier=pc.system_identifier,
        creation_date=pc.creation_date,
        gps_time_range=pc.gps_time_range,
        extra_dimensions=pc.extra_dimensions,
        properties=pc.properties,
        comments=pc.comments,
        byte_order=pc.byte_order,
        text_format=pc.text_format,
        density_points_per_m2=point_density_for_bounds(
            pc.bounds, crs, calculate_point_count(pc)
        ),
        acquisition=_acquisition(pc),
    )


# --------------------------------------------------------------------------
# 7. validate_point_cloud
# --------------------------------------------------------------------------


def validate_point_cloud(pc: PointCloudHandle) -> PointCloudValidation:
    """Check a point cloud for structural problems.

    Structural only — a file can be perfectly valid LAS and still be useless
    survey data (no CRS, no extent). Those are warnings, not errors, because they
    do not prevent reading it.
    """
    problems: list[str] = []
    warnings: list[str] = []

    if pc.file_size_bytes <= 0:
        problems.append("File is empty")
    if pc.point_count <= 0:
        problems.append("File declares zero points")
    if pc.is_las:
        if not pc.bounds.is_defined:
            warnings.append("Header declares no extent; bounds unavailable")
        elif pc.bounds.min_x == pc.bounds.max_x and pc.bounds.min_y == pc.bounds.max_y:
            warnings.append("Zero-area plan extent; point density undefined")
    else:
        warnings.append(
            "PLY does not declare an extent; bounds must be computed from points"
        )
    if detect_point_cloud_crs(pc) == UNKNOWN_CRS:
        warnings.append(
            "No CRS declared; coordinates cannot be interpreted without a caller-supplied CRS"
        )
    if not pc.properties and not pc.extra_dimensions:
        warnings.append("No per-point attributes discovered")

    return PointCloudValidation(
        is_valid=not problems, problems=problems, warnings=warnings
    )


__all__ = [
    "CRS_FROM_COMMENT",
    "CRS_FROM_GEOKEY",
    "CRS_FROM_VLR",
    "CRS_UNDECLARED",
    "HASH_CHUNK_BYTES",
    "MAX_PLY_HEADER_BYTES",
    "SUPPORTED_EXTENSIONS",
    "PointCloudError",
    "PointCloudHandle",
    "calculate_point_cloud_bounds",
    "calculate_point_count",
    "calculate_point_density",
    "detect_point_cloud_crs",
    "display_bounds_for",
    "file_sha256",
    "format_for_filename",
    "get_point_cloud_metadata",
    "is_point_cloud_filename",
    "point_density_for_bounds",
    "processing_crs_for_bounds",
    "read_point_cloud",
    "spool_upload",
    "validate_point_cloud",
]
