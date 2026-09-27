"""processing_jobs table

Records the unit of processing work behind each ingested point cloud, so
ingestion is auditable and the UI can show status.

Only ``METADATA_EXTRACTION`` runs today. The building-extraction and
floor-segmentation stages are not implemented, so they are recorded as
``NOT_IMPLEMENTED`` rather than silently omitted — the absence of features is a
fact the API should state, not hide.

Geometry is deliberately *not* stored as PostGIS geometry here: a point cloud is
millions of discrete points, not a polygon. Its extent is kept as JSONB bounds so
it can be filtered and displayed without pulling points into a geometry column.

Revision ID: 0002_processing_jobs
Revises: 0001_initial
Create Date: 2026-09-26
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002_processing_jobs"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "processing_jobs",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("source_id", sa.Text, nullable=False),
        sa.Column("job_type", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("point_count", sa.BigInteger),
        sa.Column("crs", sa.Text),
        sa.Column("bounds", JSONB),
        sa.Column("detail", sa.Text),
        sa.Column("error", sa.Text),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("created_by", sa.Text),
        sa.Column("metadata", JSONB),
    )
    op.create_index("ix_processing_jobs_source", "processing_jobs", ["source_id"])
    op.create_index(
        "ix_processing_jobs_type_status",
        "processing_jobs",
        ["job_type", "status"],
    )


def downgrade() -> None:
    op.drop_table("processing_jobs")
