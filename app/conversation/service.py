from __future__ import annotations

import hashlib
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agent.contracts import (
    ExtractedComplaint,
    InterpretationContext,
    InterpretationResult,
    MessageEnvelope,
)
from app.agent.fallback import HeuristicInterpretationProvider
from app.agent.provider import (
    GroqInterpretationProvider,
    InterpretationProvider,
    InterpretationProviderError,
)
from app.config import Settings
from app.db import Database, lock_rider
from app.domain.coverage import CommitmentSummary, logical_reference, summarize_commitments
from app.domain.decision import DecisionPlan, plan_decision
from app.domain.payout import DisputeEvaluation, PayoutPolicyEngine
from app.errors import RetryableProcessingError
from app.gates.action import ActionGate, ActionProposal
from app.gates.interpretation import InterpretationGate
from app.gates.reply import ReplyGate, ReplyProposal
from app.gates.reports import GateReport, persist_gate_report
from app.models import (
    ClaimRevision,
    Complaint,
    Conversation,
    Decision,
    Dispute,
    Evidence,
    Message,
    MessageInterpretation,
    NormalizedTrip,
    OpsItem,
    PayoutIntent,
    Rider,
)
from app.providers.payswift import PaySwiftClient, payout_request_fingerprint
from app.trace import add_trace_event

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
# Disputes a later turn may refer to. Paid/explained/rejected disputes stay linkable so a
# re-sent or rephrased complaint is reconciled against the existing action instead of
# opening a parallel dispute (CONVERSATION_WORKFLOW §4).
OPEN_DISPUTE_STATES = {
    "draft",
    "needs_clarification",
    "verifying",
    "verified_owed",
    "awaiting_approval",
    "escalated",
    "payout_processing",
    "paid",
    "resolved_explained",
    "rejected",
}
# States in which a new accepted complaint may re-open verification of an existing dispute.
REVERIFIABLE_DISPUTE_STATES = {
    "draft",
    "needs_clarification",
    "verifying",
    "verified_owed",
    "resolved_explained",
    "error",
}
# A message that stays in ``processing`` this long without a reply is considered abandoned
# (crash after durable receipt) and is resumed by the next vendor delivery.
PROCESSING_RESUME_AFTER = timedelta(seconds=20)
CONCURRENT_DUPLICATE_WAIT_SECONDS = 1.5


@dataclass(frozen=True, slots=True)
class MessageResult:
    reply: str
    message_id: str
    conversation_id: str | None
    state: str
    duplicate: bool = False


@dataclass(slots=True)
class Outcome:
    complaint_ids: list[str]
    dispute_id: str
    plan: DecisionPlan
    evaluation: DisputeEvaluation
    action_state: str
    provider_confirmed: bool = False
    approval_pending: bool = False
    escalation_pending: bool = False
    status_note: str = ""


class ConversationService:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        interpretation_provider: InterpretationProvider | None = None,
        fallback_provider: InterpretationProvider | None = None,
        payswift_client: PaySwiftClient | None = None,
    ) -> None:
        self.database = database
        self.settings = settings
        self.provider = interpretation_provider or self._build_provider()
        self.fallback = fallback_provider or HeuristicInterpretationProvider()
        self.payswift = payswift_client or PaySwiftClient(settings)
        self.interpretation_gate = InterpretationGate()
        self.action_gate = ActionGate()
        self.reply_gate = ReplyGate()
        self.policy = PayoutPolicyEngine()

    def process(self, envelope: MessageEnvelope) -> MessageResult:
        reserved = self._reserve(envelope)
        if isinstance(reserved, MessageResult):
            return reserved
        conversation_id, turn_number = reserved
        try:
            return self._process_reserved(envelope, conversation_id, turn_number)
        except RetryableProcessingError:
            raise
        except Exception as exc:  # noqa: BLE001 - converted into a retryable, traced failure
            self._record_processing_failure(envelope, conversation_id, exc)
            raise RetryableProcessingError(
                f"message {envelope.message_id} processing failed: {type(exc).__name__}"
            ) from exc

    def _process_reserved(
        self, envelope: MessageEnvelope, conversation_id: str, turn_number: int
    ) -> MessageResult:
        context = self._load_context(envelope, conversation_id)
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="message-io") as executor:
            interpretation_future = executor.submit(self.provider.interpret, context)
            ledger_future = executor.submit(self.payswift.list_payouts, envelope.rider_id)
            try:
                interpretation = interpretation_future.result()
            except Exception as exc:  # noqa: BLE001 - any provider failure degrades safely
                if not isinstance(
                    exc, InterpretationProviderError | httpx.HTTPError | ValueError
                ):
                    logger.warning(
                        "model_provider_unexpected_error",
                        extra={"error_type": type(exc).__name__},
                    )
                if not self.settings.model_fallback_enabled:
                    return self._model_failure(envelope, conversation_id)
                try:
                    interpretation = self.fallback.interpret(context)
                except Exception:  # noqa: BLE001 - the fail-safe parser must never 5xx
                    logger.exception("fallback_interpretation_failed")
                    return self._model_failure(envelope, conversation_id)
            try:
                ledger = ledger_future.result()
                ledger_ok = True
            except (httpx.HTTPError, ValueError):
                ledger = []
                ledger_ok = False
        return self._persist_and_decide(
            envelope,
            conversation_id,
            turn_number,
            interpretation,
            ledger,
            ledger_ok,
        )

    def _record_processing_failure(
        self, envelope: MessageEnvelope, conversation_id: str, exc: Exception
    ) -> None:
        logger.exception(
            "message_processing_failed",
            extra={"message_id": envelope.message_id, "error_type": type(exc).__name__},
        )
        try:
            with self.database.session() as session, session.begin():
                add_trace_event(
                    session,
                    rider_id=envelope.rider_id,
                    conversation_id=conversation_id,
                    message_id=envelope.message_id,
                    event_type="error",
                    name="processing_failed",
                    input_payload={"message_id": envelope.message_id},
                    output_payload={
                        "error_type": type(exc).__name__,
                        "resume": "next vendor delivery resumes this message",
                    },
                )
        except Exception:  # noqa: BLE001 - tracing must not mask the original failure
            logger.exception("processing_failure_trace_not_written")

    def _build_provider(self) -> InterpretationProvider:
        if self.settings.llm_provider == "groq":
            return GroqInterpretationProvider(self.settings)
        return HeuristicInterpretationProvider()

    def _reserve(self, envelope: MessageEnvelope) -> tuple[str, int] | MessageResult:
        try:
            return self._reserve_once(envelope)
        except IntegrityError:
            # Concurrent delivery lost the insert race: wait briefly for the owner's reply.
            return self._duplicate_result(envelope, wait=True)

    def _duplicate_result(self, envelope: MessageEnvelope, *, wait: bool) -> MessageResult:
        deadline = time.monotonic() + (CONCURRENT_DUPLICATE_WAIT_SECONDS if wait else 0)
        while True:
            with self.database.session() as session:
                existing = session.get(Message, envelope.message_id)
                if existing is None:
                    raise RuntimeError("duplicate message vanished during reservation")
                if existing.rider_id != envelope.rider_id:
                    # D-005: a reused vendor id from another number must never replay
                    # the first rider's reply, trip ids or amounts.
                    return MessageResult(
                        reply="Is message ko verify nahi kar paaya. Support se contact karein.",
                        message_id=envelope.message_id,
                        conversation_id=None,
                        state="rejected",
                        duplicate=True,
                    )
                conversation = session.get(Conversation, existing.conversation_id)
                state = conversation.state if conversation is not None else existing.status
                if existing.reply is not None or time.monotonic() >= deadline:
                    return MessageResult(
                        reply=existing.reply or "Aapka message process ho raha hai.",
                        message_id=existing.message_id,
                        conversation_id=existing.conversation_id,
                        state=state if existing.reply is not None else "processing",
                        duplicate=True,
                    )
            time.sleep(0.1)

    def _reserve_once(self, envelope: MessageEnvelope) -> tuple[str, int] | MessageResult:
        if not envelope.message_id.strip() or not envelope.text.strip():
            raise ValueError("message_id and text must be non-blank")
        if len(envelope.text) > self.settings.message_max_length:
            raise ValueError("message text exceeds the configured limit")
        if envelope.received_at.tzinfo is None:
            raise ValueError("received_at must include a timezone")
        duplicate_wait: bool | None = None
        with self.database.session() as session, session.begin():
            existing = session.get(Message, envelope.message_id)
            if existing is not None:
                if (
                    existing.rider_id == envelope.rider_id
                    and existing.reply is None
                    and existing.status == "processing"
                    and _message_age(existing) > PROCESSING_RESUME_AFTER
                ):
                    # Abandoned after durable receipt (crash/restart): this delivery resumes it.
                    existing.created_at = datetime.now(UTC)
                    add_trace_event(
                        session,
                        rider_id=envelope.rider_id,
                        conversation_id=existing.conversation_id,
                        message_id=envelope.message_id,
                        event_type="message_in",
                        name="vendor_message_resumed",
                        input_payload={"message_id": envelope.message_id},
                        output_payload=None,
                    )
                    return existing.conversation_id, existing.turn_number
                duplicate_wait = existing.reply is None and existing.rider_id == envelope.rider_id
        if duplicate_wait is not None:
            # Wait outside the reservation transaction so the owning request can commit.
            return self._duplicate_result(envelope, wait=duplicate_wait)
        with self.database.session() as session, session.begin():
            rider = session.get(Rider, envelope.rider_id)
            if rider is None:
                return MessageResult(
                    reply="Is account ko verify nahi kar paaya. Support se contact karein.",
                    message_id=envelope.message_id,
                    conversation_id=None,
                    state="unknown_rider",
                )
            _lock_rider(session, envelope.rider_id)
            conversation = session.scalar(
                select(Conversation)
                .where(Conversation.rider_id == envelope.rider_id)
                .order_by(Conversation.updated_at.desc())
                .limit(1)
            )
            if conversation is None or conversation.state in {"closed", "error"}:
                conversation = Conversation(
                    conversation_id=f"conv_{uuid4().hex}",
                    rider_id=envelope.rider_id,
                    state="new",
                    turn_number=0,
                    memory_version=0,
                    language="unknown",
                    memory={},
                )
                session.add(conversation)
                session.flush()
            conversation.turn_number += 1
            conversation.updated_at = datetime.now(UTC)
            message = Message(
                message_id=envelope.message_id,
                conversation_id=conversation.conversation_id,
                rider_id=envelope.rider_id,
                turn_number=conversation.turn_number,
                text=envelope.text,
                received_at=envelope.received_at,
                status="processing",
            )
            session.add(message)
            add_trace_event(
                session,
                rider_id=envelope.rider_id,
                conversation_id=conversation.conversation_id,
                message_id=envelope.message_id,
                event_type="message_in",
                name="vendor_message_received",
                input_payload={
                    "message_id": envelope.message_id,
                    "text": envelope.text,
                    "received_at": envelope.received_at.isoformat(),
                },
                output_payload=None,
            )
            return conversation.conversation_id, conversation.turn_number

    def _load_context(
        self, envelope: MessageEnvelope, conversation_id: str
    ) -> InterpretationContext:
        with self.database.session() as session:
            open_disputes = list(
                session.scalars(
                    select(Dispute).where(
                        Dispute.conversation_id == conversation_id,
                        Dispute.state.in_(OPEN_DISPUTE_STATES),
                    )
                )
            )
            dispute_context: list[dict[str, Any]] = []
            for dispute in open_disputes:
                latest = session.scalar(
                    select(Decision)
                    .where(Decision.dispute_id == dispute.dispute_id)
                    .order_by(Decision.decision_version.desc())
                    .limit(1)
                )
                effective_date, trip_ids = _dispute_evidence_summary(session, dispute, latest)
                dispute_context.append(
                    {
                        "dispute_id": dispute.dispute_id,
                        "dispute_type": dispute.dispute_type,
                        "service_date_ist": effective_date,
                        "trip_ids": list(trip_ids),
                        "state": dispute.state,
                        "amount": latest.amount if latest else None,
                    }
                )
            recent = list(
                session.scalars(
                    select(Message)
                    .where(Message.conversation_id == conversation_id)
                    .order_by(Message.turn_number.desc())
                    .limit(4)
                )
            )
            recent_turns = tuple(
                {
                    "message_id": item.message_id,
                    "turn_number": item.turn_number,
                    "text": item.text,
                    "status": item.status,
                    "reply": item.reply,
                }
                for item in reversed(recent)
                if item.message_id != envelope.message_id
            )
        return InterpretationContext(
            envelope=envelope,
            open_disputes=tuple(dispute_context),
            recent_turns=recent_turns,
        )

    def _persist_and_decide(
        self,
        envelope: MessageEnvelope,
        conversation_id: str,
        turn_number: int,
        result: InterpretationResult,
        ledger: list[dict[str, Any]],
        ledger_ok: bool,
    ) -> MessageResult:
        with self.database.session() as session, session.begin():
            _lock_rider(session, envelope.rider_id)
            conversation = session.get(Conversation, conversation_id)
            message = session.get(Message, envelope.message_id)
            assert conversation is not None and message is not None
            open_ids = set(
                session.scalars(
                    select(Dispute.dispute_id).where(
                        Dispute.conversation_id == conversation_id,
                        Dispute.state.in_(OPEN_DISPUTE_STATES),
                    )
                )
            )
            gate = self.interpretation_gate.check(result.interpretation, open_dispute_ids=open_ids)
            session.add(
                MessageInterpretation(
                    interpretation_id=f"interp_{uuid4().hex}",
                    message_id=envelope.message_id,
                    provider=result.provider,
                    model=result.model,
                    prompt_version=result.prompt_version,
                    schema_version=result.interpretation.schema_version,
                    attempt_count=result.attempt_count,
                    provider_request_id=result.provider_request_id,
                    payload=result.interpretation.raw_payload,
                )
            )
            add_trace_event(
                session,
                rider_id=envelope.rider_id,
                conversation_id=conversation_id,
                message_id=envelope.message_id,
                event_type="tool_call",
                name="model_interpretation",
                input_payload={
                    "prompt_version": result.prompt_version,
                    "schema_version": result.interpretation.schema_version,
                },
                output_payload=result.interpretation.raw_payload,
            )
            gate_run_id = persist_gate_report(session, gate, message_id=envelope.message_id)
            add_trace_event(
                session,
                rider_id=envelope.rider_id,
                conversation_id=conversation_id,
                message_id=envelope.message_id,
                event_type="decision",
                name="interpretation_gate",
                input_payload={"gate_run_id": gate_run_id},
                output_payload=gate.as_dict(),
            )
            complaint_groups = self._persist_complaints(
                session,
                envelope,
                conversation,
                result,
                gate,
                open_ids,
            )
            outcomes: list[Outcome] = []
            for dispute_id, complaint_ids in complaint_groups.items():
                outcome = self._evaluate_and_act(
                    session,
                    envelope,
                    dispute_id,
                    complaint_ids,
                    ledger,
                    ledger_ok,
                )
                if outcome is not None:
                    outcomes.append(outcome)

            self._set_conversation_state(conversation, outcomes)

            reply, payment_status = self._render_reply(
                session,
                envelope,
                result,
                gate,
                outcomes,
            )
            allowed_trip_ids = frozenset(
                trip_id for item in outcomes for trip_id in item.evaluation.trip_ids
            )
            supported_amount_values = {
                item.plan.amount for item in outcomes if item.plan.amount is not None
            }
            provider_confirmed = any(item.provider_confirmed for item in outcomes)
            approval_pending = any(item.approval_pending for item in outcomes)
            escalation_pending = any(item.escalation_pending for item in outcomes)
            if not outcomes:
                pending = session.scalar(
                    select(OpsItem)
                    .where(OpsItem.rider_id == envelope.rider_id, OpsItem.status == "pending")
                    .order_by(OpsItem.created_at.desc())
                    .limit(1)
                )
                intent = session.scalar(
                    select(PayoutIntent)
                    .where(PayoutIntent.rider_id == envelope.rider_id)
                    .order_by(PayoutIntent.created_at.desc())
                    .limit(1)
                )
                if pending is not None:
                    approval_pending = pending.item_type == "approval"
                    escalation_pending = pending.item_type == "escalation"
                    if pending.amount is not None:
                        supported_amount_values.add(pending.amount)
                if intent is not None:
                    supported_amount_values.add(intent.amount)
                    provider_confirmed = intent.status == "confirmed"
                resolved = session.scalar(
                    select(OpsItem)
                    .where(
                        OpsItem.rider_id == envelope.rider_id,
                        OpsItem.status.in_({"rejected", "approved"}),
                    )
                    .order_by(OpsItem.resolved_at.desc())
                    .limit(1)
                )
                if resolved is not None and resolved.amount is not None:
                    supported_amount_values.add(resolved.amount)
            supported_amounts = frozenset(supported_amount_values)
            reply_report = self.reply_gate.check(
                ReplyProposal(
                    reply=reply,
                    envelope_rider_id=envelope.rider_id,
                    allowed_trip_ids=allowed_trip_ids,
                    supported_amounts=supported_amounts,
                    claims_payment_status=payment_status,
                    provider_confirmed=provider_confirmed,
                    approval_pending=approval_pending,
                    escalation_pending=escalation_pending,
                )
            )
            persist_gate_report(session, reply_report, message_id=envelope.message_id)
            if not reply_report.passed:
                reply = "Aapki request record ho gayi hai. Details verify karke update denge."
                payment_status = "none"
            message.reply = reply
            message.response_payload = {
                "reply": reply,
                "message_id": envelope.message_id,
                "conversation_id": conversation_id,
                "state": conversation.state,
                "complaint_outcomes": [
                    {
                        "complaint_ids": item.complaint_ids,
                        "dispute_id": item.dispute_id,
                        "outcome": item.plan.outcome,
                        "amount": item.plan.amount,
                    }
                    for item in outcomes
                ],
            }
            message.status = "completed"
            message.processed_at = datetime.now(UTC)
            conversation.language = result.interpretation.language
            conversation.memory_version += 1
            conversation.memory = self._conversation_memory(session, conversation_id)
            conversation.updated_at = datetime.now(UTC)
            add_trace_event(
                session,
                rider_id=envelope.rider_id,
                conversation_id=conversation_id,
                message_id=envelope.message_id,
                event_type="reply",
                name="rider_reply",
                input_payload=None,
                output_payload={"reply": reply, "payment_status": payment_status},
            )
            return MessageResult(
                reply=reply,
                message_id=envelope.message_id,
                conversation_id=conversation_id,
                state=conversation.state,
            )

    def _persist_complaints(
        self,
        session: Session,
        envelope: MessageEnvelope,
        conversation: Conversation,
        result: InterpretationResult,
        gate: GateReport,
        open_ids: set[str],
    ) -> dict[str, list[str]]:
        accepted = set(gate.accepted_complaint_indexes)
        global_block = any(
            reason
            in {
                "unsupported_model_schema",
                "invalid_safety_flags",
                "identity_or_privacy_risk",
                "prompt_injection",
                "duplicate_complaint_index",
            }
            for reason in gate.blocking_reasons
        )
        group_to_dispute: dict[str, Dispute] = {}
        dispute_complaints: dict[str, list[Complaint]] = {}
        for extracted in result.interpretation.complaints:
            complaint_id = _complaint_id(envelope.message_id, extracted.complaint_index)
            grouping_key = _grouping_key(envelope, extracted)
            is_accepted = extracted.complaint_index in accepted
            can_draft = not global_block and extracted.complaint_type != "unknown"
            dispute: Dispute | None = None
            if is_accepted or can_draft:
                verified_trip_date = _owned_trip_service_date(
                    session, envelope.rider_id, extracted.trip_ids
                )
                effective_date = verified_trip_date or extracted.service_date_ist
                target = extracted.target_dispute_id
                # The model may only link to disputes Gate A saw as this conversation's own;
                # anything else is ignored rather than dereferenced (cross-account safety).
                if target and target in open_ids:
                    dispute = session.get(Dispute, target)
                    if dispute is not None and dispute.rider_id != envelope.rider_id:
                        dispute = None
                    if dispute is not None and not _can_attach_follow_up(
                        session, envelope.rider_id, dispute, extracted
                    ):
                        dispute = None
                if dispute is None:
                    grouping_key = _grouping_key_for(
                        envelope.rider_id,
                        effective_date,
                        extracted.complaint_type,
                        extracted.trip_ids,
                    )
                    dispute = group_to_dispute.get(grouping_key) or session.scalar(
                        select(Dispute)
                        .where(
                            Dispute.rider_id == envelope.rider_id,
                            Dispute.conversation_id == conversation.conversation_id,
                            Dispute.grouping_key == grouping_key,
                        )
                        .order_by(Dispute.created_at.desc())
                        .limit(1)
                    )
                if dispute is None:
                    dispute = Dispute(
                        dispute_id=f"dsp_{uuid4().hex}",
                        conversation_id=conversation.conversation_id,
                        rider_id=envelope.rider_id,
                        state="verifying" if is_accepted else "needs_clarification",
                        dispute_type=extracted.complaint_type,
                        service_date_ist=effective_date,
                        policy_cause=extracted.complaint_type,
                        grouping_key=grouping_key,
                    )
                    session.add(dispute)
                    session.flush()
                elif is_accepted:
                    # Rider-supplied follow-up facts may only fill missing fields. An owned
                    # trip's verified date is authoritative, but free-form text must not
                    # re-date or re-type an already-decided dispute.
                    if dispute.dispute_type == "unknown" and extracted.complaint_type != "unknown":
                        dispute.dispute_type = extracted.complaint_type
                        dispute.policy_cause = extracted.complaint_type
                    if verified_trip_date is not None:
                        dispute.service_date_ist = verified_trip_date
                        dispute.grouping_key = _grouping_key_for(
                            envelope.rider_id, verified_trip_date, dispute.dispute_type
                        )
                    elif (
                        dispute.service_date_ist is None
                        and extracted.service_date_ist is not None
                    ):
                        dispute.service_date_ist = extracted.service_date_ist
                        dispute.grouping_key = _grouping_key_for(
                            envelope.rider_id, dispute.service_date_ist, dispute.dispute_type
                        )
                    if dispute.state in REVERIFIABLE_DISPUTE_STATES:
                        dispute.state = "verifying"
                group_to_dispute[grouping_key] = dispute

            status = (
                "accepted" if is_accepted else "blocked" if global_block else "needs_clarification"
            )
            complaint = Complaint(
                complaint_id=complaint_id,
                source_message_id=envelope.message_id,
                rider_id=envelope.rider_id,
                dispute_id=dispute.dispute_id if dispute else None,
                complaint_index=extracted.complaint_index,
                complaint_type=extracted.complaint_type,
                summary=extracted.summary,
                service_date_ist=extracted.service_date_ist,
                trip_ids=list(extracted.trip_ids),
                alleged_amount_rupees=extracted.alleged_amount_rupees,
                grounding_status=extracted.grounding_status,
                missing_fields=list(extracted.missing_fields),
                relation_kind=extracted.relation_kind,
                target_dispute_id=extracted.target_dispute_id,
                grouping_key=grouping_key,
                status=status,
                gate_reasons=list(gate.blocking_reasons) if not is_accepted else [],
                raw_payload=extracted.raw_payload,
            )
            session.add(complaint)
            if dispute is not None:
                dispute_complaints.setdefault(dispute.dispute_id, []).append(complaint)

        session.flush()
        accepted_groups: dict[str, list[str]] = {}
        for dispute_id, complaints in dispute_complaints.items():
            revision_number = (
                int(
                    session.scalar(
                        select(func.max(ClaimRevision.revision_number)).where(
                            ClaimRevision.dispute_id == dispute_id
                        )
                    )
                    or 0
                )
                + 1
            )
            session.add(
                ClaimRevision(
                    revision_id=f"rev_{uuid4().hex}",
                    dispute_id=dispute_id,
                    revision_number=revision_number,
                    source_message_id=envelope.message_id,
                    claim_payload={
                        "complaints": [item.raw_payload for item in complaints],
                        "message_turn": conversation.turn_number,
                    },
                    contradictions=[
                        {"complaint_id": item.complaint_id, "kind": "model_flagged"}
                        for item in complaints
                        if item.raw_payload["relation"]["contradicts_prior"]
                    ],
                )
            )
            accepted_ids = [item.complaint_id for item in complaints if item.status == "accepted"]
            if accepted_ids:
                accepted_groups[dispute_id] = accepted_ids
        if accepted_groups:
            conversation.state = "investigating"
        elif dispute_complaints:
            conversation.state = "collecting_details"
        elif global_block:
            conversation.state = "resolved_explained"
        return accepted_groups

    def _evaluate_and_act(
        self,
        session: Session,
        envelope: MessageEnvelope,
        dispute_id: str,
        complaint_ids: list[str],
        ledger: list[dict[str, Any]],
        ledger_ok: bool,
    ) -> Outcome | None:
        dispute = session.get(Dispute, dispute_id)
        complaints = list(
            session.scalars(select(Complaint).where(Complaint.complaint_id.in_(complaint_ids)))
        )
        if dispute is None or not complaints:
            return None
        trip_ids = tuple(
            dict.fromkeys(trip_id for complaint in complaints for trip_id in complaint.trip_ids)
        )
        evaluation = self.policy.evaluate(
            session,
            rider_id=envelope.rider_id,
            complaint_type=dispute.dispute_type,
            service_date_ist=dispute.service_date_ist,
            trip_ids=trip_ids,
            received_at=envelope.received_at,
        )
        if evaluation.ownership == "verified" and evaluation.service_date_ist is not None:
            dispute.service_date_ist = evaluation.service_date_ist
            dispute.grouping_key = _grouping_key_for(
                envelope.rider_id, dispute.service_date_ist, dispute.dispute_type
            )
        prior_auto = self._prior_auto_payment(
            session, envelope.rider_id, envelope.received_at, ledger
        )
        # Gate B rule 9: what is already queued, paid, awaiting approval or rejected for
        # this evidence — across disputes, local intents and the PaySwift ledger.
        commitments = summarize_commitments(
            session,
            rider_id=envelope.rider_id,
            dispute_id=dispute_id,
            complaint_type=evaluation.complaint_type,
            service_date_ist=evaluation.service_date_ist,
            trip_ids=evaluation.trip_ids,
            ledger=ledger,
        )
        already_actioned = self._already_actioned(
            session, envelope, dispute, complaint_ids, evaluation, commitments
        )
        if already_actioned is not None:
            return already_actioned
        if evaluation.verified_shortfall and commitments.committed_amount > 0:
            remaining = max(evaluation.verified_shortfall - commitments.committed_amount, 0)
            evaluation = replace(
                evaluation,
                verified_shortfall=remaining,
                reason_codes=evaluation.reason_codes + ("prior_commitment_deducted",),
            )
        plan = plan_decision(
            evaluation,
            prior_auto_payment_today=prior_auto,
            committed_amount=commitments.related_amount,
        )
        pushback = any(
            complaint.complaint_type == "record_disagreement"
            or bool((complaint.raw_payload.get("relation") or {}).get("contradicts_prior"))
            for complaint in complaints
        )
        if pushback and plan.outcome == "explain_no_shortfall" and plan.reason_codes == (
            "no_shortfall",
        ):
            # Rider disagrees with a verified result: re-check happened, record it for ops.
            plan = DecisionPlan(
                "escalate",
                "create_escalation",
                None,
                ("record_disagreement",) + tuple(evaluation.reason_codes),
                escalation_category="record_disagreement",
                escalation_reason=_disagreement_reason(evaluation),
            )
        version = (
            int(
                session.scalar(
                    select(func.max(Decision.decision_version)).where(
                        Decision.dispute_id == dispute_id
                    )
                )
                or 0
            )
            + 1
        )
        evidence_id = f"ev_{uuid4().hex}"
        session.add(
            Evidence(
                evidence_id=evidence_id,
                dispute_id=dispute_id,
                kind="payout_reconciliation",
                source_ref=f"evaluation:{dispute_id}:{version}",
                payload=_evaluation_payload(evaluation)
                | {"committed_amount": commitments.committed_amount},
                policy_version=self.settings.policy_version,
                normalization_version=self.settings.normalization_version,
            )
        )
        decision_id = f"dec_{uuid4().hex}"
        decision = Decision(
            decision_id=decision_id,
            dispute_id=dispute_id,
            decision_version=version,
            decision_class=plan.outcome,
            amount=plan.amount,
            reason_codes=list(plan.reason_codes),
            evidence_ids=[evidence_id],
            policy_version=self.settings.policy_version,
        )
        session.add(decision)
        session.flush()
        action_day = envelope.received_at.astimezone(IST).date().isoformat()
        idempotency_key = f"payout:{dispute_id}:{version}"
        # The provider reference identifies the *logical* dispute (rider, cause, IST service
        # date, evidence trips) plus the IST action day used by the daily auto-pay rule — never
        # a dispute id or decision version, so ledger reconciliation survives re-sent
        # complaints, re-grouped disputes and sandbox restarts.
        logical = logical_reference(
            envelope.rider_id,
            evaluation.complaint_type,
            evaluation.service_date_ist,
            evaluation.trip_ids,
        )
        reference = f"{logical}:day:{action_day}"
        fingerprint = (
            payout_request_fingerprint(envelope.rider_id, plan.amount, reference)
            if plan.amount is not None
            else ""
        )
        existing_intent = session.scalar(
            select(PayoutIntent).where(PayoutIntent.idempotency_key == idempotency_key)
        )
        fingerprint_ok = existing_intent is None or (
            existing_intent.request_fingerprint == fingerprint
        )
        action_report = self.action_gate.check(
            ActionProposal(
                kind=plan.action_kind,  # type: ignore[arg-type]
                origin="deterministic_policy_engine",
                envelope_rider_id=envelope.rider_id,
                dispute_rider_id=dispute.rider_id,
                ownership=evaluation.ownership,
                policy_window=evaluation.policy_window,
                evidence_complete=evaluation.evidence_complete,
                source_ambiguous=evaluation.source_ambiguous,
                verified_shortfall=evaluation.verified_shortfall,
                proposed_amount=plan.amount,
                prior_auto_payment_today=prior_auto,
                payswift_reconciled=ledger_ok,
                idempotency_fingerprint_matches=fingerprint_ok,
                escalation_category=plan.escalation_category,
                escalation_reason=plan.escalation_reason,
                already_covered=bool(
                    plan.amount is not None
                    and evaluation.verified_shortfall is not None
                    and evaluation.verified_shortfall <= 0
                ),
                committed_amount=commitments.related_amount,
            )
        )
        persist_gate_report(
            session,
            action_report,
            message_id=envelope.message_id,
            dispute_id=dispute_id,
            decision_id=decision_id,
        )
        add_trace_event(
            session,
            rider_id=envelope.rider_id,
            conversation_id=dispute.conversation_id,
            message_id=envelope.message_id,
            dispute_id=dispute_id,
            event_type="decision",
            name="deterministic_action_gate",
            input_payload={"decision_id": decision_id, "plan": asdict(plan)},
            output_payload=action_report.as_dict(),
        )
        outcome = Outcome(complaint_ids, dispute_id, plan, evaluation, "not_started")
        if not action_report.passed:
            decision.decision_class = "fail_safe"
            decision.amount = None
            decision.reason_codes = list(action_report.blocking_reasons)
            dispute.state = "error"
            outcome.plan = DecisionPlan(
                "fail_safe", "reply_only", None, tuple(action_report.blocking_reasons)
            )
            outcome.action_state = "blocked"
            return outcome
        if plan.action_kind == "submit_payout" and plan.amount is not None:
            matched = _find_ledger_payout(ledger, envelope.rider_id, plan.amount, reference)
            intent = existing_intent or PayoutIntent(
                intent_id=f"intent_{uuid4().hex}",
                rider_id=envelope.rider_id,
                dispute_id=dispute_id,
                decision_id=decision_id,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                amount=plan.amount,
                provider_reference=reference,
                status="confirmed" if matched else "queued",
                provider_payout_id=str(matched.get("payout_id")) if matched else None,
                confirmed_at=datetime.now(UTC) if matched else None,
            )
            if existing_intent is None:
                session.add(intent)
            dispute.state = "paid" if matched else "payout_processing"
            outcome.action_state = "succeeded" if matched else "queued"
            outcome.provider_confirmed = bool(matched)
        elif plan.action_kind in {"create_approval", "create_escalation"}:
            item_type = "approval" if plan.action_kind == "create_approval" else "escalation"
            amount = plan.amount if item_type == "approval" else None
            item = OpsItem(
                item_id=f"ops_{uuid4().hex}",
                logical_key=f"{item_type}:{dispute_id}:{version}",
                rider_id=envelope.rider_id,
                dispute_id=dispute_id,
                decision_id=decision_id,
                item_type=item_type,
                amount=amount,
                reason=plan.escalation_reason or ", ".join(plan.reason_codes),
                status="pending",
            )
            session.add(item)
            dispute.state = "awaiting_approval" if item_type == "approval" else "escalated"
            outcome.action_state = "succeeded"
            outcome.approval_pending = item_type == "approval"
            outcome.escalation_pending = item_type == "escalation"
        else:
            dispute.state = (
                "needs_clarification"
                if plan.outcome == "ask_clarification"
                else "resolved_explained"
            )
            outcome.action_state = "succeeded"
        return outcome

    def _already_actioned(
        self,
        session: Session,
        envelope: MessageEnvelope,
        dispute: Dispute,
        complaint_ids: list[str],
        evaluation: DisputeEvaluation,
        commitments: CommitmentSummary,
    ) -> Outcome | None:
        """Report an existing action instead of deciding again (Gate B rules 1 and 9).

        Returns ``None`` when the message brings genuinely new evidence or nothing has been
        committed for this evidence yet.
        """
        is_incentive = evaluation.complaint_type == "daily_incentive"
        new_evidence = (
            False
            if is_incentive
            else bool(set(evaluation.trip_ids) - set(commitments.known_trip_ids))
        )
        same_dispute = (
            commitments.same_dispute_intent is not None
            or commitments.same_dispute_pending is not None
        )
        has_shortfall = bool(evaluation.verified_shortfall)
        fully_covered = (
            has_shortfall
            and commitments.committed_amount > 0
            and commitments.committed_amount >= (evaluation.verified_shortfall or 0)
        )
        rejected = (
            commitments.rejected_item
            if commitments.rejected_item is not None
            and (commitments.rejected_item.item_type == "approval" or not new_evidence)
            else None
        )
        if not ((same_dispute and not new_evidence) or fully_covered or rejected):
            return None
        if rejected is not None and not (same_dispute or fully_covered):
            suffix = f": {rejected.reviewer_note}" if rejected.reviewer_note else "."
            if rejected.item_type == "approval" and rejected.amount is not None:
                note = (
                    f"₹{rejected.amount} ka claim ops team ne review karke decline kiya tha"
                    f"{suffix}"
                )
                rejected_amount: int | None = rejected.amount
            else:
                note = f"Aapka case ops team ne review karke close kiya tha{suffix}"
                rejected_amount = None
            plan = DecisionPlan(
                "report_status", "reply_only", rejected_amount, ("ops_rejected",)
            )
            outcome = Outcome(complaint_ids, dispute.dispute_id, plan, evaluation, "succeeded")
            outcome.status_note = note
            self._trace_already_actioned(session, envelope, dispute, plan, commitments)
            return outcome
        intent = commitments.same_dispute_intent
        pending = commitments.same_dispute_pending
        confirmed = commitments.paid_amount
        processing = commitments.processing_amount
        approval = commitments.approval_amount
        trip_label = f"{', '.join(evaluation.trip_ids)}: " if evaluation.trip_ids else ""
        outcome = Outcome(
            complaint_ids,
            dispute.dispute_id,
            DecisionPlan("report_status", "reply_only", None, ("already_actioned",)),
            evaluation,
            "succeeded",
        )
        if pending is not None and pending.item_type == "escalation":
            outcome.escalation_pending = True
            outcome.status_note = (
                "Yeh case ops team ke paas review mein hai; update milte hi batayenge."
            )
        elif (intent is not None and intent.status == "confirmed") or (
            confirmed and not processing
        ):
            amount = (
                intent.amount if intent is not None and intent.status == "confirmed" else confirmed
            )
            outcome.provider_confirmed = True
            outcome.plan = replace(outcome.plan, amount=amount)
            outcome.status_note = (
                f"{trip_label}₹{amount} pehle hi PaySwift par process ho gaya hai."
            )
        elif intent is not None or processing:
            amount = intent.amount if intent is not None else processing
            outcome.action_state = "queued"
            outcome.plan = replace(outcome.plan, amount=amount)
            outcome.status_note = f"{trip_label}₹{amount} ka payout pehle se processing mein hai."
        elif (pending is not None and pending.item_type == "approval") or approval:
            amount = pending.amount if pending is not None and pending.amount else approval
            outcome.approval_pending = True
            outcome.plan = replace(outcome.plan, amount=amount)
            outcome.status_note = f"{trip_label}₹{amount} pehle se ops approval mein hai."
        else:
            outcome.status_note = "Is complaint par pehle hi action ho chuka hai."
        self._trace_already_actioned(session, envelope, dispute, outcome.plan, commitments)
        return outcome

    @staticmethod
    def _trace_already_actioned(
        session: Session,
        envelope: MessageEnvelope,
        dispute: Dispute,
        plan: DecisionPlan,
        commitments: CommitmentSummary,
    ) -> None:
        add_trace_event(
            session,
            rider_id=envelope.rider_id,
            conversation_id=dispute.conversation_id,
            message_id=envelope.message_id,
            dispute_id=dispute.dispute_id,
            event_type="decision",
            name="already_actioned",
            input_payload={"plan": asdict(plan)},
            output_payload={
                "committed_amount": commitments.committed_amount,
                "commitments": [asdict(item) for item in commitments.commitments],
                "rule": "money.not_already_covered",
            },
        )

    def _render_reply(
        self,
        session: Session,
        envelope: MessageEnvelope,
        result: InterpretationResult,
        gate: GateReport,
        outcomes: list[Outcome],
    ) -> tuple[str, str]:
        if any(
            reason in {"identity_or_privacy_risk", "prompt_injection"}
            for reason in gate.blocking_reasons
        ):
            return "Is request ko payout action ke liye use nahi kar sakta.", "none"
        if (
            not result.interpretation.complaints
            and result.interpretation.requested_status != "not_applicable"
        ):
            return self._status_reply(session, envelope.rider_id)
        if (
            not result.interpretation.complaints
            and result.interpretation.message_intent == "greeting_or_thanks"
        ):
            return (
                "Shukriya! Agar koi aur payout sawaal ho to date ya order ID ke saath batayein.",
                "none",
            )
        lines: list[str] = []
        statuses: set[str] = set()
        for index, item in enumerate(outcomes, start=1):
            prefix = f"{index}. " if len(outcomes) > 1 else ""
            amount = item.plan.amount
            evaluation = item.evaluation
            trip_label = f"{', '.join(evaluation.trip_ids)}: " if evaluation.trip_ids else ""
            if item.plan.outcome == "report_status":
                lines.append(f"{prefix}{item.status_note}")
                if item.provider_confirmed:
                    statuses.add("paid")
                elif item.approval_pending:
                    statuses.add("awaiting_approval")
                elif item.escalation_pending:
                    statuses.add("escalated")
                elif item.action_state == "queued":
                    statuses.add("processing")
            elif item.plan.outcome == "auto_pay" and amount is not None:
                if item.provider_confirmed:
                    lines.append(f"{prefix}{trip_label}₹{amount} PaySwift par process ho gaya hai.")
                    statuses.add("paid")
                else:
                    lines.append(
                        f"{prefix}{trip_label}₹{amount} verified hai aur processing mein hai."
                    )
                    statuses.add("processing")
            elif item.plan.outcome == "seek_approval" and amount is not None:
                lines.append(
                    f"{prefix}{trip_label}₹{amount} verified hai aur ops approval mein hai."
                )
                statuses.add("awaiting_approval")
            elif item.plan.outcome == "escalate":
                lines.append(f"{prefix}{_escalation_line(item.plan, evaluation)}")
                statuses.add("escalated")
            elif evaluation.ownership == "mismatch":
                lines.append(f"{prefix}Is trip ko aapke account se verify nahi kar paaya.")
            elif evaluation.policy_window == "expired":
                lines.append(f"{prefix}Yeh complaint 7-day policy window ke bahar hai.")
            elif evaluation.complaint_type == "daily_incentive":
                lines.append(
                    f"{prefix}{evaluation.completed_trip_count or 0} completed trips verify hue; "
                    "incentive ke liye 12 chahiye. Koi payable shortfall nahi mila."
                )
            elif evaluation.complaint_type == "distance":
                lines.append(
                    f"{prefix}Export ka recorded distance check hua, lekin actual route distance "
                    "source se prove nahi ho sakta; automatic payment nahi kar sakta."
                )
            elif evaluation.complaint_type == "policy_exception":
                lines.append(
                    f"{prefix}Recorded rider cancellation par policy penalty hai; exception "
                    "automatically approve nahi ho sakta."
                )
            elif item.plan.outcome == "fail_safe":
                lines.append(f"{prefix}Safe verification complete nahi hui; koi action nahi liya.")
            else:
                trip_text = ", ".join(evaluation.trip_ids)
                detail = f" ({trip_text})" if trip_text else ""
                lines.append(f"{prefix}Records check hue{detail}; koi unpaid shortfall nahi mila.")
        if not lines:
            missing = next(
                (
                    complaint.missing_fields
                    for complaint in result.interpretation.complaints
                    if complaint.missing_fields
                ),
                ("date", "trip_id"),
            )
            requested = " ya ".join(missing[:2])
            return f"Please {requested} share karein, phir main exact payout check karunga.", "none"
        status = next(iter(statuses)) if len(statuses) == 1 else "mixed" if statuses else "none"
        return " ".join(lines), status

    def _status_reply(self, session: Session, rider_id: str) -> tuple[str, str]:
        lines: list[str] = []
        statuses: set[str] = set()
        pending = session.scalar(
            select(OpsItem)
            .where(OpsItem.rider_id == rider_id, OpsItem.status == "pending")
            .order_by(OpsItem.created_at.desc())
            .limit(1)
        )
        intent = session.scalar(
            select(PayoutIntent)
            .where(PayoutIntent.rider_id == rider_id)
            .order_by(PayoutIntent.created_at.desc())
            .limit(1)
        )
        if intent is not None and intent.status == "confirmed":
            lines.append(f"₹{intent.amount} PaySwift par process ho gaya hai.")
            statuses.add("paid")
        elif intent is not None and intent.status != "failed":
            lines.append(f"₹{intent.amount} payout processing mein hai.")
            statuses.add("processing")
        if pending is not None and pending.item_type == "approval" and pending.amount is not None:
            lines.append(f"₹{pending.amount} ops approval mein hai.")
            statuses.add("awaiting_approval")
        elif pending is not None:
            lines.append("Aapka case ops team ke paas review mein hai.")
            statuses.add("escalated")
        if not lines:
            resolved = session.scalar(
                select(OpsItem)
                .where(OpsItem.rider_id == rider_id, OpsItem.status.in_({"rejected", "approved"}))
                .order_by(OpsItem.resolved_at.desc())
                .limit(1)
            )
            if resolved is not None and resolved.status == "rejected":
                note = f": {resolved.reviewer_note}" if resolved.reviewer_note else "."
                if resolved.item_type == "approval" and resolved.amount is not None:
                    lines.append(
                        f"₹{resolved.amount} ka claim ops team ne review karke decline kiya{note}"
                    )
                else:
                    lines.append(f"Aapka case ops team ne review karke close kiya{note}")
            else:
                lines.append("Abhi koi pending payment ya approval nahi mila.")
        status = next(iter(statuses)) if len(statuses) == 1 else "mixed" if statuses else "none"
        return " ".join(lines), status

    def _prior_auto_payment(
        self,
        session: Session,
        rider_id: str,
        received_at: datetime,
        ledger: list[dict[str, Any]],
    ) -> bool:
        action_day = received_at.astimezone(IST).date().isoformat()
        intents = list(
            session.scalars(
                select(PayoutIntent).where(
                    PayoutIntent.rider_id == rider_id,
                    PayoutIntent.status.in_(
                        {"queued", "processing", "retry", "ambiguous", "confirmed"}
                    ),
                )
            )
        )
        if any(f"day:{action_day}" in intent.provider_reference for intent in intents):
            return True
        return any(f"day:{action_day}" in str(item.get("reference", "")) for item in ledger)

    def _conversation_memory(self, session: Session, conversation_id: str) -> dict[str, Any]:
        disputes = list(
            session.scalars(
                select(Dispute).where(
                    Dispute.conversation_id == conversation_id,
                    Dispute.state.in_(OPEN_DISPUTE_STATES),
                )
            )
        )
        return {
            "open_disputes": [
                {
                    "dispute_id": item.dispute_id,
                    "type": item.dispute_type,
                    "service_date_ist": (
                        item.service_date_ist.isoformat() if item.service_date_ist else None
                    ),
                    "state": item.state,
                }
                for item in disputes
            ]
        }

    @staticmethod
    def _set_conversation_state(conversation: Conversation, outcomes: list[Outcome]) -> None:
        if not outcomes:
            return
        if any(item.escalation_pending for item in outcomes):
            conversation.state = "escalated"
        elif any(item.approval_pending for item in outcomes):
            conversation.state = "awaiting_approval"
        elif any(item.action_state == "queued" for item in outcomes):
            conversation.state = "payout_processing"
        elif all(item.provider_confirmed for item in outcomes):
            conversation.state = "resolved_paid"
        elif all(item.plan.outcome == "explain_no_shortfall" for item in outcomes):
            conversation.state = "resolved_explained"

    def _model_failure(self, envelope: MessageEnvelope, conversation_id: str) -> MessageResult:
        reply = "Details samajhne mein problem hui. Date ya trip ID share karein."
        with self.database.session() as session, session.begin():
            message = session.get(Message, envelope.message_id)
            conversation = session.get(Conversation, conversation_id)
            assert message is not None and conversation is not None
            message.status = "completed"
            message.reply = reply
            message.processed_at = datetime.now(UTC)
            conversation.state = "collecting_details"
            add_trace_event(
                session,
                rider_id=envelope.rider_id,
                conversation_id=conversation_id,
                message_id=envelope.message_id,
                event_type="error",
                name="model_unavailable",
                input_payload=None,
                output_payload={"fallback": "clarification"},
            )
        return MessageResult(reply, envelope.message_id, conversation_id, "collecting_details")


def _lock_rider(session: Session, rider_id: str) -> None:
    lock_rider(session, rider_id)


def _message_age(message: Message) -> timedelta:
    created = message.created_at
    if created is None:
        return timedelta(0)
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return datetime.now(UTC) - created


def _complaint_id(message_id: str, index: int) -> str:
    digest = hashlib.sha256(f"{message_id}:{index}".encode()).hexdigest()[:32]
    return f"cmp_{digest}"


def _grouping_key_for(
    rider_id: str,
    service_date: Any,
    complaint_type: str,
    trip_ids: tuple[str, ...] | list[str] = (),
) -> str:
    if service_date:
        date_token = service_date.isoformat()
    elif trip_ids:
        date_token = "trips:" + ",".join(sorted(set(trip_ids)))
    else:
        date_token = "unknown"
    return "|".join((rider_id, date_token, complaint_type))


def _grouping_key(envelope: MessageEnvelope, complaint: ExtractedComplaint) -> str:
    """D-007: rider + IST service date + policy cause identify a logical dispute.

    The vendor message id is deliberately *not* part of the key — a re-sent or rephrased
    complaint must attach to the existing dispute instead of opening a parallel one.
    """
    if complaint.target_dispute_id:
        return f"follow-up:{complaint.target_dispute_id}"
    return _grouping_key_for(
        envelope.rider_id,
        complaint.service_date_ist,
        complaint.complaint_type,
        complaint.trip_ids,
    )


def _owned_trip_service_date(
    session: Session, rider_id: str, trip_ids: tuple[str, ...]
) -> Any:
    if not trip_ids:
        return None
    unique_trip_ids = set(trip_ids)
    owned_trips = list(
        session.execute(
            select(NormalizedTrip.trip_id, NormalizedTrip.service_date_ist).where(
                NormalizedTrip.rider_id == rider_id,
                NormalizedTrip.trip_id.in_(unique_trip_ids),
            )
        )
    )
    if {row.trip_id for row in owned_trips} != unique_trip_ids:
        return None
    dates = {row.service_date_ist for row in owned_trips}
    return next(iter(dates)) if len(dates) == 1 else None


def _dispute_evidence_summary(
    session: Session, dispute: Dispute, latest: Decision | None
) -> tuple[str | None, tuple[str, ...]]:
    effective_date = None
    trip_ids: list[str] = []
    if latest is not None:
        for evidence_id in latest.evidence_ids or []:
            evidence = session.get(Evidence, evidence_id)
            if evidence is not None and isinstance(evidence.payload, dict):
                for trip_id in evidence.payload.get("trip_ids") or []:
                    if str(trip_id) not in trip_ids:
                        trip_ids.append(str(trip_id))
                if evidence.payload.get("service_date_ist"):
                    effective_date = str(evidence.payload["service_date_ist"])
                break
    if effective_date is None and dispute.service_date_ist is not None:
        effective_date = dispute.service_date_ist.isoformat()
    for complaint_trips in session.scalars(
        select(Complaint.trip_ids).where(Complaint.dispute_id == dispute.dispute_id)
    ):
        for trip_id in complaint_trips or []:
            if str(trip_id) not in trip_ids:
                trip_ids.append(str(trip_id))
    return effective_date, tuple(trip_ids)


def _can_attach_follow_up(
    session: Session,
    rider_id: str,
    dispute: Dispute,
    extracted: ExtractedComplaint,
) -> bool:
    if extracted.complaint_type not in {"unknown", "record_disagreement"} and (
        dispute.dispute_type not in {"unknown", extracted.complaint_type}
    ):
        return False
    latest = session.scalar(
        select(Decision)
        .where(Decision.dispute_id == dispute.dispute_id)
        .order_by(Decision.decision_version.desc())
        .limit(1)
    )
    prior_date_str, prior_trips = _dispute_evidence_summary(session, dispute, latest)
    complaint_date = (
        _owned_trip_service_date(session, rider_id, extracted.trip_ids)
        or extracted.service_date_ist
    )
    if (
        complaint_date is not None
        and prior_date_str is not None
        and complaint_date.isoformat() != prior_date_str
    ):
        return False
    if extracted.trip_ids:
        if prior_trips:
            if set(extracted.trip_ids).isdisjoint(prior_trips):
                return False
        elif dispute.state not in {"draft", "needs_clarification"}:
            return False
    return True


def _escalation_line(plan: DecisionPlan, evaluation: DisputeEvaluation) -> str:
    category = plan.escalation_category or ""
    if category == "ownership":
        return "Is trip ko aapke account se verify nahi kar paaya; ops team check karegi."
    if category == "distance":
        return (
            "Export ka recorded distance check hua, lekin actual route distance source se "
            "prove nahi ho sakta; ops team review karegi."
        )
    if category == "policy_exception":
        return (
            "Recorded rider cancellation par policy penalty hai; exception ke liye case ops "
            "team ke paas review mein hai."
        )
    if category == "record_disagreement":
        if evaluation.complaint_type == "daily_incentive":
            count = evaluation.completed_trip_count or 0
            return (
                f"Dobara check kiya: record mein {count} completed trips hain (incentive ke liye "
                "12 chahiye). Aapka disagreement ops team ko review ke liye diya hai."
            )
        return (
            "Dobara check kiya, record wahi hai; aapka disagreement ops team ko review ke "
            "liye diya hai."
        )
    if category == "data_quality":
        return "Is trip ke source records mein conflict hai; ops team verify karegi."
    return "Is complaint ko ops investigation ke liye refer kiya hai."


def _disagreement_reason(evaluation: DisputeEvaluation) -> str:
    if evaluation.complaint_type == "daily_incentive":
        return (
            f"Rider disputes the recorded count: {evaluation.completed_trip_count or 0} completed "
            f"trips on {evaluation.service_date_ist}; incentive needs 12."
        )
    trips = ", ".join(evaluation.trip_ids) or "the checked trips"
    return (
        f"Rider disputes the verified result for {trips}: expected {evaluation.expected_total}, "
        f"actual {evaluation.actual_total}, no shortfall."
    )


def _evaluation_payload(evaluation: DisputeEvaluation) -> dict[str, Any]:
    return {
        "complaint_type": evaluation.complaint_type,
        "rider_id": evaluation.rider_id,
        "service_date_ist": (
            evaluation.service_date_ist.isoformat() if evaluation.service_date_ist else None
        ),
        "trip_ids": list(evaluation.trip_ids),
        "ownership": evaluation.ownership,
        "policy_window": evaluation.policy_window,
        "expected_components": [asdict(item) for item in evaluation.expected_components],
        "actual_components": [asdict(item) for item in evaluation.actual_components],
        "expected_total": evaluation.expected_total,
        "actual_total": evaluation.actual_total,
        "verified_shortfall": evaluation.verified_shortfall,
        "completed_trip_count": evaluation.completed_trip_count,
        "evidence_complete": evaluation.evidence_complete,
        "source_ambiguous": evaluation.source_ambiguous,
        "reason_codes": list(evaluation.reason_codes),
        "data_quality_flags": list(evaluation.data_quality_flags),
    }


def _find_ledger_payout(
    ledger: list[dict[str, Any]], rider_id: str, amount: int, reference: str
) -> dict[str, Any] | None:
    return next(
        (
            item
            for item in ledger
            if item.get("rider_id") == rider_id
            and item.get("amount") == amount
            and item.get("reference") == reference
            and item.get("status") == "processed"
        ),
        None,
    )
