"""Add durable payout outbox retry and reconciliation fields.

Revision ID: 20261031_0005
Revises: 20261031_0004
Create Date: 2026-10-31
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261031_0005"
down_revision: str | None = "20261031_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "payout_intents",
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "payout_intents",
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )
    op.add_column(
        "payout_intents",
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "payout_intents",
        sa.Column("last_error", sa.String(length=200), nullable=True),
    )
    op.add_column(
        "payout_intents",
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_payout_intents_due",
        "payout_intents",
        ["status", "next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_payout_intents_due", table_name="payout_intents")
    op.drop_column("payout_intents", "confirmed_at")
    op.drop_column("payout_intents", "last_error")
    op.drop_column("payout_intents", "lease_until")
    op.drop_column("payout_intents", "next_attempt_at")
    op.drop_column("payout_intents", "attempt_count")
