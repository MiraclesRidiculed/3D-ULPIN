"""review_cases, review_decisions, audit_events: human verification and audit

Three tables, and the split between them is the design rather than an accident.

``review_cases``
    Mutable current state. One row per validation finding, enforced unique on
    ``issue_id`` so two people cannot decide the same finding. This is what a
    review queue reads.

``review_decisions``
    Immutable history. One row per decision, never updated. Carries reviewer,
    timestamp, decision, reason, the finding, the affected object and both
    states, because a row that only records ``APPROVED`` cannot be reconstructed
    into a justification months later.

``audit_events``
    The cross-cutting trail. One row per important cadastral operation, whatever
    subsystem produced it, so a single query can answer "what has happened to
    this parcel".

``review_cases.issue_id`` is deliberately **not** a foreign key to
``validation_issues``. ``validate()`` clears and rebuilds the issues collection
on every run; a foreign key would either block that or cascade the review history
away with it. The history of a human decision must outlive the automated run that
produced the finding it was about.

All three are append-only or explicitly mutable by the service layer:
``storage.UPDATABLE_FIELDS`` gives ``review_decisions`` and ``audit_events`` an
empty set, so a write attempt is dropped rather than applied.

Revision ID: 0007_review_and_audit
Revises: 0006_validation_rule_columns
Create Date: 2026-09-26
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0007_review_and_audit"
down_revision = "0006_validation_rule_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "review_cases",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("issue_id", sa.Text, nullable=False),
        sa.Column("issue_type", sa.Text),
        sa.Column("severity", sa.Text),
        sa.Column("object_a", sa.Text),
        sa.Column("object_b", sa.Text),
        sa.Column("object_type", sa.Text),
        sa.Column("state", sa.Text, nullable=False),
        sa.Column("assigned_to", sa.Text),
        sa.Column("priority", sa.Text),
        sa.Column("reason", sa.Text),
        sa.Column("created_by", sa.Text),
        # The shared ``created_at``/``updated_at`` helpers produce nullable
        # columns with a server default, and 0001 established that for every
        # existing table. Declaring these NOT NULL here would make the models
        # and the migrations disagree, which the autogenerate drift check
        # catches -- so the precedent wins.
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("decided_by", sa.Text),
        sa.Column("decision", sa.Text),
        sa.Column("decision_reason", sa.Text),
        sa.Column(
            "review_count", sa.Integer, nullable=False, server_default="0"
        ),
        # One case per finding: two open cases would let two reviewers decide the
        # same issue and the audit trail would record a contradiction.
        sa.UniqueConstraint("issue_id", name="uq_review_cases_issue"),
    )
    op.create_index("ix_review_cases_state", "review_cases", ["state"])
    op.create_index("ix_review_cases_assignee", "review_cases", ["assigned_to"])

    op.create_table(
        "review_decisions",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("review_case_id", sa.Text, nullable=False),
        sa.Column("issue_id", sa.Text),
        sa.Column("decision", sa.Text, nullable=False),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("reviewer", sa.Text),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("object_id", sa.Text),
        sa.Column("related_object_id", sa.Text),
        sa.Column("object_type", sa.Text),
        sa.Column("previous_state", sa.Text),
        sa.Column("new_state", sa.Text, nullable=False),
        sa.Column(
            "decided_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
    )
    op.create_index(
        "ix_review_decisions_case", "review_decisions", ["review_case_id"]
    )
    op.create_index(
        "ix_review_decisions_issue", "review_decisions", ["issue_id"]
    )
    op.create_index(
        "ix_review_decisions_reviewer", "review_decisions", ["reviewer"]
    )

    op.create_table(
        "audit_events",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("object_id", sa.Text),
        sa.Column("object_type", sa.Text),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("detail", sa.Text),
        sa.Column("issue_id", sa.Text),
        sa.Column("review_case_id", sa.Text),
        sa.Column("previous_state", sa.Text),
        sa.Column("new_state", sa.Text),
        # Nullable, matching the shared ``created_at`` helper and 0001.
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column("context", JSONB),
    )
    op.create_index("ix_audit_events_object", "audit_events", ["object_id"])
    op.create_index("ix_audit_events_action", "audit_events", ["action"])
    op.create_index("ix_audit_events_actor", "audit_events", ["actor"])
    op.create_index("ix_audit_events_issue", "audit_events", ["issue_id"])


def downgrade() -> None:
    op.drop_index("ix_audit_events_issue", table_name="audit_events")
    op.drop_index("ix_audit_events_actor", table_name="audit_events")
    op.drop_index("ix_audit_events_action", table_name="audit_events")
    op.drop_index("ix_audit_events_object", table_name="audit_events")
    op.drop_table("audit_events")

    op.drop_index("ix_review_decisions_reviewer", table_name="review_decisions")
    op.drop_index("ix_review_decisions_issue", table_name="review_decisions")
    op.drop_index("ix_review_decisions_case", table_name="review_decisions")
    op.drop_table("review_decisions")

    op.drop_index("ix_review_cases_assignee", table_name="review_cases")
    op.drop_index("ix_review_cases_state", table_name="review_cases")
    op.drop_table("review_cases")
