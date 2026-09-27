"""Select generated metric companions from each geometry's location.

The original schema generated every ``*_metric`` column in UTM 43N. That is
valid only for the Delhi demo anchor; a source elsewhere still had a metre
unit, but with needless and potentially material projection distortion. All
authoritative geometry remains WGS84 (SRID 4326). This revision only replaces
the derived companions with a geometry whose SRID is the UTM zone (or polar
stereographic CRS) selected from the stored geometry's WGS84 centroid.

Revision ID: 0011_dynamic_metric_crs
Revises: 0010_cadastral_change_difference
Create Date: 2026-09-27
"""
from __future__ import annotations

from alembic import op

revision = "0011_dynamic_metric_crs"
down_revision = "0010_cadastral_change_difference"
branch_labels = None
depends_on = None


METRIC_COLUMNS = (
    ("parcels", "geometry_metric", "geometry", "idx_parcels_geometry_metric"),
    ("buildings", "footprint_metric", "footprint", "idx_buildings_footprint_metric"),
    ("floors", "geometry_3d_metric", "geometry_3d", "idx_floors_geometry_3d_metric"),
    (
        "property_volumes",
        "geometry_3d_metric",
        "geometry_3d",
        "idx_property_volumes_geometry_3d_metric",
    ),
    (
        "infrastructure",
        "geometry_3d_metric",
        "geometry_3d",
        "idx_infrastructure_geometry_3d_metric",
    ),
    (
        "validation_issues",
        "geometry_metric",
        "geometry",
        "idx_validation_issues_geometry_metric",
    ),
    (
        "geometry_versions",
        "geometry_metric",
        "geometry",
        "idx_geometry_versions_geometry_metric",
    ),
    (
        "extracted_buildings",
        "footprint_metric",
        "footprint",
        "idx_extracted_buildings_footprint_metric",
    ),
    (
        "extracted_floors",
        "footprint_metric",
        "footprint",
        "idx_extracted_floors_footprint_metric",
    ),
    (
        "generated_property_volumes",
        "geometry_3d_metric",
        "geometry_3d",
        "idx_generated_property_volumes_geometry_3d_metric",
    ),
)


#: SQL selecting a metre-based SRID for a WGS84 geometry, chosen from its own
#: centroid: the UTM zone, or the polar stereographic system where UTM has none.
#:
#: **Deliberately inlined rather than imported from ``app.db.types``.** A
#: migration that calls into current application code is not immutable: if that
#: helper ever changes, a database created afterwards silently gets a different
#: schema from one created before, while ``alembic_version`` reports the same
#: revision. That is the two-sources-of-truth problem this project already
#: suffered from, so this file holds its own copy. Keep the two in step by hand
#: and note the change here.
_METRIC_SRID_SQL = """
    CASE
        WHEN ST_Y(ST_Centroid({source})) >= 84.0 THEN 3413
        WHEN ST_Y(ST_Centroid({source})) <= -80.0 THEN 3031
        WHEN ST_Y(ST_Centroid({source})) >= 0
            THEN 32600 + LEAST(60, GREATEST(1,
                FLOOR((ST_X(ST_Centroid({source})) + 180.0) / 6.0)::integer + 1))
        ELSE 32700 + LEAST(60, GREATEST(1,
                FLOOR((ST_X(ST_Centroid({source})) + 180.0) / 6.0)::integer + 1))
    END
"""


def upgrade() -> None:
    for table, column, source, index in METRIC_COLUMNS:
        op.execute(f"DROP INDEX IF EXISTS {index}")
        op.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        op.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} geometry(Polygon) "
            f"GENERATED ALWAYS AS (ST_Transform({source}, "
            f"{_METRIC_SRID_SQL.format(source=source)})) STORED"
        )
        op.create_index(index, table, [column], postgresql_using="gist")


def downgrade() -> None:
    """Keep the location-aware metric companions when rolling back.

    Reintroducing a fixed zone would make persisted measurements less correct.
    Earlier application code can read the generic-SRID generated columns, so a
    schema no-op is safer than restoring the retired universal processing CRS.
    """
