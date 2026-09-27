"""extracted_floors table

Storeys detected in point-cloud data by algorithmic/geometric segmentation.

Separate from the existing ``floors`` table on purpose. A cadastral ``floors``
row belongs to a legal building and feeds property volumes; this row is a storey
inferred from a scan, with no legal standing. Sharing the table would let an
inferred storey be read as surveyed record, and would require inventing a
``building_id`` foreign key for a building that does not exist in the cadastre.

Deliberate schema choices:

* **No ``confidence`` column.** The segmentation is geometric, so its defensible
  score is ``geometric_quality`` -- a regularity measure, not a probability.
  Uncertainty is carried as explicit review flags (``requires_human_review`` plus
  reasons in ``segmentation``) rather than as a number that invites over-trust.
* **No property or ownership anything.** This milestone produces storeys only.
  There is deliberately no link to ``property_volumes`` and no owner column.
* ``z_min`` / ``z_max`` are absolute elevations in the point cloud's own
  projected CRS, recorded in ``crs``; heights above the building's base are kept
  alongside because they are the engineering numbers a reviewer wants.

Footprint is a 2D ``POLYGON`` in SRID 4326 with a generated SRID 32643 companion,
matching every other geometry column. ``geometry_3d`` is JSONB, as for the
cadastral tables, so the API's volumetric wrapper is identical on both backends.

Revision ID: 0004_extracted_floors
Revises: 0003_extracted_buildings
Create Date: 2026-09-26
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004_extracted_floors"
down_revision = "0003_extracted_buildings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "extracted_floors",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("source_id", sa.Text, nullable=False),
        sa.Column("processing_job_id", sa.Text, nullable=False),
        sa.Column("building_id", sa.Text, nullable=False),
        sa.Column("floor_number", sa.Integer, nullable=False),
        sa.Column("z_min", sa.Float, nullable=False),
        sa.Column("z_max", sa.Float, nullable=False),
        sa.Column("z_min_above_ground", sa.Float, nullable=False),
        sa.Column("z_max_above_ground", sa.Float, nullable=False),
        sa.Column("footprint", sa.Text, nullable=True),
        sa.Column("geometry_3d", JSONB),
        sa.Column("area_m2", sa.Float, nullable=False),
        sa.Column("volume_m3", sa.Float, nullable=False),
        sa.Column("crs", sa.Text, nullable=False),
        sa.Column("method", sa.Text, nullable=False),
        sa.Column("geometry_hash", sa.Text, nullable=False),
        sa.Column("geometric_quality", sa.Float, nullable=False),
        sa.Column(
            "requires_human_review",
            sa.Boolean,
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("segmentation", JSONB),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.UniqueConstraint(
            "processing_job_id",
            "building_id",
            "floor_number",
            name="uq_extracted_floors_job_building_level",
        ),
    )
    op.execute(
        "ALTER TABLE extracted_floors "
        "ALTER COLUMN footprint TYPE geometry(Polygon, 4326) "
        "USING ST_GeomFromGeoJSON(footprint)"
    )
    op.execute(
        "ALTER TABLE extracted_floors "
        "ADD COLUMN footprint_metric geometry(Polygon, 32643) "
        "GENERATED ALWAYS AS (ST_Transform(footprint, 32643)) STORED"
    )
    # Named as GeoAlchemy2's ``spatial_index=True`` generates them, so
    # autogenerate reports no drift against the model.
    op.create_index("idx_extracted_floors_footprint", "extracted_floors", ["footprint"])
    op.create_index(
        "idx_extracted_floors_footprint_metric",
        "extracted_floors",
        ["footprint_metric"],
    )
    op.create_index("ix_extracted_floors_source", "extracted_floors", ["source_id"])
    op.create_index("ix_extracted_floors_building", "extracted_floors", ["building_id"])


def downgrade() -> None:
    op.drop_table("extracted_floors")
