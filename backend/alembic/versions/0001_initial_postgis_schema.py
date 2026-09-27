"""initial PostGIS cadastral schema

Reconciliation with the original ``database/init.sql`` (inspected before this
migration was written):

* TEXT primary keys holding the application's own ids, rather than UUID plus a
  separate TEXT business key, so the API contract is preserved exactly.
* Plural table names, matching the repository's collection vocabulary.
* 2D ``geometry(Polygon,4326)`` plus scalar ``z_min``/``z_max`` columns instead of
  ``POLYGONZ`` / ``POLYHEDRALSURFACEZ``: the record contract is a 2D footprint
  with explicit elevations, and the application never builds a 3D geometry.
* Added ``unit_label`` (property volumes), ``owner`` + ``reference``
  (infrastructure), ``gap_m`` + ``location`` (validation issues) â€” all present
  in the API models but missing from the original DDL.
* Added ``geometry_version`` to every geometry-bearing table, plus the
  ``geometry_versions`` history table.
* Each geometry column gains a generated, GIST-indexed companion in SRID 32643
  (WGS84 / UTM 43N) so metric area can be computed in square metres. Measuring
  on the 4326 column would return square degrees.

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-26
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geometry
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

#: Storage CRS: WGS84 degrees.
GEOGRAPHIC_SRID = 4326
#: Measurement CRS: WGS84 / UTM zone 43N, metres.
METRIC_SRID = 32643
POLYGON = "POLYGON"


def _geo(name: str, *, nullable: bool = True) -> list:
    """Column definition list for the authoritative WGS84 geometry column.

    Returned as a single-element *list* so it can be splatted into
    ``op.create_table``: passing a bare ``Column`` would be read as one more
    positional column rather than a definition.

    ``spatial_index=False`` because GeoAlchemy2 otherwise creates a GIST index
    implicitly during ``create_table``. Indexes are declared explicitly below, so
    the migration stays the single readable source of the index set and the
    names match what the models generate.
    """
    return [
        sa.Column(
            name,
            Geometry(
                geometry_type=POLYGON,
                srid=GEOGRAPHIC_SRID,
                nullable=nullable,
                spatial_index=False,
            ),
        )
    ]


def _metric(name: str, source: str) -> list:
    """Column definition list for a generated metric companion."""
    return [
        sa.Column(
            name,
            Geometry(
                geometry_type=POLYGON, srid=METRIC_SRID, spatial_index=False
            ),
            sa.Computed(f"ST_Transform({source}, {METRIC_SRID})", persisted=True),
        )
    ]


def upgrade() -> None:
    op.create_table(
        "parcels",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("parcel_id", sa.Text, nullable=False, unique=True),
        sa.Column("prototype_ulpin", sa.Text, unique=True),
        *_geo("geometry", nullable=False),
        *_metric("geometry_metric", "geometry"),
        sa.Column("area", sa.Float),
        sa.Column("land_use", sa.Text),
        sa.Column("survey_reference", sa.Text),
        sa.Column("geometry_hash", sa.Text),
        sa.Column("geometry_version", sa.Integer, server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
        sa.Column("version", sa.Integer, server_default="1", nullable=False),
    )
    op.create_index("idx_parcels_geometry","parcels", ["geometry"], postgresql_using="gist")
    op.create_index(
        "idx_parcels_geometry_metric",
        "parcels", ["geometry_metric"], postgresql_using="gist"
    )

    op.create_table(
        "buildings",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("building_id", sa.Text, nullable=False, unique=True),
        sa.Column("parcel_id", sa.Text, sa.ForeignKey("parcels.parcel_id")),
        *_geo("footprint"),
        *_metric("footprint_metric", "footprint"),
        # The API exposes a geometry_3d wrapper on buildings; persisted as JSONB so
        # the response shape is identical on both backends.
        sa.Column("geometry_3d", JSONB),
        sa.Column("height", sa.Float),
        sa.Column("floor_count", sa.Integer),
        sa.Column("confidence", sa.Float),
        sa.Column("geometry_hash", sa.Text),
        sa.Column("geometry_version", sa.Integer, server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
        sa.Column("version", sa.Integer, server_default="1", nullable=False),
    )
    op.create_index(
        "idx_buildings_footprint",
        "buildings", ["footprint"], postgresql_using="gist"
    )
    op.create_index(
        "idx_buildings_footprint_metric",
        "buildings", ["footprint_metric"],
        postgresql_using="gist",
    )

    op.create_table(
        "floors",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column(
            "building_id",
            sa.Text,
            sa.ForeignKey("buildings.building_id"),
            nullable=False,
        ),
        sa.Column("floor_number", sa.Integer, nullable=False),
        sa.Column("z_min", sa.Float, nullable=False),
        sa.Column("z_max", sa.Float, nullable=False),
        *_geo("geometry_3d"),
        *_metric("geometry_3d_metric", "geometry_3d"),
        sa.Column("confidence", sa.Float),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
        sa.Column("version", sa.Integer, server_default="1", nullable=False),
        sa.UniqueConstraint("building_id", "floor_number", name="uq_floors_building_level"),
    )
    op.create_index(
        "idx_floors_geometry_3d", "floors", ["geometry_3d"], postgresql_using="gist"
    )
    op.create_index(
        "idx_floors_geometry_3d_metric",
        "floors",
        ["geometry_3d_metric"],
        postgresql_using="gist",
    )

    op.create_table(
        "property_volumes",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("prototype_ulpin", sa.Text, unique=True),
        sa.Column("parent_parcel_id", sa.Text, sa.ForeignKey("parcels.parcel_id")),
        sa.Column("building_id", sa.Text),
        sa.Column("property_type", sa.Text),
        sa.Column("floor_number", sa.Integer),
        sa.Column("unit_label", sa.Text),
        sa.Column("z_min", sa.Float),
        sa.Column("z_max", sa.Float),
        *_geo("geometry_3d"),
        *_metric("geometry_3d_metric", "geometry_3d"),
        sa.Column("volume_m3", sa.Float),
        sa.Column("area_m2", sa.Float),
        sa.Column("geometry_hash", sa.Text, nullable=False),
        sa.Column("status", sa.Text),
        sa.Column("confidence", sa.Float),
        sa.Column("geometry_version", sa.Integer, server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
        sa.Column("version", sa.Integer, server_default="1", nullable=False),
    )
    op.create_index(
        "idx_property_volumes_geometry_3d",
        "property_volumes",
        ["geometry_3d"],
        postgresql_using="gist",
    )
    op.create_index(
        "idx_property_volumes_geometry_3d_metric",
        "property_volumes",
        ["geometry_3d_metric"],
        postgresql_using="gist",
    )

    op.create_table(
        "infrastructure",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("type", sa.Text),
        *_geo("geometry_3d"),
        *_metric("geometry_3d_metric", "geometry_3d"),
        sa.Column("z_min", sa.Float),
        sa.Column("z_max", sa.Float),
        sa.Column("owner", sa.Text),
        sa.Column("reference", sa.Text),
        sa.Column("geometry_hash", sa.Text),
        sa.Column("geometry_version", sa.Integer, server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
        sa.Column("version", sa.Integer, server_default="1", nullable=False),
    )
    op.create_index(
        "idx_infrastructure_geometry_3d",
        "infrastructure",
        ["geometry_3d"],
        postgresql_using="gist",
    )
    op.create_index(
        "idx_infrastructure_geometry_3d_metric",
        "infrastructure",
        ["geometry_3d_metric"],
        postgresql_using="gist",
    )

    op.create_table(
        "validation_issues",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("object_a", sa.Text, nullable=False),
        sa.Column("object_b", sa.Text),
        sa.Column("issue_type", sa.Text, nullable=False),
        sa.Column("severity", sa.Text, nullable=False),
        sa.Column("overlap_volume", sa.Float, server_default="0", nullable=False),
        sa.Column("gap_m", sa.Float),
        sa.Column("description", sa.Text, nullable=False),
        *_geo("geometry"),
        *_metric("geometry_metric", "geometry"),
        sa.Column("status", sa.Text, server_default="OPEN", nullable=False),
        sa.Column("location", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "idx_validation_issues_geometry",
        "validation_issues", ["geometry"],
        postgresql_using="gist",
    )
    op.create_index(
        "idx_validation_issues_geometry_metric",
        "validation_issues", ["geometry_metric"],
        postgresql_using="gist",
    )

    op.create_table(
        "data_sources",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("source_type", sa.Text, nullable=False),
        sa.Column("filename", sa.Text, nullable=False),
        sa.Column("crs", sa.Text),
        sa.Column("acquisition_date", sa.Date),
        sa.Column("metadata", JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "geometry_versions",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("object_id", sa.Text, nullable=False),
        sa.Column("object_type", sa.Text, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        *_geo("geometry", nullable=False),
        *_metric("geometry_metric", "geometry"),
        sa.Column("z_min", sa.Float, nullable=False),
        sa.Column("z_max", sa.Float, nullable=False),
        sa.Column("geometry_hash", sa.Text, nullable=False),
        sa.Column("source_id", sa.Text),
        sa.Column("processing_job_id", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("created_by", sa.Text),
        sa.Column("change_reason", sa.Text),
        sa.UniqueConstraint("object_id", "version", name="uq_geometry_versions_object"),
    )
    op.create_index(
        "ix_geometry_versions_object",
        "geometry_versions", ["object_id"]
    )
    op.create_index(
        "idx_geometry_versions_geometry",
        "geometry_versions", ["geometry"], postgresql_using="gist"
    )
    op.create_index(
        "idx_geometry_versions_geometry_metric",
        "geometry_versions", ["geometry_metric"], postgresql_using="gist"
    )


def downgrade() -> None:
    op.drop_table("geometry_versions")
    op.drop_table("data_sources")
    op.drop_table("validation_issues")
    op.drop_table("infrastructure")
    op.drop_table("property_volumes")
    op.drop_table("floors")
    op.drop_table("buildings")
    op.drop_table("parcels")
