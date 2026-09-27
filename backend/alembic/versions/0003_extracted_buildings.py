"""extracted_buildings table

Buildings derived from point-cloud scans by algorithmic/geometric extraction.

Separate from the existing ``buildings`` table on purpose. A cadastral
``buildings`` row is a legal object tied to a parcel; this row is a *measurement*
inferred from a scan, with no parcel and no legal standing. Sharing the table
would let an inferred footprint be read as surveyed record.

Two deliberate schema choices:

* **No ``confidence`` column.** The extractor is geometric, so its defensible
  score is ``geometric_quality`` -- a regularity measure, not a probability of
  correctness. A ``confidence`` field here would invite exactly the misreading
  the extractor refuses to make, and the demo's pre-existing simulated adapters
  already demonstrate why invented confidence numbers are misleading.
* **Footprint in SRID 4326 with a generated SRID 32643 companion**, matching every
  other geometry column. The scan's own projected CRS is preserved separately in
  the ``crs`` column, because a footprint's coordinates are meaningless without
  it.

Geometry is a 2D ``POLYGON`` plus the scalar ``height_m``, consistent with the
record contract used everywhere else in this schema.

Revision ID: 0003_extracted_buildings
Revises: 0002_processing_jobs
Create Date: 2026-09-26
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003_extracted_buildings"
down_revision = "0002_processing_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "extracted_buildings",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("source_id", sa.Text, nullable=False),
        sa.Column("processing_job_id", sa.Text, nullable=False),
        sa.Column("footprint", sa.Text, nullable=True),
        sa.Column("height_m", sa.Float, nullable=False),
        sa.Column("crs", sa.Text, nullable=False),
        sa.Column("method", sa.Text, nullable=False),
        sa.Column("extractor", sa.Text, nullable=False),
        sa.Column("extractor_version", sa.Text, nullable=False),
        sa.Column("geometry_hash", sa.Text, nullable=False),
        sa.Column("geometric_quality", sa.Float, nullable=False),
        sa.Column("extraction", JSONB),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
    )
    op.execute(
        "ALTER TABLE extracted_buildings "
        "ALTER COLUMN footprint TYPE geometry(Polygon, 4326) "
        "USING ST_GeomFromGeoJSON(footprint)"
    )
    op.execute(
        "ALTER TABLE extracted_buildings "
        "ADD COLUMN footprint_metric geometry(Polygon, 32643) "
        "GENERATED ALWAYS AS (ST_Transform(footprint, 32643)) STORED"
    )
    # Named as GeoAlchemy2's ``spatial_index=True`` generates them, so
    # autogenerate reports no drift against the model.
    op.create_index(
        "idx_extracted_buildings_footprint", "extracted_buildings", ["footprint"]
    )
    op.create_index(
        "idx_extracted_buildings_footprint_metric",
        "extracted_buildings",
        ["footprint_metric"],
    )
    op.create_index(
        "ix_extracted_buildings_source", "extracted_buildings", ["source_id"]
    )
    op.create_index(
        "ix_extracted_buildings_job", "extracted_buildings", ["processing_job_id"]
    )


def downgrade() -> None:
    op.drop_table("extracted_buildings")
