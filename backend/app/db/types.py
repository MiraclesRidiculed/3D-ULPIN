"""PostGIS column types and CRS constants.

Two coordinate reference systems are in play, and conflating them is the classic
way to get cadastre areas wrong by a factor of ~10^10:

``GEOGRAPHIC_SRID`` (4326, WGS84 lon/lat)
    The **authoritative storage CRS**. Degrees. Every persisted footprint lives
    here, and its SRID is recorded on the column so no consumer has to guess.
    Topological predicates (``ST_Intersects``, ``ST_Contains``) are correct in
    this CRS because they are purely combinatorial.

``*_metric`` generated companions
    A **projected, metre-based** geometry selected from each WGS84 footprint's
    centroid. The companion retains that selected SRID, so a Delhi record uses
    UTM 43N while a London record uses UTM 30N. It is only for per-record metric
    measures; cross-record topology continues to use the authoritative WGS84
    geometry.

Never measure on the geographic column
--------------------------------------
``ST_Area`` on a 4326 geometry returns **square degrees**, not square metres.
Measured on this demo scene it returns ``7.59e-06`` where the true footprint is
``82294.8 m²``. Each geometry column therefore carries a companion *generated*
metric column selected from its own location, STORED and GIST-indexed, so metric
work is both correct and fast, and cannot drift from the source geometry.
"""
from __future__ import annotations

from typing import Any

from geoalchemy2 import Geometry
from sqlalchemy import Computed

#: Storage CRS: WGS84 geographic (degrees).
GEOGRAPHIC_SRID = 4326

#: Geometry types used. The record contract is a 2D footprint plus scalar
#: z_min/z_max, so 2D polygon types are correct here. ``init.sql`` originally
#: declared POLYGONZ / POLYHEDRALSURFACEZ, but the application has never built a
#: 3D geometry; storing a 2D footprint with explicit elevations is both faithful
#: to the API contract and simpler to index.
POLYGON = "POLYGON"

#: Companion metric column name for a given source column.
METRIC_SUFFIX = "_metric"


def geographic_geometry(*, nullable: bool = True) -> tuple[Any, ...]:
    """Column arguments for the authoritative store: WGS84, SRID 4326.

    Splat into ``mapped_column(...)``::

        geometry = mapped_column(*geographic_geometry(nullable=False))
    """
    return (
        Geometry(
            geometry_type=POLYGON,
            srid=GEOGRAPHIC_SRID,
            spatial_index=True,
            nullable=nullable,
        ),
    )


def metric_srid_sql(source_column: str) -> str:
    """SQL expression selecting a metre-based SRID for WGS84 geometry.

    UTM is selected by centroid and hemisphere. The polar regions use the
    corresponding polar stereographic system because UTM has no zones there.
    The authoritative source column is always SRID 4326, so these longitude and
    latitude operations have an explicit, stable meaning.
    """
    lon = f"ST_X(ST_Centroid({source_column}))"
    lat = f"ST_Y(ST_Centroid({source_column}))"
    zone = f"LEAST(60, GREATEST(1, FLOOR(({lon} + 180.0) / 6.0)::integer + 1))"
    return (
        f"CASE WHEN {lat} >= 84.0 THEN 3413 "
        f"WHEN {lat} <= -80.0 THEN 3031 "
        f"WHEN {lat} >= 0 THEN 32600 + {zone} "
        f"ELSE 32700 + {zone} END"
    )


def metric_geometry(source_column: str) -> tuple[Any, ...]:
    """Column arguments for a generated, GIST-indexed metric companion.

    Holds ``source_column`` projected to the metric CRS selected from its own
    WGS84 centroid. ``srid=-1`` is intentional: one column holds correctly
    tagged geometries from different processing zones. Persisted and derived by
    the database, so it can never disagree with the source geometry.
    """
    return (
        Geometry(
            geometry_type=POLYGON,
            srid=-1,
            spatial_index=True,
            nullable=True,
        ),
        Computed(
            f"ST_Transform({source_column}, {metric_srid_sql(source_column)})",
            persisted=True,
        ),
    )
