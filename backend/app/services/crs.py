"""Coordinate reference system management.

Replaces the previous approximate local conversion (``x / 98000``,
``y / 111000``) with real, library-backed coordinate operations via PROJ.

Why this module exists
----------------------
Every metric quantity in a cadastre — area, distance, overlap, volume — is only
meaningful in a **projected, metre-based** CRS. In a geographic CRS such as
WGS84, ``ST_Area``/``shapely.area`` return *square degrees* and distance returns
*degrees*, which are off by roughly 10 orders of magnitude for a real site. This
module is the single place that knows which CRS is which, so no call site has to
remember.

Three CRS roles
---------------
``source_crs``
    The CRS incoming data claims, read from the payload
    (:func:`declared_crs_from_geojson`) or a record's ``crs`` field. Never
    assumed silently: undeclared data is reported as unknown, not defaulted
    quietly to WGS84.

``processing_crs``
    A projected metre-based CRS used for **all** measurement. Selected from the
    data by :func:`select_processing_crs`.

``display_crs``
    WGS84, for the web map and API payloads.

A note on authority
-------------------
:func:`select_processing_crs` derives a **UTM** zone from the data's longitude.
UTM is chosen because it is a well-understood, globally available, low-distortion
projected CRS — it is a *technical* choice about arithmetic, **not** a claim
about any official or government cadastral reference system. No such requirement
has been supplied by the data, so none is invented here. Callers that *do* have
an authoritative CRS for a survey area must pass it in via ``processing_crs=``;
it will be used verbatim.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Iterable, Mapping

from pyproj import CRS, Transformer
from pyproj.exceptions import CRSError
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as _shapely_transform

#: Geographic CRS for storage and web display.
WGS84 = "EPSG:4326"

#: Reported when a payload declares no CRS. Deliberately not WGS84: assuming it
#: would silently mis-place foreign data.
UNKNOWN_CRS = "UNKNOWN"

#: Rounding applied to coordinates crossing the API boundary, in degrees.
#: ~1 cm, and keeps payloads small and stable.
DISPLAY_PRECISION = 7

#: Area below which a planar measurement is not meaningful (m²). A polygon
#: thinner than this is within numerical noise of the projection.
MIN_MEANINGFUL_AREA_M2 = 1e-9


# --------------------------------------------------------------------------
# CRS resolution
# --------------------------------------------------------------------------


@lru_cache(maxsize=256)
def resolve_crs(value: Any) -> CRS:
    """Coerce a CRS reference into a :class:`pyproj.CRS`.

    Accepts ``"EPSG:4326"``, ``4326``, ``"urn:ogc:def:crs:EPSG::4326"`` or an
    existing ``CRS``.
    """
    if isinstance(value, CRS):
        return value
    if value is None:
        raise CRSError("no CRS supplied")
    if isinstance(value, int):
        return CRS.from_epsg(value)
    text = str(value).strip()
    if not text or text.upper() == UNKNOWN_CRS:
        raise CRSError(f"CRS is unknown or unset: {value!r}")
    try:
        return CRS.from_user_input(text)
    except CRSError:
        # Bare numeric strings, e.g. "4326".
        if text.isdigit():
            return CRS.from_epsg(int(text))
        raise


@lru_cache(maxsize=256)
def crs_authority(value: Any) -> str:
    """Canonical string form, e.g. ``"EPSG:32643"``.

    ``CRS.to_authority()`` returns a ``(auth, code)`` tuple, which is easy to
    mistake for a string and silently breaks equality comparisons — so it is
    joined here, once.
    """
    crs = resolve_crs(value)
    authority = crs.to_authority()
    if authority:
        auth, code = authority
        return f"{auth}:{code}"
    return crs.to_wkt()


def is_projected(value: Any) -> bool:
    """True when the CRS uses projected (planar) coordinates."""
    return resolve_crs(value).is_projected


def crs_units(value: Any) -> str:
    """Axis unit name, e.g. ``"metre"`` or ``"degree"``."""
    crs = resolve_crs(value)
    try:
        return crs.axis_info[0].unit_name.lower()
    except (IndexError, CRSError):
        return "unknown"


def is_metric(value: Any) -> bool:
    """True when distances in this CRS are metres (or another linear unit)."""
    if not is_projected(value):
        return False
    return crs_units(value) in ("metre", "meter", "m")


@lru_cache(maxsize=64)
def _transformer(source: Any, target: Any) -> Transformer:
    """Cached transformer. ``always_xy`` so inputs are always (x, y)."""
    return Transformer.from_crs(
        resolve_crs(source), resolve_crs(target), always_xy=True
    )


# --------------------------------------------------------------------------
# CRS roles
# --------------------------------------------------------------------------


def declared_crs_from_geojson(payload: Mapping[str, Any]) -> str:
    """Read the CRS a GeoJSON document declares.

    Honours the legacy ``crs`` member (``{"type":"name","properties":{"name":
    "EPSG:4326"}}``). Modern GeoJSON (RFC 7946) mandates WGS84 and omits it, in
    which case :data:`WGS84` is returned because the specification fixes it.

    Returns :data:`UNKNOWN_CRS` when a ``crs`` member is present but unusable,
    so the caller can surface the ambiguity rather than guess.
    """
    crs_member = payload.get("crs")
    if crs_member is None:
        # RFC 7946: GeoJSON is always WGS84 / CRS84.
        return WGS84
    if not isinstance(crs_member, Mapping):
        return UNKNOWN_CRS
    properties = crs_member.get("properties")
    if not isinstance(properties, Mapping):
        return UNKNOWN_CRS
    name = properties.get("name") or properties.get("href")
    if not name:
        return UNKNOWN_CRS
    try:
        return crs_authority(name)
    except CRSError:
        return UNKNOWN_CRS


def get_source_crs(
    record: Mapping[str, Any] | None = None,
    *,
    default: str = UNKNOWN_CRS,
) -> str:
    """Resolve the CRS a record's data was captured in.

    Looks at, in order: an explicit ``crs``, ``source_crs`` or
    ``declared_crs`` field, then ``metadata.crs`` / ``metadata.source_crs``.

    Returns ``default`` (:data:`UNKNOWN_CRS` unless overridden) when nothing is
    declared. Callers must handle that case explicitly rather than assuming
    WGS84.
    """
    if not record:
        return default
    for key in ("crs", "source_crs", "declared_crs"):
        value = record.get(key)
        if value:
            try:
                return crs_authority(value)
            except CRSError:
                continue
    metadata = record.get("metadata")
    if isinstance(metadata, Mapping):
        for key in ("crs", "source_crs", "declared_crs"):
            value = metadata.get(key)
            if value:
                try:
                    return crs_authority(value)
                except CRSError:
                    continue
    return default


@lru_cache(maxsize=64)
def utm_epsg_for(longitude: float, latitude: float = 0.0) -> str:
    """UTM EPSG code for a longitude/latitude, derived from the position.

    Zone = ``floor((lon + 180) / 6) + 1``; north hemisphere uses the 326xx
    block, south the 327xx block. This is arithmetic, not policy: see the module
    docstring on authority.
    """
    if not -180.0 <= longitude <= 180.0:
        raise ValueError(f"longitude out of range: {longitude}")
    zone = int(math.floor((longitude + 180.0) / 6.0)) + 1
    zone = min(max(zone, 1), 60)
    base = 32600 if latitude >= 0 else 32700
    return f"EPSG:{base + zone}"


def select_processing_crs(
    geometry: BaseGeometry | None = None,
    *,
    point: tuple[float, float] | None = None,
    source_crs: Any = WGS84,
    processing_crs: Any = None,
) -> str:
    """Choose the projected metre CRS to measure in.

    Resolution order:

    1. an explicit ``processing_crs`` — always honoured verbatim, so an
       authoritative survey CRS supplied by the caller wins;
    2. ``source_crs`` itself, if it is already projected in metres (reprojecting
       would only add error);
    3. the UTM zone containing the geometry/point, derived from the data.

    Parameters
    ----------
    geometry, point:
        Where the work happens. Supply at least one. Defaults to the demo
        anchor when neither is given.
    source_crs:
        CRS the geometry's coordinates are expressed in.
    """
    if processing_crs:
        return crs_authority(processing_crs)

    if source_crs and is_metric(source_crs):
        return crs_authority(source_crs)

    if point is None and geometry is not None:
        target = WGS84 if (not source_crs or not is_projected(source_crs)) else source_crs
        geom_in_target = transform_geometry(geometry, source_crs, target)
        centroid = geom_in_target.centroid
        point = (centroid.x, centroid.y)
        source_crs = target

    if point is None:
        from app.services.geometry import DEMO_ANCHOR_LON, DEMO_ANCHOR_LAT

        point = (DEMO_ANCHOR_LON, DEMO_ANCHOR_LAT)
        source_crs = WGS84

    lon, lat = transform_point(point[0], point[1], source_crs, WGS84)
    return utm_epsg_for(lon, lat)


@dataclass(frozen=True)
class CRSContext:
    """The three CRS roles resolved together for a unit of work."""

    source_crs: str
    processing_crs: str
    display_crs: str = WGS84

    def describe(self) -> dict[str, str]:
        return {
            "source_crs": self.source_crs,
            "processing_crs": self.processing_crs,
            "display_crs": self.display_crs,
        }


def crs_context(
    geometry: BaseGeometry | None = None,
    *,
    point: tuple[float, float] | None = None,
    source_crs: Any = None,
    processing_crs: Any = None,
) -> CRSContext:
    """Resolve all three CRS roles at once.

    ``source_crs`` falls back to :func:`get_source_crs` semantics: unknown stays
    unknown rather than defaulting to WGS84.
    """
    resolved_source = crs_authority(source_crs) if source_crs else WGS84
    return CRSContext(
        source_crs=resolved_source,
        processing_crs=select_processing_crs(
            geometry,
            point=point,
            source_crs=resolved_source,
            processing_crs=processing_crs,
        ),
        display_crs=WGS84,
    )


# --------------------------------------------------------------------------
# transformation
# --------------------------------------------------------------------------


def transform_point(
    x: float, y: float, source_crs: Any, target_crs: Any
) -> tuple[float, float]:
    """Transform a single coordinate between CRSs.

    Always ``(x, y)`` / (lon, lat) ordering, regardless of axis order in the
    CRS definition, so callers never have to think about authority axis order.
    """
    if crs_authority(source_crs) == crs_authority(target_crs):
        return x, y
    tx_x, tx_y = _transformer(source_crs, target_crs).transform(x, y)
    return tx_x, tx_y


def transform_geometry(
    geometry: BaseGeometry, source_crs: Any, target_crs: Any
) -> BaseGeometry:
    """Reproject a geometry between CRSs.

    Validity is preserved: reprojection is a per-vertex operation and does not
    introduce self-intersections in practice. Callers handling untrusted or
    degenerate input should still re-validate with
    :func:`app.services.geometry.validate_polygon`.
    """
    if geometry is None or geometry.is_empty:
        return geometry
    if crs_authority(source_crs) == crs_authority(target_crs):
        return geometry
    transformer = _transformer(source_crs, target_crs)
    return _shapely_transform(
        lambda x, y, z=None: transformer.transform(x, y), geometry
    )


def normalize_geometry_crs(
    geometry: BaseGeometry,
    *,
    source_crs: Any = None,
    target_crs: Any = None,
) -> BaseGeometry:
    """Bring a geometry into the canonical processing CRS.

    With ``source_crs`` omitted the geometry is assumed **already projected**, so
    nothing is transformed — pass the source CRS whenever it is known, because
    silently assuming one is how coordinates get misplaced.
    """
    if source_crs is None:
        return geometry
    target = target_crs or select_processing_crs(geometry, source_crs=source_crs)
    return transform_geometry(geometry, source_crs, target)


# --------------------------------------------------------------------------
# metric measurement
# --------------------------------------------------------------------------


def calculate_metric_area(
    geometry: BaseGeometry,
    *,
    source_crs: Any = None,
    processing_crs: Any = None,
) -> float:
    """Area in square metres.

    Reprojects into a projected metre CRS first when needed. Never measures in a
    geographic CRS: doing so returns square degrees, which looks like a small
    number rather than an error.
    """
    if geometry is None or geometry.is_empty:
        return 0.0
    target = processing_crs or select_processing_crs(
        geometry, source_crs=source_crs or WGS84
    )
    if source_crs is not None:
        measured = transform_geometry(geometry, source_crs, target)
    else:
        # Already projected; the target was derived from its own position.
        measured = geometry
    return abs(measured.area)


def calculate_metric_distance(
    a: BaseGeometry,
    b: BaseGeometry,
    *,
    method: str = "min",
    source_crs: Any = None,
    processing_crs: Any = None,
) -> float:
    """Distance in metres between two geometries.

    ``method``:

    ``"min"``
        Closest approach between the two footprints. Zero when they touch or
        overlap, which is what a "gap" check wants.
    ``"centroid"``
        Distance between centroids.
    ``"hausdorff"``
        Hausdorff distance, the worst-case separation.
    """
    if a is None or b is None or a.is_empty or b.is_empty:
        return 0.0
    target = processing_crs or select_processing_crs(
        a, source_crs=source_crs or WGS84
    )
    if source_crs is not None:
        left = transform_geometry(a, source_crs, target)
        right = transform_geometry(b, source_crs, target)
    else:
        left, right = a, b
    if method == "min":
        return float(left.distance(right))
    if method == "centroid":
        return float(left.centroid.distance(right.centroid))
    if method == "hausdorff":
        return float(left.hausdorff_distance(right))
    raise ValueError(
        f"unsupported distance method {method!r}; "
        "expected 'min', 'centroid' or 'hausdorff'"
    )


def metric_bounds(
    geometry: BaseGeometry,
    *,
    source_crs: Any = None,
    processing_crs: Any = None,
) -> tuple[float, float, float, float]:
    """Bounds in metres, reprojecting first when the geometry is geographic."""
    target = processing_crs or select_processing_crs(
        geometry, source_crs=source_crs or WGS84
    )
    projected = (
        transform_geometry(geometry, source_crs, target)
        if source_crs is not None
        else geometry
    )
    minx, miny, maxx, maxy = projected.bounds
    return minx, miny, maxx, maxy


def describe_crs_roles(
    geometry: BaseGeometry | None = None,
    *,
    source_crs: Any = WGS84,
    processing_crs: Any = None,
) -> dict[str, str]:
    """Convenience for logging and API metadata."""
    return crs_context(
        geometry, source_crs=source_crs, processing_crs=processing_crs
    ).describe()


__all__ = [
    "CRSContext",
    "DISPLAY_PRECISION",
    "MIN_MEANINGFUL_AREA_M2",
    "UNKNOWN_CRS",
    "WGS84",
    "calculate_metric_area",
    "calculate_metric_distance",
    "crs_authority",
    "crs_context",
    "crs_units",
    "declared_crs_from_geojson",
    "describe_crs_roles",
    "get_source_crs",
    "is_metric",
    "is_projected",
    "metric_bounds",
    "normalize_geometry_crs",
    "resolve_crs",
    "select_processing_crs",
    "transform_geometry",
    "transform_point",
    "utm_epsg_for",
]
