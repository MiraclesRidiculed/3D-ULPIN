"""Geometry engine — the single place cadastral geometry is computed.

Everything spatial funnels through here: the CRS transform, validity, measures,
Z-range handling, topology, volumetric intersection and geometry identity. No
other module should touch Shapely directly.

Coordinate reference system
---------------------------
The demo scene is authored in **local projected metres**: rectangles anchored at
a real point and expressed as offsets from it. That plane is a translation within
a genuine projected metre CRS (UTM zone 43N, derived from the anchor by
:mod:`app.services.crs`), so:

* metric measures over it are exact, correct to within UTM's small distortion
  across a site;
* conversion to and from WGS84 is a real projection through PROJ, not a
  fixed-divisor approximation. The previous ``x / 98000`` shortcut was wrong by
  ~14 cm over 80 m here, and the error grows with distance from the anchor.

Because the local plane *is* the processing CRS up to translation, measuring
directly on it is equivalent to measuring after reprojection — and vastly better
than measuring in degrees. See :mod:`app.services.crs` for the three CRS roles
and for why no government CRS is assumed.

Internal polygons here are always in **local projected metres**, never degrees.
GeoJSON payloads are always WGS84.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Iterable, Mapping, NamedTuple, Sequence

import shapely
from shapely import set_precision
from shapely.affinity import translate as shapely_translate
from shapely.errors import GEOSException
from shapely.geometry import LineString, MultiLineString, MultiPoint, MultiPolygon, Point, Polygon, box
from shapely.geometry.base import BaseGeometry
from shapely.ops import polygonize, unary_union
from shapely.validation import explain_validity

from app.services import crs as crs_service
from app.utils import now


def create_rectangle(x_min: float, y_min: float, x_max: float, y_max: float) -> Polygon:
    """Axis-aligned rectangle in local projected metres, from its bounds.

    Lets callers construct seed/demo geometry without importing Shapely, so this
    module stays the only place that does.
    """
    return box(x_min, y_min, x_max, y_max)


# --------------------------------------------------------------------------
# CRS and the local projected plane
# --------------------------------------------------------------------------

#: Where the demo city sits, in WGS84. A real anchor, not a synthetic origin.
DEMO_ANCHOR_LON, DEMO_ANCHOR_LAT = 77.2089, 28.6131

#: Projected metre CRS the local plane lives in, derived from the anchor.
PROCESSING_CRS: str = crs_service.utm_epsg_for(DEMO_ANCHOR_LON, DEMO_ANCHOR_LAT)

#: Authoritative storage / display CRS.
GEOGRAPHIC_CRS: str = crs_service.WGS84

#: Easting/northing of the anchor inside :data:`PROCESSING_CRS`. Local plane
#: coordinates are offsets in metres from this point.
_ANCHOR_EASTING, _ANCHOR_NORTHING = crs_service.transform_point(
    DEMO_ANCHOR_LON, DEMO_ANCHOR_LAT, GEOGRAPHIC_CRS, PROCESSING_CRS
)

#: Decimal places kept when writing WGS84 coordinates (~1 cm).
WGS84_PRECISION = crs_service.DISPLAY_PRECISION

#: Decimal places used when hashing, so the hash survives float noise.
HASH_PRECISION = 3


def xy_to_ll(x: float, y: float) -> list[float]:
    """Local projected metres -> WGS84 ``[lon, lat]`` through PROJ."""
    lon, lat = crs_service.transform_point(
        _ANCHOR_EASTING + x,
        _ANCHOR_NORTHING + y,
        PROCESSING_CRS,
        GEOGRAPHIC_CRS,
    )
    return [round(lon, WGS84_PRECISION), round(lat, WGS84_PRECISION)]


def ll_to_xy(lon: float, lat: float) -> tuple[float, float]:
    """WGS84 ``[lon, lat]`` -> local projected metres through PROJ."""
    easting, northing = crs_service.transform_point(
        lon, lat, GEOGRAPHIC_CRS, PROCESSING_CRS
    )
    return easting - _ANCHOR_EASTING, northing - _ANCHOR_NORTHING


def local_to_wgs84(geometry: BaseGeometry) -> BaseGeometry:
    """Local projected metres -> WGS84 geometry, for display.

    The local plane is :data:`PROCESSING_CRS` **translated** so the anchor sits
    at (0, 0), so the anchor offset must be re-applied before reprojecting.
    Getting this wrong places geometry at the projection's false origin rather
    than at the site.
    """
    if geometry is None or geometry.is_empty:
        return geometry
    shifted = shapely_translate(
        geometry, xoff=_ANCHOR_EASTING, yoff=_ANCHOR_NORTHING
    )
    return crs_service.transform_geometry(shifted, PROCESSING_CRS, GEOGRAPHIC_CRS)


def wgs84_to_local(geometry: BaseGeometry) -> BaseGeometry:
    """WGS84 geometry -> local projected metres, inverse of :func:`local_to_wgs84`."""
    if geometry is None or geometry.is_empty:
        return geometry
    projected = crs_service.transform_geometry(geometry, GEOGRAPHIC_CRS, PROCESSING_CRS)
    return shapely_translate(
        projected, xoff=-_ANCHOR_EASTING, yoff=-_ANCHOR_NORTHING
    )


def geojson_to_polygon(geo: Mapping[str, Any]) -> Polygon:
    """GeoJSON Polygon mapping -> Shapely polygon in the local plane.

    Only the exterior ring is read; interior rings (holes) are ignored, which
    matches the record contract (a footprint plus separate ``z_min``/``z_max``).
    """
    return Polygon([ll_to_xy(pt[0], pt[1]) for pt in geo["coordinates"][0]])


def wgs84_geojson_to_polygon(geo: Mapping[str, Any]) -> Polygon:
    """WGS84 GeoJSON Polygon mapping -> Shapely polygon **in degrees**.

    The counterpart of :func:`polygon_to_geojson`, and deliberately distinct from
    :func:`geojson_to_polygon`, which converts into the local projected plane.
    Use this when the input really is WGS84 -- for example a geometry just read
    back from storage -- and pair it with an explicit ``source_crs``.

    Only the exterior ring is read, matching the record contract.
    """
    return Polygon([(float(pt[0]), float(pt[1])) for pt in geo["coordinates"][0]])


def polygon_to_geojson(polygon: Polygon) -> dict[str, Any]:
    """Shapely polygon in the local plane -> GeoJSON mapping in WGS84."""
    return {
        "type": "Polygon",
        "coordinates": [
            [xy_to_ll(x, y) for x, y in polygon.exterior.coords]
        ],
    }


def shape_to_geojson(shape: Any) -> dict[str, Any] | None:
    """Any polygonal shape in the local plane -> GeoJSON, or ``None`` if empty.

    Unlike :func:`polygon_to_geojson` this keeps **interior rings** and handles a
    MultiPolygon. A symmetric difference between two footprints is routinely
    multi-part -- a hole left where a building was removed, a sliver on both sides
    of a moved boundary -- and a serialiser emitting only the exterior ring would
    silently fill the hole back in, showing a reviewer the *opposite* of the region
    that changed.

    ``None`` rather than an empty polygon for an empty shape, so "nothing left"
    stays distinguishable from "a region of no area".
    """
    if shape is None or getattr(shape, "is_empty", True):
        return None
    kind = shape.geom_type
    if kind == "Polygon":
        return {
            "type": "Polygon",
            "coordinates": [
                [xy_to_ll(x, y) for x, y in shape.exterior.coords],
                *[[xy_to_ll(x, y) for x, y in ring.coords] for ring in shape.interiors],
            ],
        }
    if kind == "MultiPolygon":
        parts = [shape_to_geojson(part) for part in shape.geoms]
        parts = [part for part in parts if part]
        if not parts:
            return None
        return {"type": "MultiPolygon", "coordinates": [p["coordinates"] for p in parts]}
    if kind == "GeometryCollection":
        parts = [
            shape_to_geojson(part)
            for part in shape.geoms
            if part.geom_type in ("Polygon", "MultiPolygon")
        ]
        parts = [part for part in parts if part]
        if not parts:
            return None
        if len(parts) == 1:
            return parts[0]
        return {"type": "GeometryCollection", "geometries": parts}
    return None


def record_to_polygon(record: Mapping[str, Any]) -> Polygon:
    """Extract a record's footprint as a local-plane polygon.

    Looks for a GeoJSON polygon among ``geometry_3d`` (property volumes,
    infrastructure), ``geometry`` (parcels) and ``footprint`` (buildings), taking
    the first that actually carries ring coordinates.

    The shape check matters: a building's ``geometry_3d`` is a wrapper object
    holding ``z_min``/``z_max``/``footprint`` rather than a GeoJSON polygon, so
    presence of the key alone is not enough.
    """
    for field in ("geometry_3d", "geometry", "footprint"):
        value = record.get(field)
        if isinstance(value, Mapping) and "coordinates" in value:
            return geojson_to_polygon(value)
    raise KeyError(
        f"record {record.get('id')!r} has no GeoJSON footprint in "
        f"geometry_3d/geometry/footprint"
    )


# --------------------------------------------------------------------------
# Validity and repair
# --------------------------------------------------------------------------


class PolygonValidation(NamedTuple):
    """Outcome of a polygon validity check."""

    is_valid: bool
    reason: str | None


def validate_polygon(polygon: BaseGeometry) -> PolygonValidation:
    """Check OGC validity, returning a human-readable reason when invalid."""
    if polygon.is_valid:
        return PolygonValidation(True, None)
    return PolygonValidation(False, explain_validity(polygon))


def repair_polygon(polygon: BaseGeometry) -> BaseGeometry:
    """Best-effort repair of an invalid polygon via a zero-width buffer.

    Returns the input unchanged when it is already valid, when the repair
    fails, or when the repair collapses the geometry to something other than a
    single polygon. Never raises; callers should re-validate the result.
    """
    if polygon.is_valid:
        return polygon
    try:
        repaired = polygon.buffer(0)
    except Exception:  # noqa: BLE001 - repair is best-effort by contract
        return polygon
    if isinstance(repaired, (Polygon, MultiPolygon)) and repaired.is_valid:
        return repaired
    return polygon


# --------------------------------------------------------------------------
# Measures
# --------------------------------------------------------------------------


def calculate_area(polygon: BaseGeometry) -> float:
    """Planar area in square metres.

    The polygon is already in local projected metres, so this is a direct
    measurement — no reprojection needed, and never a measurement in degrees.
    """
    return abs(polygon.area)


def calculate_centroid(polygon: BaseGeometry) -> tuple[float, float]:
    """Planar centroid ``(x, y)`` in local plane metres."""
    c = polygon.centroid
    return c.x, c.y


def calculate_volume(polygon: BaseGeometry, z_min: float, z_max: float) -> float:
    """Prismatic volume in cubic metres: plan area x vertical extent.

    Deliberately unclamped, so an inverted Z range yields a negative value
    rather than a silent zero. Validate the range with
    :func:`validate_z_range` before trusting a volume.
    """
    return calculate_area(polygon) * (z_max - z_min)


def calculate_height_difference(a: Mapping[str, Any], b: Mapping[str, Any]) -> float:
    """Signed difference in top elevation, ``a.z_max - b.z_max`` (metres).

    Positive means ``a`` is taller.
    """
    return a["z_max"] - b["z_max"]


def calculate_volume_difference(a: Mapping[str, Any], b: Mapping[str, Any]) -> float:
    """Signed difference in geometric volume, ``volume(a) - volume(b)`` (m³).

    Computed from geometry rather than the cached ``volume_m3`` field.
    """
    va = calculate_volume(record_to_polygon(a), a["z_min"], a["z_max"])
    vb = calculate_volume(record_to_polygon(b), b["z_min"], b["z_max"])
    return va - vb


# --------------------------------------------------------------------------
# Vertical (Z) handling
# --------------------------------------------------------------------------


class ZRangeValidation(NamedTuple):
    """Outcome of a Z-range check."""

    is_valid: bool
    reason: str | None


def validate_z_range(z_min: float, z_max: float) -> ZRangeValidation:
    """Check that a volume has a positive vertical extent.

    A zero or inverted range (``z_min >= z_max``) is invalid: it describes no
    volume and would make containment and overlap arithmetic meaningless.
    """
    if z_min < z_max:
        return ZRangeValidation(True, None)
    if z_min == z_max:
        return ZRangeValidation(False, "z_min equals z_max; volume has zero height")
    return ZRangeValidation(False, "z_min is greater than z_max")


def calculate_vertical_overlap(
    a_z_min: float, a_z_max: float, b_z_min: float, b_z_max: float
) -> float:
    """Shared vertical extent in metres, clamped at zero.

    Zero means the two ranges are disjoint or merely touching.
    """
    return max(0.0, min(a_z_max, b_z_max) - max(a_z_min, b_z_min))


# --------------------------------------------------------------------------
# Topology
# --------------------------------------------------------------------------


def calculate_horizontal_intersection(
    a: BaseGeometry, b: BaseGeometry
) -> BaseGeometry:
    """Planar intersection of two footprints.

    May be empty, a ``LineString`` (edge contact) or a ``GeometryCollection``;
    callers must check ``is_empty`` and the geometry type before assuming area.
    """
    return a.intersection(b)


def calculate_footprint_difference(
    inner: BaseGeometry, outer: BaseGeometry
) -> BaseGeometry:
    """Portion of ``inner`` lying outside ``outer``."""
    return inner.difference(outer)


def is_polygon_inside(inner: BaseGeometry, outer: BaseGeometry) -> bool:
    """True when ``inner`` is fully contained by ``outer``."""
    return calculate_area(calculate_footprint_difference(inner, outer)) == 0


def calculate_outside_area(inner: BaseGeometry, outer: BaseGeometry) -> float:
    """Area in m² of ``inner`` falling outside ``outer``."""
    return calculate_area(calculate_footprint_difference(inner, outer))


def union_footprints(polygons: Iterable[BaseGeometry]) -> BaseGeometry:
    """Dissolved plan footprint of several polygons (e.g. one floor's units)."""
    return unary_union(list(polygons))


def calculate_volume_intersection(
    a: Mapping[str, Any], b: Mapping[str, Any]
) -> tuple[BaseGeometry, float]:
    """Volumetric intersection of two records.

    Returns the plan overlap geometry and the overlap volume in cubic metres
    (plan overlap area x vertical overlap). Accepts records carrying
    ``geometry_3d``/``z_min``/``z_max``; infrastructure can be proxied by
    passing a dict with just those three keys.
    """
    overlap = calculate_horizontal_intersection(
        record_to_polygon(a), record_to_polygon(b)
    )
    z = calculate_vertical_overlap(a["z_min"], a["z_max"], b["z_min"], b["z_max"])
    return overlap, calculate_area(overlap) * z


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------


def calculate_geometry_hash(
    polygon: BaseGeometry, z_min: float, z_max: float
) -> str:
    """Stable 12-character uppercase hash of a footprint plus its Z extent.

    Coordinates are rounded to 3 dp before hashing so the value survives
    floating-point noise. Z bounds are coerced to ``float`` so that an integer
    ``0`` and a float ``0.0`` — which serialise differently in JSON — cannot
    produce different hashes for identical geometry.

    .. note::
       Historically this hash fed the prototype ULPIN, so changing it changed
       every identifier. Identity is now geometry-independent
       (:mod:`app.services.ulpin`), which is what makes it safe to normalise
       the payload here.
    """
    rounded = [
        (round(x, HASH_PRECISION), round(y, HASH_PRECISION))
        for x, y in polygon.exterior.coords
    ]
    payload = {
        "ring": rounded,
        "z": [
            round(float(z_min), HASH_PRECISION),
            round(float(z_max), HASH_PRECISION),
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode()
    ).hexdigest()[:12].upper()


def calculate_spatial_code(polygon: BaseGeometry) -> str:
    """Prototype local-metre grid index derived from the polygon centroid.

    .. warning::
       This encodes **local metres**, not a geographic grid reference. It is a
       prototype placeholder, not a spatial reference, and the integer
       truncation makes it sensitive to floating-point representation.
    """
    x, y = calculate_centroid(polygon)
    return f"{int((x + 1000) * 10):05d}{int((y + 1000) * 10):05d}"


# --------------------------------------------------------------------------
# Record construction
# --------------------------------------------------------------------------


def make_volume(
    *,
    ident: str,
    parcel: str,
    building: str | None,
    prop_type: str,
    floor: int | None,
    label: str,
    footprint: Polygon,
    zmin: float,
    zmax: float,
    status: str = "MAPPED",
) -> dict[str, Any]:
    """Build a property-volume record from a planar footprint.

    Computes ``area_m2``, ``volume_m3`` and ``geometry_hash`` through the engine
    so the derived fields can never drift from the geometry.

    There is deliberately **no** ``confidence`` argument. It used to be a required
    keyword, and every caller passed one of two literals -- the same fabricated
    figures the audit removed from the API. A required parameter invites a
    fabricated value; making it absent means a record either has a real
    ``geometric_quality`` or has no score at all. There is nothing to measure here,
    so nothing is reported.
    """
    timestamp = now()
    return {
        "id": ident,
        "prototype_ulpin": None,
        "parent_parcel_id": parcel,
        "building_id": building,
        "property_type": prop_type,
        "floor_number": floor,
        "unit_label": label,
        "z_min": zmin,
        "z_max": zmax,
        "geometry_3d": polygon_to_geojson(footprint),
        "volume_m3": round(calculate_volume(footprint, zmin, zmax), 2),
        "area_m2": round(calculate_area(footprint), 2),
        "geometry_hash": calculate_geometry_hash(footprint, zmin, zmax),
        "status": status,
        "created_at": timestamp,
        "updated_at": timestamp,
        "version": 1,
    }


# --------------------------------------------------------------------------
# Point-cloud support
# --------------------------------------------------------------------------
# Primitives the point-cloud extractor needs. They live here so that module
# stays free of Shapely, and so every hull, simplification and measure goes
# through one place.


def multi_point_from_xy(
    x: Sequence[float], y: Sequence[float]
) -> MultiPoint:
    """Build a point cloud from parallel coordinate arrays.

    Exists so point-cloud code can hand raw arrays to the engine without
    importing Shapely itself.
    """
    return MultiPoint([(float(a), float(b)) for a, b in zip(x, y)])


def concave_hull(points: BaseGeometry, *, ratio: float = 0.3) -> BaseGeometry:
    """Concave hull of a point set, tightening as ``ratio`` approaches 1.

    Used for building footprints: a convex hull fills in the notch of an
    L-shaped building, which would place a wall where there is none.
    """
    return shapely.concave_hull(points, ratio=ratio)


def largest_polygon(geometry: BaseGeometry) -> BaseGeometry:
    """Largest ``Polygon`` within ``geometry``, whatever collection type it is.

    A concave hull or a validity repair can return a ``MultiPolygon``; callers
    wanting one footprint must not silently receive a collection.
    """
    if geometry.geom_type == "Polygon":
        return geometry
    if geometry.geom_type in ("MultiPolygon", "GeometryCollection"):
        polygons = [g for g in geometry.geoms if g.geom_type == "Polygon"]
        if polygons:
            return max(polygons, key=lambda g: g.area)
    return geometry


def simplify_polygon(polygon: BaseGeometry, *, tolerance: float) -> BaseGeometry:
    """Douglas-Peucker simplification, repaired to a single valid polygon.

    Tolerance is tied to point spacing by the caller: too large and a real
    building loses its corners, too small and noise survives.
    """
    simplified = polygon.simplify(tolerance, preserve_topology=True)
    if simplified.is_empty or simplified.geom_type != "Polygon":
        simplified = polygon
    if not simplified.is_valid:
        simplified = shapely.make_valid(simplified)
    return largest_polygon(simplified)


def measure_area_m2(
    geometry: BaseGeometry,
    *,
    source_crs: Any = None,
    processing_crs: Any = None,
) -> float:
    """Area in square metres for a geometry in any CRS.

    Thin wrapper over :func:`~app.services.crs.calculate_metric_area` so callers
    outside the local plane never measure in degrees by accident.
    """
    return crs_service.calculate_metric_area(
        geometry, source_crs=source_crs, processing_crs=processing_crs
    )


def measure_volume_m3(
    geometry: BaseGeometry,
    z_min: float,
    z_max: float,
    *,
    source_crs: Any = None,
    processing_crs: Any = None,
) -> float:
    """Volume in cubic metres: plan area times the vertical extent.

    The volume of a prism over a footprint. No attempt is made to model anything
    but the prism -- a property volume in this system is a footprint plus a
    vertical band, and pretending otherwise would misstate the geometry.
    """
    if z_max <= z_min:
        return 0.0
    return measure_area_m2(
        geometry, source_crs=source_crs, processing_crs=processing_crs
    ) * (z_max - z_min)


def _divider_chord(
    polygon: BaseGeometry, line: Any, tolerance: float, reach: float
) -> Any:
    """Replace a boundary-reaching divider with its exact chord across ``polygon``.

    A planar subdivision only closes its faces when the dividing edges actually
    meet the boundary. A surface that has been round-tripped through another CRS
    is no longer exactly the polygon the plan was drawn against -- its corners
    are transformed independently, so its edges tilt by nanometres -- and a plan
    wall in exact projected coordinates then misses those edges. ``polygonize``
    does not complain: it returns the **whole polygon as one face**, so the
    division silently disappears and an occupied storey looks undivided.

    Snapping the endpoints is not enough, because a wall that misses a tilted edge
    still shares no node with it. Extending the wall along its own direction and
    clipping to the polygon produces a chord that touches the boundary *by
    construction*.

    Only dividers that already reach the boundary at both ends are extended. A
    stub that stops in open floor is left alone, so a half-height partition is
    never promoted into an invented division.
    """
    if line.is_empty:
        return line
    parts = list(line.geoms) if line.geom_type == "MultiLineString" else [line]

    out: list[Any] = []
    for part in parts:
        if part.geom_type not in ("LineString", "LinearRing") or len(part.coords) < 2:
            out.append(part)
            continue
        coords = list(part.coords)
        boundary = polygon.exterior
        reaches = all(
            boundary.distance(Point(c)) <= tolerance
            for c in (coords[0], coords[-1])
        )
        if not reaches:
            out.append(part)
            continue

        x0, y0 = coords[0][0], coords[0][1]
        x1, y1 = coords[-1][0], coords[-1][1]
        dx, dy = x1 - x0, y1 - y0
        length = math.hypot(dx, dy)
        if length <= 0:
            out.append(part)
            continue
        ux, uy = dx / length, dy / length
        extended = LineString(
            [
                (x0 - ux * reach, y0 - uy * reach),
                (x0 + ux * reach, y0 + uy * reach),
            ]
        )
        clipped = extended.intersection(polygon)
        if clipped.is_empty:
            out.append(part)
        elif clipped.geom_type == "MultiLineString":
            out.extend(clipped.geoms)
        else:
            out.append(clipped)

    kept = [part for part in out if not part.is_empty]
    if not kept:
        return line
    if len(kept) == 1:
        return kept[0]
    return MultiLineString(kept)


#: Coordinates are snapped to this grid before any subdivision or overlay.
#:
#: One millimetre is far below survey accuracy, and snapping to it is what makes
#: a planar subdivision reliable. A surface reprojected between CRSs has its
#: corners transformed independently, so its edges end up tilted by nanometres;
#: a dividing wall drawn in the target CRS then shares no exact node with that
#: edge, and ``polygonize`` returns the **whole polygon as one face** -- an
#: occupied storey silently reported as undivided, with no error anywhere.
#: Snapping both operands to a common grid restores the shared nodes.
SUBDIVISION_GRID = 0.001


def snap_to_grid(geometry: Any, grid_size: float = SUBDIVISION_GRID) -> Any:
    """Round a geometry's coordinates to ``grid_size``.

    Numerical hygiene, not simplification: the grid is far finer than any
    meaningful measurement, so no real detail is lost.
    """
    if geometry is None or geometry.is_empty:
        return geometry
    return set_precision(geometry, grid_size=grid_size)


def planar_subdivide(
    polygon: BaseGeometry,
    lines: Iterable[Any],
    *,
    snap_tolerance: float = 0.05,
) -> list[Polygon]:
    """Cut ``polygon`` into faces using ``lines`` as dividing edges.

    Uses a planar subdivision rather than repeated two-way splits, because
    repeated splitting is **order-dependent**: the same three walls can yield
    three regions or two depending on the order they are applied. This is
    order-independent by construction.

    Both operands are snapped to :data:`SUBDIVISION_GRID` first; see that
    constant for why that is necessary rather than cosmetic. A divider that
    already reaches the boundary is then extended to its exact chord, and one
    that stops short is left alone -- a half-height partition is never promoted
    into an invented division.

    Edges that do not actually divide the polygon -- a stub, a duplicate, a line
    that lies outside it, a line along its boundary -- are absorbed, so the result
    always tiles the input and its total area always equals the input's.
    """
    if polygon is None or polygon.is_empty:
        return []
    dividers = [line for line in lines if line is not None and not line.is_empty]
    if not dividers:
        return [largest_polygon(polygon)]

    try:
        plate = snap_to_grid(polygon)
        snapped = [snap_to_grid(line) for line in dividers]
        reach = plate.length * 2.0 + 1.0
        chords = [
            _divider_chord(plate, line, snap_tolerance, reach) for line in snapped
        ]
        merged = unary_union([plate.exterior, *chords])
    except (GEOSException, ValueError, AttributeError, IndexError):
        # Genuinely degenerate geometry falls back to undivided. A broad
        # ``except`` here once swallowed a programming error and made every
        # subdivision silently report "no division", so the type is deliberately
        # narrow: a bug should surface, not masquerade as an absent wall.
        return [largest_polygon(polygon)]

    faces = [
        face
        for face in polygonize(merged)
        if face.geom_type == "Polygon" and face.area > 0
    ]
    tolerance = max(1e-9, plate.area * 1e-9)
    inside = [
        face
        for face in faces
        if face.area > 0 and face.intersection(plate).area >= face.area - tolerance
    ]
    if not inside:
        return [largest_polygon(plate)]
    return sorted(inside, key=lambda face: (-face.area, face.bounds))


def clip_polygon(
    geometry: BaseGeometry, clipper: BaseGeometry, *, minimum_area: float = 0.0
) -> Polygon | None:
    """Clip a geometry to another, returning ``None`` if nothing survives.

    Used to hold a supplied unit outline inside the floor it belongs to.
    """
    if geometry is None or geometry.is_empty or clipper is None or clipper.is_empty:
        return None
    clipped = largest_polygon(geometry.intersection(clipper))
    if clipped.is_empty or clipped.area <= minimum_area:
        return None
    return clipped


def to_local_plane(geometry: BaseGeometry, source_crs: Any) -> BaseGeometry:
    """A geometry in ``source_crs`` -> the engine's local projected plane.

    The path every geometry hash takes, so a hash is independent of which
    projected CRS the geometry happened to be expressed in.
    """
    if crs_service.is_metric(source_crs):
        wgs84 = crs_service.transform_geometry(
            geometry, source_crs, GEOGRAPHIC_CRS
        )
    else:
        wgs84 = geometry
    return wgs84_to_local(wgs84)


def polygon_perimeter(
    geometry: BaseGeometry,
    *,
    source_crs: Any = None,
    processing_crs: Any = None,
) -> float:
    """Perimeter in metres, reprojecting first when the geometry is geographic.

    Mirrors :func:`~app.services.crs.calculate_metric_area`: a geographic
    geometry is projected into a metre CRS before measuring, so the result is
    metres and never degrees.
    """
    if geometry is None or geometry.is_empty:
        return 0.0
    if source_crs is None:
        return float(geometry.length)
    target = processing_crs or crs_service.select_processing_crs(
        geometry, source_crs=source_crs
    )
    return float(crs_service.transform_geometry(geometry, source_crs, target).length)


__all__ = [
    "DEMO_ANCHOR_LAT",
    "DEMO_ANCHOR_LON",
    "GEOGRAPHIC_CRS",
    "HASH_PRECISION",
    "PROCESSING_CRS",
    "WGS84_PRECISION",
    "PolygonValidation",
    "ZRangeValidation",
    "calculate_area",
    "calculate_centroid",
    "calculate_footprint_difference",
    "calculate_geometry_hash",
    "calculate_height_difference",
    "calculate_horizontal_intersection",
    "calculate_outside_area",
    "calculate_spatial_code",
    "calculate_vertical_overlap",
    "calculate_volume",
    "calculate_volume_difference",
    "calculate_volume_intersection",
    "clip_polygon",
    "concave_hull",
    "create_rectangle",
    "geojson_to_polygon",
    "is_polygon_inside",
    "largest_polygon",
    "ll_to_xy",
    "local_to_wgs84",
    "make_volume",
    "multi_point_from_xy",
    "polygon_perimeter",
    "polygon_to_geojson",
    "record_to_polygon",
    "repair_polygon",
    "simplify_polygon",
    "union_footprints",
    "validate_polygon",
    "validate_z_range",
    "wgs84_geojson_to_polygon",
    "wgs84_to_local",
    "xy_to_ll",
]
