from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"

    worker_name: Mapped[str] = mapped_column(String(100), primary_key=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class ImportBatch(Base):
    __tablename__ = "import_batches"

    batch_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    source_snapshot_version: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    source_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    normalization_version: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    counts: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RawSourceRow(Base):
    __tablename__ = "raw_source_rows"
    __table_args__ = (
        UniqueConstraint(
            "source_snapshot_version",
            "source_file",
            "source_row_number",
            name="uq_raw_source_row_location",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    first_seen_batch_id: Mapped[str] = mapped_column(
        ForeignKey("import_batches.batch_id"), nullable=False
    )
    source_snapshot_version: Mapped[str] = mapped_column(String(100), nullable=False)
    source_file: Mapped[str] = mapped_column(String(100), nullable=False)
    source_row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    row_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Rider(Base):
    __tablename__ = "riders"

    rider_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    city: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    joined_on: Mapped[date] = mapped_column(Date, nullable=False)
    source_snapshot_version: Mapped[str] = mapped_column(String(100), nullable=False)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class NormalizedTrip(Base):
    __tablename__ = "normalized_trips"
    __table_args__ = (
        Index("ix_normalized_trips_rider_service_date", "rider_id", "service_date_ist"),
    )

    trip_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    rider_id: Mapped[str] = mapped_column(ForeignKey("riders.rider_id"), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    service_date_ist: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    raw_distance_value: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    distance_km: Mapped[Decimal] = mapped_column(Numeric(8, 3), nullable=False)
    surge_multiplier: Mapped[Decimal] = mapped_column(Numeric(6, 3), nullable=False)
    normalization_version: Mapped[str] = mapped_column(String(100), nullable=False)
    source_snapshot_version: Mapped[str] = mapped_column(String(100), nullable=False)
    quality_flags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    exact_duplicate_row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class PayoutLine(Base):
    __tablename__ = "payout_lines"
    __table_args__ = (
        Index("ix_payout_lines_rider_date", "rider_id", "payout_date"),
        Index("ix_payout_lines_trip_type", "trip_id", "line_type"),
    )

    line_id: Mapped[str] = mapped_column(String(30), primary_key=True)
    payout_date: Mapped[date] = mapped_column(Date, nullable=False)
    rider_id: Mapped[str] = mapped_column(ForeignKey("riders.rider_id"), nullable=False)
    line_type: Mapped[str] = mapped_column(String(40), nullable=False)
    trip_id: Mapped[str | None] = mapped_column(ForeignKey("normalized_trips.trip_id"))
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    source_snapshot_version: Mapped[str] = mapped_column(String(100), nullable=False)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class SourceFinding(Base):
    __tablename__ = "source_findings"
    __table_args__ = (Index("ix_source_findings_escalation", "requires_escalation", "severity"),)

    finding_id: Mapped[str] = mapped_column(String(72), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    first_seen_batch_id: Mapped[str] = mapped_column(
        ForeignKey("import_batches.batch_id"), nullable=False
    )
    source_snapshot_version: Mapped[str] = mapped_column(String(100), nullable=False)
    source_file: Mapped[str] = mapped_column(String(100), nullable=False)
    source_row_number: Mapped[int | None] = mapped_column(Integer)
    record_key: Mapped[str] = mapped_column(String(100), nullable=False)
    rule_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    requires_escalation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    message: Mapped[str] = mapped_column(String(500), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Conversation(Base):
    __tablename__ = "conversations"

    conversation_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    rider_id: Mapped[str] = mapped_column(ForeignKey("riders.rider_id"), nullable=False)
    state: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    turn_number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    memory_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    language: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown")
    memory: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_rider_received", "rider_id", "received_at"),)

    message_id: Mapped[str] = mapped_column(String(150), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.conversation_id"), nullable=False
    )
    rider_id: Mapped[str] = mapped_column(ForeignKey("riders.rider_id"), nullable=False)
    turn_number: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(String(4000), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    reply: Mapped[str | None] = mapped_column(String(4000))
    response_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Dispute(Base):
    __tablename__ = "disputes"
    __table_args__ = (
        Index("ix_disputes_rider_state", "rider_id", "state"),
        Index("ix_disputes_rider_service_date", "rider_id", "service_date_ist"),
    )

    dispute_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.conversation_id"), nullable=False
    )
    rider_id: Mapped[str] = mapped_column(ForeignKey("riders.rider_id"), nullable=False)
    state: Mapped[str] = mapped_column(String(40), nullable=False)
    dispute_type: Mapped[str] = mapped_column(String(60), nullable=False)
    service_date_ist: Mapped[date | None] = mapped_column(Date)
    policy_cause: Mapped[str | None] = mapped_column(String(80))
    grouping_key: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Complaint(Base):
    __tablename__ = "complaints"
    __table_args__ = (
        UniqueConstraint(
            "source_message_id",
            "complaint_index",
            name="uq_complaint_message_index",
        ),
        Index("ix_complaints_dispute", "dispute_id", "created_at"),
        Index("ix_complaints_rider_status", "rider_id", "status"),
    )

    complaint_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    source_message_id: Mapped[str] = mapped_column(
        ForeignKey("messages.message_id"), nullable=False
    )
    rider_id: Mapped[str] = mapped_column(ForeignKey("riders.rider_id"), nullable=False)
    dispute_id: Mapped[str | None] = mapped_column(ForeignKey("disputes.dispute_id"))
    complaint_index: Mapped[int] = mapped_column(Integer, nullable=False)
    complaint_type: Mapped[str] = mapped_column(String(60), nullable=False)
    summary: Mapped[str] = mapped_column(String(500), nullable=False)
    service_date_ist: Mapped[date | None] = mapped_column(Date)
    trip_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    alleged_amount_rupees: Mapped[int | None] = mapped_column(Integer)
    grounding_status: Mapped[str] = mapped_column(String(30), nullable=False)
    missing_fields: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    relation_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    target_dispute_id: Mapped[str | None] = mapped_column(ForeignKey("disputes.dispute_id"))
    grouping_key: Mapped[str] = mapped_column(String(300), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    gate_reasons: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MessageInterpretation(Base):
    __tablename__ = "message_interpretations"

    interpretation_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    message_id: Mapped[str] = mapped_column(
        ForeignKey("messages.message_id"), nullable=False, unique=True
    )
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    model: Mapped[str] = mapped_column(String(150), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(100), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(50), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_request_id: Mapped[str | None] = mapped_column(String(150))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ClaimRevision(Base):
    __tablename__ = "claim_revisions"
    __table_args__ = (
        UniqueConstraint("dispute_id", "revision_number", name="uq_claim_revision_number"),
    )

    revision_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    dispute_id: Mapped[str] = mapped_column(ForeignKey("disputes.dispute_id"), nullable=False)
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    source_message_id: Mapped[str] = mapped_column(
        ForeignKey("messages.message_id"), nullable=False
    )
    claim_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    contradictions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Evidence(Base):
    __tablename__ = "evidence"
    __table_args__ = (Index("ix_evidence_dispute_kind", "dispute_id", "kind"),)

    evidence_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    dispute_id: Mapped[str] = mapped_column(ForeignKey("disputes.dispute_id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(60), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    policy_version: Mapped[str] = mapped_column(String(100), nullable=False)
    normalization_version: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Decision(Base):
    __tablename__ = "decisions"
    __table_args__ = (
        UniqueConstraint("dispute_id", "decision_version", name="uq_decision_version"),
        Index("ix_decisions_dispute_created", "dispute_id", "created_at"),
    )

    decision_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    dispute_id: Mapped[str] = mapped_column(ForeignKey("disputes.dispute_id"), nullable=False)
    decision_version: Mapped[int] = mapped_column(Integer, nullable=False)
    decision_class: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    amount: Mapped[int | None] = mapped_column(Integer)
    reason_codes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    evidence_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    policy_version: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RuleCheckResult(Base):
    __tablename__ = "rule_check_results"
    __table_args__ = (
        UniqueConstraint(
            "gate_run_id",
            "rule_id",
            name="uq_rule_check_gate_run_rule",
        ),
        Index("ix_rule_checks_gate_run", "gate_run_id"),
        Index("ix_rule_checks_decision_gate", "decision_id", "gate"),
    )

    result_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    gate_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("gate_runs.gate_run_id", name="fk_rule_check_gate_run")
    )
    decision_id: Mapped[str | None] = mapped_column(ForeignKey("decisions.decision_id"))
    gate: Mapped[str] = mapped_column(String(20), nullable=False)
    rule_id: Mapped[str] = mapped_column(String(100), nullable=False)
    result: Mapped[str] = mapped_column(String(30), nullable=False)
    rule_version: Mapped[str] = mapped_column(String(100), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class GateRun(Base):
    __tablename__ = "gate_runs"
    __table_args__ = (
        Index("ix_gate_runs_message_created", "message_id", "created_at"),
        Index("ix_gate_runs_decision_gate", "decision_id", "gate"),
    )

    gate_run_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    message_id: Mapped[str | None] = mapped_column(ForeignKey("messages.message_id"))
    dispute_id: Mapped[str | None] = mapped_column(ForeignKey("disputes.dispute_id"))
    decision_id: Mapped[str | None] = mapped_column(ForeignKey("decisions.decision_id"))
    gate: Mapped[str] = mapped_column(String(20), nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    checker_version: Mapped[str] = mapped_column(String(100), nullable=False)
    blocking_reasons: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TraceEvent(Base):
    __tablename__ = "trace_events"
    __table_args__ = (
        Index("ix_trace_events_rider_order", "rider_id", "id"),
        Index("ix_trace_events_message", "message_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rider_id: Mapped[str] = mapped_column(ForeignKey("riders.rider_id"), nullable=False)
    conversation_id: Mapped[str | None] = mapped_column(ForeignKey("conversations.conversation_id"))
    message_id: Mapped[str | None] = mapped_column(ForeignKey("messages.message_id"))
    dispute_id: Mapped[str | None] = mapped_column(ForeignKey("disputes.dispute_id"))
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    input_payload: Mapped[dict[str, Any]] = mapped_column("input", JSON, nullable=False)
    output_payload: Mapped[dict[str, Any]] = mapped_column("output", JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class OpsItem(Base):
    __tablename__ = "ops_items"
    __table_args__ = (
        CheckConstraint(
            "(item_type = 'approval' AND amount IS NOT NULL AND amount > 0) "
            "OR (item_type = 'escalation' AND amount IS NULL)",
            name="ck_ops_item_type_amount",
        ),
        Index("ix_ops_items_pending", "status", "created_at"),
        Index("ix_ops_items_rider", "rider_id", "created_at"),
    )

    item_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    logical_key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    rider_id: Mapped[str] = mapped_column(ForeignKey("riders.rider_id"), nullable=False)
    dispute_id: Mapped[str] = mapped_column(ForeignKey("disputes.dispute_id"), nullable=False)
    decision_id: Mapped[str] = mapped_column(ForeignKey("decisions.decision_id"), nullable=False)
    item_type: Mapped[str] = mapped_column(String(20), nullable=False)
    amount: Mapped[int | None] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    reviewer: Mapped[str | None] = mapped_column(String(100))
    reviewer_note: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PayoutIntent(Base):
    __tablename__ = "payout_intents"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_payout_intent_positive_amount"),
        Index("ix_payout_intents_rider_created", "rider_id", "created_at"),
        Index("ix_payout_intents_status", "status"),
        Index("ix_payout_intents_due", "status", "next_attempt_at"),
    )

    intent_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    rider_id: Mapped[str] = mapped_column(ForeignKey("riders.rider_id"), nullable=False)
    dispute_id: Mapped[str] = mapped_column(ForeignKey("disputes.dispute_id"), nullable=False)
    decision_id: Mapped[str] = mapped_column(ForeignKey("decisions.decision_id"), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_reference: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    provider_payout_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(200))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class PayoutAttempt(Base):
    __tablename__ = "payout_attempts"
    __table_args__ = (
        UniqueConstraint("intent_id", "attempt_number", name="uq_payout_attempt_number"),
        Index("ix_payout_attempts_intent_created", "intent_id", "created_at"),
    )

    attempt_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    intent_id: Mapped[str] = mapped_column(ForeignKey("payout_intents.intent_id"), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    response_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    outcome: Mapped[str] = mapped_column(String(40), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
