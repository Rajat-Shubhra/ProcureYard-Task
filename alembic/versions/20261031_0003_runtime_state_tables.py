"""Create durable conversation, audit, ops, and payout state tables.

Revision ID: 20261031_0003
Revises: 20261031_0002
Create Date: 2026-10-31
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261031_0003"
down_revision: str | None = "20261031_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def timestamp(name: str, *, nullable: bool = False) -> sa.Column:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        server_default=sa.text("CURRENT_TIMESTAMP") if not nullable else None,
        nullable=nullable,
    )


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("conversation_id", sa.String(length=50), nullable=False),
        sa.Column("rider_id", sa.String(length=20), nullable=False),
        sa.Column("state", sa.String(length=40), nullable=False),
        timestamp("created_at"),
        timestamp("updated_at"),
        sa.ForeignKeyConstraint(["rider_id"], ["riders.rider_id"]),
        sa.PrimaryKeyConstraint("conversation_id"),
    )
    op.create_index("ix_conversations_state", "conversations", ["state"])

    op.create_table(
        "messages",
        sa.Column("message_id", sa.String(length=150), nullable=False),
        sa.Column("conversation_id", sa.String(length=50), nullable=False),
        sa.Column("rider_id", sa.String(length=20), nullable=False),
        sa.Column("text", sa.String(length=4000), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("reply", sa.String(length=4000), nullable=True),
        sa.Column("response_payload", sa.JSON(), nullable=True),
        timestamp("created_at"),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.conversation_id"]),
        sa.ForeignKeyConstraint(["rider_id"], ["riders.rider_id"]),
        sa.PrimaryKeyConstraint("message_id"),
    )
    op.create_index("ix_messages_status", "messages", ["status"])
    op.create_index("ix_messages_rider_received", "messages", ["rider_id", "received_at"])

    op.create_table(
        "disputes",
        sa.Column("dispute_id", sa.String(length=50), nullable=False),
        sa.Column("conversation_id", sa.String(length=50), nullable=False),
        sa.Column("rider_id", sa.String(length=20), nullable=False),
        sa.Column("state", sa.String(length=40), nullable=False),
        sa.Column("dispute_type", sa.String(length=60), nullable=False),
        sa.Column("service_date_ist", sa.Date(), nullable=True),
        sa.Column("policy_cause", sa.String(length=80), nullable=True),
        sa.Column("grouping_key", sa.String(length=200), nullable=False),
        timestamp("created_at"),
        timestamp("updated_at"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.conversation_id"]),
        sa.ForeignKeyConstraint(["rider_id"], ["riders.rider_id"]),
        sa.PrimaryKeyConstraint("dispute_id"),
    )
    op.create_index("ix_disputes_rider_state", "disputes", ["rider_id", "state"])
    op.create_index("ix_disputes_rider_service_date", "disputes", ["rider_id", "service_date_ist"])

    op.create_table(
        "claim_revisions",
        sa.Column("revision_id", sa.String(length=50), nullable=False),
        sa.Column("dispute_id", sa.String(length=50), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("source_message_id", sa.String(length=150), nullable=False),
        sa.Column("claim_payload", sa.JSON(), nullable=False),
        sa.Column("contradictions", sa.JSON(), nullable=False),
        timestamp("created_at"),
        sa.ForeignKeyConstraint(["dispute_id"], ["disputes.dispute_id"]),
        sa.ForeignKeyConstraint(["source_message_id"], ["messages.message_id"]),
        sa.PrimaryKeyConstraint("revision_id"),
        sa.UniqueConstraint("dispute_id", "revision_number", name="uq_claim_revision_number"),
    )

    op.create_table(
        "evidence",
        sa.Column("evidence_id", sa.String(length=50), nullable=False),
        sa.Column("dispute_id", sa.String(length=50), nullable=False),
        sa.Column("kind", sa.String(length=60), nullable=False),
        sa.Column("source_ref", sa.String(length=200), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("policy_version", sa.String(length=100), nullable=False),
        sa.Column("normalization_version", sa.String(length=100), nullable=False),
        timestamp("created_at"),
        sa.ForeignKeyConstraint(["dispute_id"], ["disputes.dispute_id"]),
        sa.PrimaryKeyConstraint("evidence_id"),
    )
    op.create_index("ix_evidence_dispute_kind", "evidence", ["dispute_id", "kind"])

    op.create_table(
        "decisions",
        sa.Column("decision_id", sa.String(length=50), nullable=False),
        sa.Column("dispute_id", sa.String(length=50), nullable=False),
        sa.Column("decision_version", sa.Integer(), nullable=False),
        sa.Column("decision_class", sa.String(length=40), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=True),
        sa.Column("reason_codes", sa.JSON(), nullable=False),
        sa.Column("evidence_ids", sa.JSON(), nullable=False),
        sa.Column("policy_version", sa.String(length=100), nullable=False),
        timestamp("created_at"),
        sa.ForeignKeyConstraint(["dispute_id"], ["disputes.dispute_id"]),
        sa.PrimaryKeyConstraint("decision_id"),
        sa.UniqueConstraint("dispute_id", "decision_version", name="uq_decision_version"),
    )
    op.create_index("ix_decisions_decision_class", "decisions", ["decision_class"])
    op.create_index("ix_decisions_dispute_created", "decisions", ["dispute_id", "created_at"])

    op.create_table(
        "rule_check_results",
        sa.Column("result_id", sa.String(length=50), nullable=False),
        sa.Column("decision_id", sa.String(length=50), nullable=False),
        sa.Column("gate", sa.String(length=20), nullable=False),
        sa.Column("rule_id", sa.String(length=100), nullable=False),
        sa.Column("result", sa.String(length=30), nullable=False),
        sa.Column("rule_version", sa.String(length=100), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        timestamp("checked_at"),
        sa.ForeignKeyConstraint(["decision_id"], ["decisions.decision_id"]),
        sa.PrimaryKeyConstraint("result_id"),
        sa.UniqueConstraint(
            "decision_id", "gate", "rule_id", name="uq_rule_check_decision_gate_rule"
        ),
    )
    op.create_index("ix_rule_checks_decision_gate", "rule_check_results", ["decision_id", "gate"])

    op.create_table(
        "trace_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("rider_id", sa.String(length=20), nullable=False),
        sa.Column("conversation_id", sa.String(length=50), nullable=True),
        sa.Column("message_id", sa.String(length=150), nullable=True),
        sa.Column("dispute_id", sa.String(length=50), nullable=True),
        sa.Column("event_type", sa.String(length=30), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("input", sa.JSON(), nullable=False),
        sa.Column("output", sa.JSON(), nullable=False),
        timestamp("created_at"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.conversation_id"]),
        sa.ForeignKeyConstraint(["dispute_id"], ["disputes.dispute_id"]),
        sa.ForeignKeyConstraint(["message_id"], ["messages.message_id"]),
        sa.ForeignKeyConstraint(["rider_id"], ["riders.rider_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_trace_events_rider_order", "trace_events", ["rider_id", "id"])
    op.create_index("ix_trace_events_message", "trace_events", ["message_id"])

    op.create_table(
        "ops_items",
        sa.Column("item_id", sa.String(length=50), nullable=False),
        sa.Column("logical_key", sa.String(length=200), nullable=False),
        sa.Column("rider_id", sa.String(length=20), nullable=False),
        sa.Column("dispute_id", sa.String(length=50), nullable=False),
        sa.Column("decision_id", sa.String(length=50), nullable=False),
        sa.Column("item_type", sa.String(length=20), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("reviewer", sa.String(length=100), nullable=True),
        sa.Column("reviewer_note", sa.String(length=1000), nullable=True),
        timestamp("created_at"),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(item_type = 'approval' AND amount IS NOT NULL AND amount > 0) "
            "OR (item_type = 'escalation' AND amount IS NULL)",
            name="ck_ops_item_type_amount",
        ),
        sa.ForeignKeyConstraint(["decision_id"], ["decisions.decision_id"]),
        sa.ForeignKeyConstraint(["dispute_id"], ["disputes.dispute_id"]),
        sa.ForeignKeyConstraint(["rider_id"], ["riders.rider_id"]),
        sa.PrimaryKeyConstraint("item_id"),
        sa.UniqueConstraint("logical_key"),
    )
    op.create_index("ix_ops_items_pending", "ops_items", ["status", "created_at"])
    op.create_index("ix_ops_items_rider", "ops_items", ["rider_id", "created_at"])

    op.create_table(
        "payout_intents",
        sa.Column("intent_id", sa.String(length=50), nullable=False),
        sa.Column("rider_id", sa.String(length=20), nullable=False),
        sa.Column("dispute_id", sa.String(length=50), nullable=False),
        sa.Column("decision_id", sa.String(length=50), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("provider_reference", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("provider_payout_id", sa.String(length=100), nullable=True),
        timestamp("created_at"),
        timestamp("updated_at"),
        sa.CheckConstraint("amount > 0", name="ck_payout_intent_positive_amount"),
        sa.ForeignKeyConstraint(["decision_id"], ["decisions.decision_id"]),
        sa.ForeignKeyConstraint(["dispute_id"], ["disputes.dispute_id"]),
        sa.ForeignKeyConstraint(["rider_id"], ["riders.rider_id"]),
        sa.PrimaryKeyConstraint("intent_id"),
        sa.UniqueConstraint("idempotency_key"),
        sa.UniqueConstraint("provider_payout_id"),
    )
    op.create_index("ix_payout_intents_rider_created", "payout_intents", ["rider_id", "created_at"])
    op.create_index("ix_payout_intents_status", "payout_intents", ["status"])

    op.create_table(
        "payout_attempts",
        sa.Column("attempt_id", sa.String(length=50), nullable=False),
        sa.Column("intent_id", sa.String(length=50), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("request_payload", sa.JSON(), nullable=False),
        sa.Column("response_payload", sa.JSON(), nullable=True),
        sa.Column("outcome", sa.String(length=40), nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        timestamp("created_at"),
        sa.ForeignKeyConstraint(["intent_id"], ["payout_intents.intent_id"]),
        sa.PrimaryKeyConstraint("attempt_id"),
        sa.UniqueConstraint("intent_id", "attempt_number", name="uq_payout_attempt_number"),
    )
    op.create_index(
        "ix_payout_attempts_intent_created", "payout_attempts", ["intent_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_payout_attempts_intent_created", table_name="payout_attempts")
    op.drop_table("payout_attempts")
    op.drop_index("ix_payout_intents_status", table_name="payout_intents")
    op.drop_index("ix_payout_intents_rider_created", table_name="payout_intents")
    op.drop_table("payout_intents")
    op.drop_index("ix_ops_items_rider", table_name="ops_items")
    op.drop_index("ix_ops_items_pending", table_name="ops_items")
    op.drop_table("ops_items")
    op.drop_index("ix_trace_events_message", table_name="trace_events")
    op.drop_index("ix_trace_events_rider_order", table_name="trace_events")
    op.drop_table("trace_events")
    op.drop_index("ix_rule_checks_decision_gate", table_name="rule_check_results")
    op.drop_table("rule_check_results")
    op.drop_index("ix_decisions_dispute_created", table_name="decisions")
    op.drop_index("ix_decisions_decision_class", table_name="decisions")
    op.drop_table("decisions")
    op.drop_index("ix_evidence_dispute_kind", table_name="evidence")
    op.drop_table("evidence")
    op.drop_table("claim_revisions")
    op.drop_index("ix_disputes_rider_service_date", table_name="disputes")
    op.drop_index("ix_disputes_rider_state", table_name="disputes")
    op.drop_table("disputes")
    op.drop_index("ix_messages_rider_received", table_name="messages")
    op.drop_index("ix_messages_status", table_name="messages")
    op.drop_table("messages")
    op.drop_index("ix_conversations_state", table_name="conversations")
    op.drop_table("conversations")
