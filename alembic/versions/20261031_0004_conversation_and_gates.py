"""Add multi-complaint conversation state and persisted gate runs.

Revision ID: 20261031_0004
Revises: 20261031_0003
Create Date: 2026-10-31
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261031_0004"
down_revision: str | None = "20261031_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("turn_number", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "conversations",
        sa.Column("memory_version", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "conversations",
        sa.Column("language", sa.String(length=20), server_default="unknown", nullable=False),
    )
    op.add_column(
        "conversations",
        sa.Column("memory", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
    )
    op.add_column(
        "messages",
        sa.Column("turn_number", sa.Integer(), server_default="1", nullable=False),
    )

    op.create_table(
        "message_interpretations",
        sa.Column("interpretation_id", sa.String(length=50), nullable=False),
        sa.Column("message_id", sa.String(length=150), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("model", sa.String(length=150), nullable=False),
        sa.Column("prompt_version", sa.String(length=100), nullable=False),
        sa.Column("schema_version", sa.String(length=50), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("provider_request_id", sa.String(length=150), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["message_id"], ["messages.message_id"]),
        sa.PrimaryKeyConstraint("interpretation_id"),
        sa.UniqueConstraint("message_id"),
    )

    op.create_table(
        "complaints",
        sa.Column("complaint_id", sa.String(length=50), nullable=False),
        sa.Column("source_message_id", sa.String(length=150), nullable=False),
        sa.Column("rider_id", sa.String(length=20), nullable=False),
        sa.Column("dispute_id", sa.String(length=50), nullable=True),
        sa.Column("complaint_index", sa.Integer(), nullable=False),
        sa.Column("complaint_type", sa.String(length=60), nullable=False),
        sa.Column("summary", sa.String(length=500), nullable=False),
        sa.Column("service_date_ist", sa.Date(), nullable=True),
        sa.Column("trip_ids", sa.JSON(), nullable=False),
        sa.Column("alleged_amount_rupees", sa.Integer(), nullable=True),
        sa.Column("grounding_status", sa.String(length=30), nullable=False),
        sa.Column("missing_fields", sa.JSON(), nullable=False),
        sa.Column("relation_kind", sa.String(length=30), nullable=False),
        sa.Column("target_dispute_id", sa.String(length=50), nullable=True),
        sa.Column("grouping_key", sa.String(length=300), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("gate_reasons", sa.JSON(), nullable=False),
        sa.Column("raw_payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["dispute_id"], ["disputes.dispute_id"]),
        sa.ForeignKeyConstraint(["rider_id"], ["riders.rider_id"]),
        sa.ForeignKeyConstraint(["source_message_id"], ["messages.message_id"]),
        sa.ForeignKeyConstraint(["target_dispute_id"], ["disputes.dispute_id"]),
        sa.PrimaryKeyConstraint("complaint_id"),
        sa.UniqueConstraint(
            "source_message_id", "complaint_index", name="uq_complaint_message_index"
        ),
    )
    op.create_index("ix_complaints_dispute", "complaints", ["dispute_id", "created_at"])
    op.create_index("ix_complaints_rider_status", "complaints", ["rider_id", "status"])

    op.create_table(
        "gate_runs",
        sa.Column("gate_run_id", sa.String(length=50), nullable=False),
        sa.Column("message_id", sa.String(length=150), nullable=True),
        sa.Column("dispute_id", sa.String(length=50), nullable=True),
        sa.Column("decision_id", sa.String(length=50), nullable=True),
        sa.Column("gate", sa.String(length=20), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("checker_version", sa.String(length=100), nullable=False),
        sa.Column("blocking_reasons", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["decision_id"], ["decisions.decision_id"]),
        sa.ForeignKeyConstraint(["dispute_id"], ["disputes.dispute_id"]),
        sa.ForeignKeyConstraint(["message_id"], ["messages.message_id"]),
        sa.PrimaryKeyConstraint("gate_run_id"),
    )
    op.create_index("ix_gate_runs_message_created", "gate_runs", ["message_id", "created_at"])
    op.create_index("ix_gate_runs_decision_gate", "gate_runs", ["decision_id", "gate"])

    with op.batch_alter_table("rule_check_results") as batch_op:
        batch_op.add_column(sa.Column("gate_run_id", sa.String(length=50), nullable=True))
        batch_op.alter_column("decision_id", existing_type=sa.String(length=50), nullable=True)
        batch_op.drop_constraint("uq_rule_check_decision_gate_rule", type_="unique")
        batch_op.create_foreign_key(
            "fk_rule_check_gate_run",
            "gate_runs",
            ["gate_run_id"],
            ["gate_run_id"],
        )
        batch_op.create_unique_constraint("uq_rule_check_gate_run_rule", ["gate_run_id", "rule_id"])
    op.create_index("ix_rule_checks_gate_run", "rule_check_results", ["gate_run_id"])


def downgrade() -> None:
    op.drop_index("ix_rule_checks_gate_run", table_name="rule_check_results")
    with op.batch_alter_table("rule_check_results") as batch_op:
        batch_op.drop_constraint("uq_rule_check_gate_run_rule", type_="unique")
        batch_op.drop_constraint("fk_rule_check_gate_run", type_="foreignkey")
        batch_op.alter_column("decision_id", existing_type=sa.String(length=50), nullable=False)
        batch_op.drop_column("gate_run_id")
        batch_op.create_unique_constraint(
            "uq_rule_check_decision_gate_rule", ["decision_id", "gate", "rule_id"]
        )

    op.drop_index("ix_gate_runs_decision_gate", table_name="gate_runs")
    op.drop_index("ix_gate_runs_message_created", table_name="gate_runs")
    op.drop_table("gate_runs")
    op.drop_index("ix_complaints_rider_status", table_name="complaints")
    op.drop_index("ix_complaints_dispute", table_name="complaints")
    op.drop_table("complaints")
    op.drop_table("message_interpretations")
    op.drop_column("messages", "turn_number")
    op.drop_column("conversations", "memory")
    op.drop_column("conversations", "language")
    op.drop_column("conversations", "memory_version")
    op.drop_column("conversations", "turn_number")
