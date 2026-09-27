"""cadastral_changes.difference_geometry: the region a change actually covers

A change record already keeps **both** outlines, so a reviewer can check the
comparison months later. It could not, however, be *drawn*: highlighting a change
means shading the plan area that belongs to exactly one of the two outlines, and
that polygon was not stored. Recomputing it on every read would put a different
answer on screen than the one that was recorded, which is the same class of drift
as re-running validation and losing a reviewer's approval.

Nullable, and left empty when a side has no footprint. A storey that does not
exist has no outline, and an empty polygon would read as "it shrank to nothing"
rather than "it is not there".

This column is evidence, not a queryable location, exactly like the two geometries
beside it, so it is JSONB rather than a geometry column.

Revision ID: 0010_cadastral_change_difference
Revises: 0009_provenance_links
Create Date: 2026-09-27
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0010_cadastral_change_difference"
down_revision = "0009_provenance_links"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cadastral_changes", sa.Column("difference_geometry", JSONB, nullable=True)
    )


def downgrade() -> None:
    op.drop_column("cadastral_changes", "difference_geometry")
