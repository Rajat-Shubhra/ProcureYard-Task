"""Create raw and normalized source-ingestion tables.

Revision ID: 20261031_0002
Revises: 20261031_0001
Create Date: 2026-10-31
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261031_0002"
down_revision: str | None = "20261031_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "import_batches",
        sa.Column("batch_id", sa.String(length=50), nullable=False),
        sa.Column("source_snapshot_version", sa.String(length=100), nullable=False),
        sa.Column("source_digest", sa.String(length=71), nullable=False),
        sa.Column("normalization_version", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("counts", sa.JSON(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("batch_id"),
    )
    op.create_index(
        "ix_import_batches_source_snapshot_version",
        "import_batches",
        ["source_snapshot_version"],
    )
    op.create_index("ix_import_batches_status", "import_batches", ["status"])

    op.create_table(
        "riders",
        sa.Column("rider_id", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("city", sa.String(length=100), nullable=False),
        sa.Column("joined_on", sa.Date(), nullable=False),
        sa.Column("source_snapshot_version", sa.String(length=100), nullable=False),
        sa.Column("raw_payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("rider_id"),
    )
    op.create_index("ix_riders_city", "riders", ["city"])

    op.create_table(
        "raw_source_rows",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("first_seen_batch_id", sa.String(length=50), nullable=False),
        sa.Column("source_snapshot_version", sa.String(length=100), nullable=False),
        sa.Column("source_file", sa.String(length=100), nullable=False),
        sa.Column("source_row_number", sa.Integer(), nullable=False),
        sa.Column("row_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "imported_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["first_seen_batch_id"], ["import_batches.batch_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_snapshot_version",
            "source_file",
            "source_row_number",
            name="uq_raw_source_row_location",
        ),
    )

    op.create_table(
        "normalized_trips",
        sa.Column("trip_id", sa.String(length=20), nullable=False),
        sa.Column("rider_id", sa.String(length=20), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("service_date_ist", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("raw_distance_value", sa.Numeric(precision=12, scale=3), nullable=False),
        sa.Column("distance_km", sa.Numeric(precision=8, scale=3), nullable=False),
        sa.Column("surge_multiplier", sa.Numeric(precision=6, scale=3), nullable=False),
        sa.Column("normalization_version", sa.String(length=100), nullable=False),
        sa.Column("source_snapshot_version", sa.String(length=100), nullable=False),
        sa.Column("quality_flags", sa.JSON(), nullable=False),
        sa.Column("exact_duplicate_row_count", sa.Integer(), nullable=False),
        sa.Column("raw_payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["rider_id"], ["riders.rider_id"]),
        sa.PrimaryKeyConstraint("trip_id"),
    )
    op.create_index(
        "ix_normalized_trips_rider_service_date",
        "normalized_trips",
        ["rider_id", "service_date_ist"],
    )
    op.create_index("ix_normalized_trips_status", "normalized_trips", ["status"])

    op.create_table(
        "payout_lines",
        sa.Column("line_id", sa.String(length=30), nullable=False),
        sa.Column("payout_date", sa.Date(), nullable=False),
        sa.Column("rider_id", sa.String(length=20), nullable=False),
        sa.Column("line_type", sa.String(length=40), nullable=False),
        sa.Column("trip_id", sa.String(length=20), nullable=True),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("source_snapshot_version", sa.String(length=100), nullable=False),
        sa.Column("raw_payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["rider_id"], ["riders.rider_id"]),
        sa.ForeignKeyConstraint(["trip_id"], ["normalized_trips.trip_id"]),
        sa.PrimaryKeyConstraint("line_id"),
    )
    op.create_index("ix_payout_lines_rider_date", "payout_lines", ["rider_id", "payout_date"])
    op.create_index("ix_payout_lines_trip_type", "payout_lines", ["trip_id", "line_type"])

    op.create_table(
        "source_findings",
        sa.Column("finding_id", sa.String(length=72), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("first_seen_batch_id", sa.String(length=50), nullable=False),
        sa.Column("source_snapshot_version", sa.String(length=100), nullable=False),
        sa.Column("source_file", sa.String(length=100), nullable=False),
        sa.Column("source_row_number", sa.Integer(), nullable=True),
        sa.Column("record_key", sa.String(length=100), nullable=False),
        sa.Column("rule_id", sa.String(length=100), nullable=False),
        sa.Column("severity", sa.String(length=20), nullable=False),
        sa.Column("requires_escalation", sa.Boolean(), nullable=False),
        sa.Column("message", sa.String(length=500), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["first_seen_batch_id"], ["import_batches.batch_id"]),
        sa.PrimaryKeyConstraint("finding_id"),
        sa.UniqueConstraint("fingerprint"),
    )
    op.create_index(
        "ix_source_findings_escalation",
        "source_findings",
        ["requires_escalation", "severity"],
    )
    op.create_index("ix_source_findings_rule_id", "source_findings", ["rule_id"])


def downgrade() -> None:
    op.drop_index("ix_source_findings_rule_id", table_name="source_findings")
    op.drop_index("ix_source_findings_escalation", table_name="source_findings")
    op.drop_table("source_findings")
    op.drop_index("ix_payout_lines_trip_type", table_name="payout_lines")
    op.drop_index("ix_payout_lines_rider_date", table_name="payout_lines")
    op.drop_table("payout_lines")
    op.drop_index("ix_normalized_trips_status", table_name="normalized_trips")
    op.drop_index("ix_normalized_trips_rider_service_date", table_name="normalized_trips")
    op.drop_table("normalized_trips")
    op.drop_table("raw_source_rows")
    op.drop_index("ix_riders_city", table_name="riders")
    op.drop_table("riders")
    op.drop_index("ix_import_batches_status", table_name="import_batches")
    op.drop_index("ix_import_batches_source_snapshot_version", table_name="import_batches")
    op.drop_table("import_batches")
