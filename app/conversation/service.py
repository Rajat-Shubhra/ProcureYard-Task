from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, select, text
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
from app.db import Database
from app.domain.decision import DecisionPlan, plan_decision
from app.domain.payout import DisputeEvaluation, PayoutPolicyEngine
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
    OpsItem,
    PayoutIntent,
    Rider,
)
from app.providers.payswift import PaySwiftClient, payout_request_fingerprint
from app.trace import add_trace_event

IST = ZoneInfo("Asia/Kolkata")
OPEN_DISPUTE_STATES = {
    "draft",
    "needs_clarification",
    "verifying",
    "verified_owed",
    "awaiting_approval",
    "escalated",
    "payout_processing",
}


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
        context = self._load_context(envelope, conversation_id)
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="message-io") as executor:
            interpretation_future = executor.submit(self.provider.interpret, context)
            ledger_future = executor.submit(self.payswift.list_payouts, envelope.rider_id)
            try:
                interpretation = interpretation_future.result()
            except (InterpretationProviderError, httpx.HTTPError, ValueError):
                if not self.settings.model_fallback_enabled:
                    return self._model_failure(envelope, conversation_id)
                interpretation = self.fallback.interpret(context)
            try:
                ledger = ledger_future.result()
                ledger_ok = True
            except httpx.HTTPError:
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

    def _build_provider(self) -> InterpretationProvider:
        if self.settings.llm_provider == "groq":
            return GroqInterpretationProvider(self.settings)
        return HeuristicInterpretationProvider()

    def _reserve(self, envelope: MessageEnvelope) -> tuple[str, int] | MessageResult:
        try:
            return self._reserve_once(envelope)
        except IntegrityError:
            with self.database.session() as session:
                existing = session.get(Message, envelope.message_id)
                if existing is None:
                    raise
                return MessageResult(
                    reply=existing.reply or "Aapka message process ho raha hai.",
                    message_id=existing.message_id,
                    conversation_id=existing.conversation_id,
                    state=existing.status,
                    duplicate=True,
                )

    def _reserve_once(self, envelope: MessageEnvelope) -> tuple[str, int] | MessageResult:
        if not envelope.message_id.strip() or not envelope.text.strip():
            raise ValueError("message_id and text must be non-blank")
        if len(envelope.text) > self.settings.message_max_length:
            raise ValueError("message text exceeds the configured limit")
        if envelope.received_at.tzinfo is None:
            raise ValueError("received_at must include a timezone")
        with self.database.session() as session, session.begin():
            existing = session.get(Message, envelope.message_id)
            if existing is not None:
                return MessageResult(
                    reply=existing.reply or "Aapka message process ho raha hai.",
                    message_id=existing.message_id,
                    conversation_id=existing.conversation_id,
                    state=existing.status,
                    duplicate=True,
                )
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
                dispute_context.append(
                    {
                        "dispute_id": dispute.dispute_id,
                        "dispute_type": dispute.dispute_type,
                        "service_date_ist": (
                            dispute.service_date_ist.isoformat()
                            if dispute.service_date_ist
                            else None
                        ),
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
                if extracted.target_dispute_id:
                    dispute = session.get(Dispute, extracted.target_dispute_id)
                else:
                    dispute = group_to_dispute.get(grouping_key)
                if dispute is None:
                    dispute = Dispute(
                        dispute_id=f"dsp_{uuid4().hex}",
                        conversation_id=conversation.conversation_id,
                        rider_id=envelope.rider_id,
                        state="verifying" if is_accepted else "needs_clarification",
                        dispute_type=extracted.complaint_type,
                        service_date_ist=extracted.service_date_ist,
                        policy_cause=extracted.complaint_type,
                        grouping_key=grouping_key,
                    )
                    session.add(dispute)
                    session.flush()
                elif is_accepted:
                    dispute.state = "verifying"
                    dispute.dispute_type = extracted.complaint_type
                    dispute.policy_cause = extracted.complaint_type
                    if extracted.service_date_ist is not None:
                        dispute.service_date_ist = extracted.service_date_ist
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
        prior_auto = self._prior_auto_payment(
            session, envelope.rider_id, envelope.received_at, ledger
        )
        plan = plan_decision(evaluation, prior_auto_payment_today=prior_auto)
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
                payload=_evaluation_payload(evaluation),
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
        reference = f"qd:{dispute_id}:day:{action_day}"
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

    def _render_reply(
        self,
        session: Session,
        envelope: MessageEnvelope,
        result: InterpretationResult,
        gate: GateReport,
        outcomes: list[Outcome],
    ) -> tuple[str, str]:
        if (
            not result.interpretation.complaints
            and result.interpretation.requested_status != "not_applicable"
        ):
            return self._status_reply(session, envelope.rider_id)
        if any(
            reason in {"identity_or_privacy_risk", "prompt_injection"}
            for reason in gate.blocking_reasons
        ):
            return "Is request ko payout action ke liye use nahi kar sakta.", "none"
        lines: list[str] = []
        statuses: set[str] = set()
        for index, item in enumerate(outcomes, start=1):
            prefix = f"{index}. " if len(outcomes) > 1 else ""
            amount = item.plan.amount
            evaluation = item.evaluation
            trip_label = f"{', '.join(evaluation.trip_ids)}: " if evaluation.trip_ids else ""
            if item.plan.outcome == "auto_pay" and amount is not None:
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
                lines.append(f"{prefix}Is complaint ko ops investigation ke liye bheja hai.")
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
        pending = session.scalar(
            select(OpsItem)
            .where(OpsItem.rider_id == rider_id, OpsItem.status == "pending")
            .order_by(OpsItem.created_at.desc())
            .limit(1)
        )
        if pending is not None and pending.item_type == "approval":
            return f"₹{pending.amount} ops approval mein hai.", "awaiting_approval"
        intent = session.scalar(
            select(PayoutIntent)
            .where(PayoutIntent.rider_id == rider_id)
            .order_by(PayoutIntent.created_at.desc())
            .limit(1)
        )
        if intent is not None and intent.status == "confirmed":
            return f"₹{intent.amount} PaySwift par process ho gaya hai.", "paid"
        if intent is not None:
            return f"₹{intent.amount} payout processing mein hai.", "processing"
        return "Abhi koi pending payment ya approval nahi mila.", "none"

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
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:rider_id))"), {"rider_id": rider_id}
        )


def _complaint_id(message_id: str, index: int) -> str:
    digest = hashlib.sha256(f"{message_id}:{index}".encode()).hexdigest()[:32]
    return f"cmp_{digest}"


def _grouping_key(envelope: MessageEnvelope, complaint: ExtractedComplaint) -> str:
    if complaint.target_dispute_id:
        return f"follow-up:{complaint.target_dispute_id}"
    service_date = (
        complaint.service_date_ist.isoformat() if complaint.service_date_ist else "unknown"
    )
    return "|".join(
        (
            envelope.rider_id,
            service_date,
            complaint.complaint_type,
            envelope.message_id,
        )
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
