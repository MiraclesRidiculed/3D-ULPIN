"""cadastral_changes: differences between the approved cadastre and a new survey

One row per detected difference, retaining **both** geometries. A reviewer
months later needs to see the two outlines that were compared; storing only the
arithmetic would make the record impossible to check.

The two geometries are JSONB rather than geometry columns deliberately. They are
evidence of a comparison, not a queryable location: nothing spatially queries a
change, because a change is looked up by the object it concerns and by the survey
that found it. Making them PostGIS columns would imply a spatial index that
nothing uses and a precision claim the survey does not make.

``status`` has no "illegal" or "unauthorised" value. A geometric difference is a
fact about two measurements; whether anyone was entitled to produce it is a
question for a human, and every row starts at ``REQUIRES_VERIFICATION``.

``geometric_quality`` is an agreement score in [0, 1] derived from the geometry
alone. It is **not** a confidence in the survey, and the column comment on the
model says so, because a number called a confidence on a survey result would be
an invented accuracy figure.

Revision ID: 0008_cadastral_changes
Revises: 0007_review_and_audit
Create Date: 2026-09-26
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0008_cadastral_changes"
down_revision = "0007_review_and_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cadastral_changes",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("change_type", sa.Text, nullable=False),
        sa.Column("object_id", sa.Text, nullable=False),
        sa.Column("object_type", sa.Text, nullable=False),
        # Evidence of a comparison, not a queryable location. See the module
        # docstring for why these are not geometry columns.
        sa.Column("previous_geometry", JSONB),
        sa.Column("new_geometry", JSONB),
        sa.Column("previous_z_min", sa.Float),
        sa.Column("previous_z_max", sa.Float),
        sa.Column("new_z_min", sa.Float),
        sa.Column("new_z_max", sa.Float),
        sa.Column("area_delta", sa.Float, nullable=False, server_default="0"),
        sa.Column("height_delta", sa.Float, nullable=False, server_default="0"),
        sa.Column("volume_delta", sa.Float, nullable=False, server_default="0"),
        sa.Column("previous_area_m2", sa.Float),
        sa.Column("new_area_m2", sa.Float),
        sa.Column("previous_height_m", sa.Float),
        sa.Column("new_height_m", sa.Float),
        sa.Column("previous_volume_m3", sa.Float),
        sa.Column("new_volume_m3", sa.Float),
        # Agreement in [0, 1] from geometry alone. Not a confidence.
        sa.Column(
            "geometric_quality", sa.Float, nullable=False, server_default="0"
        ),
        sa.Column("source_id", sa.Text, nullable=False),
        sa.Column(
            "detected_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("floor_number", sa.Integer),
        sa.Column("building_id", sa.Text),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("tolerance", sa.Float, nullable=False, server_default="0.01"),
        sa.Column("finding_id", sa.Text),
    )
    op.create_index("ix_cadastral_changes_object", "cadastral_changes", ["object_id"])
    op.create_index("ix_cadastral_changes_source", "cadastral_changes", ["source_id"])
    op.create_index("ix_cadastral_changes_type", "cadastral_changes", ["change_type"])


def downgrade() -> None:
    op.drop_index("ix_cadastral_changes_type", table_name="cadastral_changes")
    op.drop_index("ix_cadastral_changes_source", table_name="cadastral_changes")
    op.drop_index("ix_cadastral_changes_object", table_name="cadastral_changes")
    op.drop_table("cadastral_changes")
