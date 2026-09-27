"""validation_issues: rule attribution and evidence

The rule-based engine files every finding with the rule that produced it and the
numbers that justified it. Three columns make that durable:

* ``rule_id`` -- which of the thirty rules fired, so a finding can be traced,
  reproduced with ``run_rule``, and explained with ``get_rule_definitions()``.
* ``category`` -- the rule's group (GEOMETRY, PARCEL, VERTICAL, CADASTRAL,
  INFRASTRUCTURE, CHANGE), for filtering without a join to the rule registry.
* ``evidence`` -- the JSONB numbers behind the finding (an overlap volume, an
  outside area, a Z band, a height delta). Without these a finding is an
  assertion; with them it is a measurement.

All three are nullable, so existing rows keep working. The original eight columns
are untouched: this migration only adds, and the demo's three deliberate findings
round-trip unchanged.

Revision ID: 0006_validation_rule_columns
Revises: 0005_generated_property_volumes
Create Date: 2026-09-26
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0006_validation_rule_columns"
down_revision = "0005_generated_property_volumes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "validation_issues", sa.Column("rule_id", sa.Text, nullable=True)
    )
    op.add_column(
        "validation_issues", sa.Column("category", sa.Text, nullable=True)
    )
    op.add_column(
        "validation_issues", sa.Column("evidence", JSONB, nullable=True)
    )
    # Findings are usually triaged per rule or per category, so both are indexed.
    op.create_index("ix_validation_issues_rule_id", "validation_issues", ["rule_id"])
    op.create_index("ix_validation_issues_category", "validation_issues", ["category"])


def downgrade() -> None:
    op.drop_index("ix_validation_issues_category", table_name="validation_issues")
    op.drop_index("ix_validation_issues_rule_id", table_name="validation_issues")
    op.drop_column("validation_issues", "evidence")
    op.drop_column("validation_issues", "category")
    op.drop_column("validation_issues", "rule_id")
