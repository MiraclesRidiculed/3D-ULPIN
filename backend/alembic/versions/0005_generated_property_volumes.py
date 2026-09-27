"""generated_property_volumes table

Property volumes generated from a parcel, an extracted building and its storeys.

Separate from the cadastral ``property_volumes`` table on purpose. This row is a
*derived* record assembled from a point cloud and a parent parcel; that table
holds surveyed rights, and the demo scene's 17 properties are a deliberate
showcase that must not be disturbed. Mixing them would let a generated volume be
read as a cadastral one.

The honesty columns are the substance of this migration:

* ``volume_scope`` is ``UNIT`` only where floor-plan data actually divided a
  storey, and ``FLOOR`` otherwise. It is never ``UNIT`` by default -- inventing
  apartment boundaries is precisely what this milestone refuses to do.
* ``units_inferred`` exists and is always false. It is stored so a consumer can
  *verify* that no boundary was imagined, rather than trusting the code.
* ``unit_label_source`` distinguishes a label that came from a floor plan from
  one synthesised as a volume key, because the two are not the same claim.

Deliberately absent:

* **No ``confidence`` column.** These are derived geometric records. Their
  defensible signals are ``area_m2``/``volume_m3``, the input geometry hashes in
  ``source_provenance``, and the explicit ``review_reasons``.
* **No owner, tenant or title column.** This milestone generates geometry, not
  rights. ``parent_parcel_id`` is an association with a parcel, not ownership.

``geometry_3d`` is SRID 4326 with a generated SRID 32643 companion, matching
every other geometry column, and ``prototype_ulpin`` is unique so two volumes can
never share an identifier.

Revision ID: 0005_generated_property_volumes
Revises: 0004_extracted_floors
Create Date: 2026-09-26
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005_generated_property_volumes"
down_revision = "0004_extracted_floors"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "generated_property_volumes",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("source_id", sa.Text),
        sa.Column("prototype_ulpin", sa.Text, nullable=False, unique=True),
        sa.Column("parent_parcel_id", sa.Text, nullable=False),
        sa.Column("building_id", sa.Text, nullable=False),
        sa.Column("floor_number", sa.Integer, nullable=False),
        sa.Column("floor_label", sa.Text, nullable=False),
        sa.Column("unit_label", sa.Text),
        sa.Column("unit_label_source", sa.Text, nullable=False),
        sa.Column("volume_scope", sa.Text, nullable=False),
        sa.Column("property_type", sa.Text, nullable=False),
        sa.Column("vertical_position", sa.Text, nullable=False),
        sa.Column("z_min", sa.Float, nullable=False),
        sa.Column("z_max", sa.Float, nullable=False),
        sa.Column("geometry_3d", sa.Text, nullable=True),
        sa.Column("area_m2", sa.Float, nullable=False),
        sa.Column("volume_m3", sa.Float, nullable=False),
        sa.Column("geometry_hash", sa.Text, nullable=False),
        sa.Column(
            "geometry_version",
            sa.Integer,
            nullable=False,
            server_default="1",
            default=1,
        ),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column(
            "units_inferred",
            sa.Boolean,
            nullable=False,
            server_default=sa.false(),
            default=False,
        ),
        sa.Column(
            "requires_human_review",
            sa.Boolean,
            nullable=False,
            server_default=sa.false(),
            default=False,
        ),
        sa.Column("source_provenance", JSONB),
        sa.Column("processing_provenance", JSONB),
        sa.Column("review_reasons", JSONB),
        sa.Column("notes", JSONB),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
    )
    op.execute(
        "ALTER TABLE generated_property_volumes "
        "ALTER COLUMN geometry_3d TYPE geometry(Polygon, 4326) "
        "USING ST_GeomFromGeoJSON(geometry_3d)"
    )
    op.execute(
        "ALTER TABLE generated_property_volumes "
        "ADD COLUMN geometry_3d_metric geometry(Polygon, 32643) "
        "GENERATED ALWAYS AS (ST_Transform(geometry_3d, 32643)) STORED"
    )
    # Named as GeoAlchemy2's ``spatial_index=True`` generates them, so
    # autogenerate reports no drift against the model.
    op.create_index(
        "idx_generated_property_volumes_geometry_3d",
        "generated_property_volumes",
        ["geometry_3d"],
    )
    op.create_index(
        "idx_generated_property_volumes_geometry_3d_metric",
        "generated_property_volumes",
        ["geometry_3d_metric"],
    )
    op.create_index(
        "ix_generated_volumes_source", "generated_property_volumes", ["source_id"]
    )
    op.create_index(
        "ix_generated_volumes_building", "generated_property_volumes", ["building_id"]
    )
    op.create_index(
        "ix_generated_volumes_parcel", "generated_property_volumes", ["parent_parcel_id"]
    )


def downgrade() -> None:
    op.drop_table("generated_property_volumes")
