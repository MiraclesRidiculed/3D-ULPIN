"""provenance_links: how every derived object came to exist

One row per step of the chain from an ingested dataset to a reviewed finding::

    DATA_SOURCE -> PROCESSING_JOB -> BUILDING -> FLOOR
                -> PROPERTY_VOLUME -> ULPIN -> VALIDATION -> REVIEW

A graph of parent-to-child links, not columns on each object, for two reasons: a
generated volume has one source but several things it also contributes to (a
finding about it, a review of that finding), which a single ``source_id`` column
cannot express; and the later stages are events rather than geometry, with no row
to hang a foreign key off.

``source_id`` is denormalised onto every link so "everything from this upload" is
one indexed lookup rather than a walk of the whole graph.

``model_name``/``model_version`` are nullable and **empty for every link this
system writes**. Every stage is algorithmic or geometric -- a progressive grid
ground filter, DBSCAN, a concavity hull, an elevation-histogram peak search, a
planar subdivision -- and there is no trained model anywhere in the pipeline. The
columns exist so that the day a model *is* used, the information lands somewhere
queryable rather than being dropped. They are never populated with a placeholder,
and the service layer rejects a model name attached to a known algorithmic method,
because a model name on a grid filter reads later as "a model produced this",
which is false.

Revision ID: 0009_provenance_links
Revises: 0008_cadastral_changes
Create Date: 2026-09-27
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0009_provenance_links"
down_revision = "0008_cadastral_changes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provenance_links",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("stage", sa.Text, nullable=False),
        sa.Column("object_id", sa.Text, nullable=False),
        sa.Column("source_id", sa.Text),
        sa.Column("processing_job_id", sa.Text),
        sa.Column("parent_id", sa.Text),
        sa.Column("parent_stage", sa.Text),
        sa.Column("algorithm", sa.Text, nullable=False),
        sa.Column("method_description", sa.Text),
        # Empty for algorithmic methods. Never a placeholder.
        sa.Column("model_name", sa.Text),
        sa.Column("model_version", sa.Text),
        sa.Column("parameters", JSONB),
        sa.Column("created_by", sa.Text),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
    )
    # Lineage walks are by object and by parent, and "everything from this
    # upload" is a single-source query, so all four are indexed.
    op.create_index("ix_provenance_links_object", "provenance_links", ["object_id"])
    op.create_index("ix_provenance_links_parent", "provenance_links", ["parent_id"])
    op.create_index("ix_provenance_links_source", "provenance_links", ["source_id"])
    op.create_index("ix_provenance_links_stage", "provenance_links", ["stage"])


def downgrade() -> None:
    op.drop_index("ix_provenance_links_stage", table_name="provenance_links")
    op.drop_index("ix_provenance_links_source", table_name="provenance_links")
    op.drop_index("ix_provenance_links_parent", table_name="provenance_links")
    op.drop_index("ix_provenance_links_object", table_name="provenance_links")
    op.drop_table("provenance_links")
