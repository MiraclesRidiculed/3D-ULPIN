"""PostGIS column types and CRS constants.

Two coordinate reference systems are in play, and conflating them is the classic
way to get cadastre areas wrong by a factor of ~10^10:

``GEOGRAPHIC_SRID`` (4326, WGS84 lon/lat)
    The **authoritative storage CRS**. Degrees. Every persisted footprint lives
    here, and its SRID is recorded on the column so no consumer has to guess.
    Topological predicates (``ST_Intersects``, ``ST_Contains``) are correct in
    this CRS because they are purely combinatorial.

``METRIC_SRID`` (32643, WGS84 / UTM zone 43N)
    A **projected, metre-based** CRS used for measurement and metric indexing.
    UTM 43N is correct for the demo scene near 77.2E, 28.6N.

Never measure on the geographic column
--------------------------------------
``ST_Area`` on a 4326 geometry returns **square degrees**, not square metres.
Measured on this demo scene it returns ``7.59e-06`` where the true footprint is
``82294.8 m²``. Each geometry column therefore carries a companion *generated*
metric column (``ST_Transform(geom, 32643)``, STORED and GIST-indexed) so metric
work is both correct and fast, and cannot drift from the source geometry.

In production ``METRIC_SRID`` must be selected per survey area rather than
hard-coded; it is a single well-known constant here only because the prototype
operates in one locality.
"""
from __future__ import annotations

from typing import Any

from geoalchemy2 import Geometry
from sqlalchemy import Computed

#: Storage CRS: WGS84 geographic (degrees).
GEOGRAPHIC_SRID = 4326

#: Measurement CRS: WGS84 / UTM zone 43N (metres). Demo scene only.
METRIC_SRID = 32643

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


def metric_geometry(source_column: str) -> tuple[Any, ...]:
    """Column arguments for a generated, GIST-indexed metric companion.

    Holds ``source_column`` projected to ``METRIC_SRID``. Persisted and derived
    by the database, so it can never disagree with the source geometry.
    """
    return (
        Geometry(
            geometry_type=POLYGON,
            srid=METRIC_SRID,
            spatial_index=True,
            nullable=True,
        ),
        Computed(f"ST_Transform({source_column}, {METRIC_SRID})", persisted=True),
    )
