from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import select

from app.config import Settings
from app.db import Database, lock_rider
from app.domain.coverage import LIVE_INTENT_STATES, logical_reference, summarize_commitments
from app.gates.action import ActionGate, ActionProposal
from app.gates.reports import persist_gate_report
from app.models import Decision, Dispute, Evidence, OpsItem, PayoutIntent
from app.providers.payswift import (
    PaySwiftClient,
    payout_request_fingerprint,
    requeue_failed_intent,
)
from app.trace import add_trace_event

PROVIDER_FAILURE_PREFIX = "provider-failure:"


class OpsConflictError(RuntimeError):
    pass


class OpsNotFoundError(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class OpsResult:
    item_id: str
    status: str
    payout_intent_id: str | None = None


class OpsService:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        payswift: PaySwiftClient,
    ) -> None:
        self.database = database
        self.settings = settings
        self.payswift = payswift
        self.gate = ActionGate()

    def approve(self, item_id: str, reviewer: str, note: str | None) -> OpsResult:
        with self.database.session() as session:
            item = session.get(OpsItem, item_id)
            if item is None:
                raise OpsNotFoundError(item_id)
            if item.status == "approved":
                intent = session.scalar(
                    select(PayoutIntent).where(PayoutIntent.decision_id == item.decision_id)
                )
                return OpsResult(item_id, "approved", intent.intent_id if intent else None)
            if item.status == "closed_stale":
                return OpsResult(item_id, "closed_stale", _live_intent_id(session, item))
            if item.status != "pending":
                raise OpsConflictError("Ops item already has a different terminal decision")
            if item.item_type != "approval" or item.amount is None:
                raise OpsConflictError(
                    "Escalations require a new verified decision before approval"
                )
            rider_id = item.rider_id
        try:
            ledger = self.payswift.list_payouts(rider_id)
            ledger_ok = True
        except (httpx.HTTPError, ValueError):
            ledger = []
            ledger_ok = False

        with self.database.session() as session, session.begin():
            lock_rider(session, rider_id)
            statement = select(OpsItem).where(OpsItem.item_id == item_id)
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                statement = statement.with_for_update()
            item = session.scalar(statement)
            assert item is not None
            if item.status == "approved":
                intent = session.scalar(
                    select(PayoutIntent).where(PayoutIntent.decision_id == item.decision_id)
                )
                return OpsResult(item_id, "approved", intent.intent_id if intent else None)
            if item.status == "closed_stale":
                return OpsResult(item_id, "closed_stale", _live_intent_id(session, item))
            if item.status != "pending" or item.item_type != "approval" or item.amount is None:
                raise OpsConflictError("Ops item is no longer approvable")
            decision = session.get(Decision, item.decision_id)
            dispute = session.get(Dispute, item.dispute_id)
            if decision is None or dispute is None:
                raise OpsConflictError("Approval evidence is no longer available")
            # Revalidate against the evidence that produced *this* approval, not whatever
            # evidence a later turn happened to write for the dispute.
            evidence = _evidence_for_decision(session, decision)
            if evidence is None:
                raise OpsConflictError("Approval has no reproducible evidence")
            payload = evidence.payload
            trip_ids = tuple(str(value) for value in payload.get("trip_ids") or [])
            service_date = _parse_date(payload.get("service_date_ist"))
            logical = logical_reference(
                item.rider_id, str(payload.get("complaint_type")), service_date, trip_ids
            )
            reference = f"{logical}:approval:{item.item_id[-12:]}"
            idempotency_key = f"payout:{dispute.dispute_id}:{decision.decision_version}"
            fingerprint = payout_request_fingerprint(item.rider_id, item.amount, reference)
            existing = session.scalar(
                select(PayoutIntent).where(PayoutIntent.idempotency_key == idempotency_key)
            )
            # Gate B rule 9 for humans: anything already queued/paid for this evidence — on
            # this dispute, on another dispute, or only in the PaySwift ledger — closes the
            # item as stale instead of paying again.
            commitments = summarize_commitments(
                session,
                rider_id=item.rider_id,
                dispute_id=dispute.dispute_id,
                complaint_type=str(payload.get("complaint_type")),
                service_date_ist=service_date,
                trip_ids=trip_ids,
                ledger=ledger,
            )
            already_covered = any(
                commitment.kind in {"intent", "ledger"} for commitment in commitments.commitments
            )
            report = self.gate.check(
                ActionProposal(
                    kind="submit_payout",
                    origin="human_ops",
                    envelope_rider_id=item.rider_id,
                    dispute_rider_id=dispute.rider_id,
                    ownership=payload["ownership"],
                    policy_window=payload["policy_window"],
                    evidence_complete=bool(payload.get("evidence_complete")),
                    source_ambiguous=bool(payload.get("source_ambiguous")),
                    verified_shortfall=payload.get("verified_shortfall"),
                    proposed_amount=item.amount,
                    prior_auto_payment_today=True,
                    payswift_reconciled=ledger_ok,
                    idempotency_fingerprint_matches=(
                        existing is None or existing.request_fingerprint == fingerprint
                    ),
                    human_approval_present=True,
                    already_covered=already_covered,
                )
            )
            persist_gate_report(
                session,
                report,
                dispute_id=dispute.dispute_id,
                decision_id=decision.decision_id,
            )
            if not report.passed:
                # API_CONTRACT: a stale/already-paid item closes without another payout.
                item.status = "closed_stale"
                item.reviewer = reviewer
                item.reviewer_note = (
                    f"{note + ' | ' if note else ''}closed without payout: "
                    + ", ".join(report.blocking_reasons)
                )[:1000]
                item.resolved_at = datetime.now(UTC)
                live = session.scalar(
                    select(PayoutIntent).where(
                        PayoutIntent.dispute_id == dispute.dispute_id,
                        PayoutIntent.status.in_(LIVE_INTENT_STATES),
                    )
                )
                if live is not None:
                    dispute.state = "paid" if live.status == "confirmed" else "payout_processing"
                elif already_covered:
                    dispute.state = "paid"
                add_trace_event(
                    session,
                    rider_id=item.rider_id,
                    dispute_id=dispute.dispute_id,
                    event_type="decision",
                    name="ops_approval_stale",
                    input_payload={"item_id": item_id, "reviewer": reviewer},
                    output_payload={
                        "status": "closed_stale",
                        "blocking_reasons": list(report.blocking_reasons),
                        "committed_amount": commitments.committed_amount,
                    },
                )
                return OpsResult(item_id, "closed_stale", live.intent_id if live else None)
            matched = _find_ledger(ledger, item.rider_id, item.amount, reference)
            intent = existing or PayoutIntent(
                intent_id=f"intent_{uuid4().hex}",
                rider_id=item.rider_id,
                dispute_id=dispute.dispute_id,
                decision_id=decision.decision_id,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                amount=item.amount,
                provider_reference=reference,
                status="confirmed" if matched else "queued",
                provider_payout_id=str(matched.get("payout_id")) if matched else None,
                confirmed_at=datetime.now(UTC) if matched else None,
            )
            if existing is None:
                session.add(intent)
            item.status = "approved"
            item.reviewer = reviewer
            item.reviewer_note = note
            item.resolved_at = datetime.now(UTC)
            dispute.state = "paid" if matched else "payout_processing"
            add_trace_event(
                session,
                rider_id=item.rider_id,
                dispute_id=dispute.dispute_id,
                event_type="decision",
                name="ops_approval",
                input_payload={"item_id": item_id, "reviewer": reviewer},
                output_payload={"status": "approved", "intent_id": intent.intent_id},
            )
            return OpsResult(item_id, "approved", intent.intent_id)

    def reject(self, item_id: str, reviewer: str, note: str | None) -> OpsResult:
        with self.database.session() as session, session.begin():
            statement = select(OpsItem).where(OpsItem.item_id == item_id)
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                statement = statement.with_for_update()
            item = session.scalar(statement)
            if item is None:
                raise OpsNotFoundError(item_id)
            lock_rider(session, item.rider_id)
            if item.status == "rejected":
                return OpsResult(item_id, "rejected")
            if item.status != "pending":
                raise OpsConflictError("Ops item already has a different terminal decision")
            item.status = "rejected"
            item.reviewer = reviewer
            item.reviewer_note = note
            item.resolved_at = datetime.now(UTC)
            dispute = session.get(Dispute, item.dispute_id)
            if dispute is not None:
                dispute.state = "rejected"
            add_trace_event(
                session,
                rider_id=item.rider_id,
                dispute_id=item.dispute_id,
                event_type="decision",
                name="ops_rejection",
                input_payload={"item_id": item_id, "reviewer": reviewer},
                output_payload={"status": "rejected", "note": note},
            )
            return OpsResult(item_id, "rejected")

    def retry_payout(self, item_id: str, reviewer: str, note: str | None) -> OpsResult:
        """Re-queue the retained payout intent behind a provider-failure escalation.

        The intent keeps its idempotency key, body fingerprint and reference, so PaySwift
        sees the same request again (or a reconcile-before-submit finds the earlier payout).
        """
        with self.database.session() as session, session.begin():
            statement = select(OpsItem).where(OpsItem.item_id == item_id)
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                statement = statement.with_for_update()
            item = session.scalar(statement)
            if item is None:
                raise OpsNotFoundError(item_id)
            lock_rider(session, item.rider_id)
            if not item.logical_key.startswith(PROVIDER_FAILURE_PREFIX):
                raise OpsConflictError("Only provider-failure escalations can retry a payout")
            intent_id = item.logical_key[len(PROVIDER_FAILURE_PREFIX) :]
            intent = session.get(PayoutIntent, intent_id)
            if intent is None:
                raise OpsConflictError("The retained payout intent no longer exists")
            if item.status == "requeued":
                return OpsResult(item_id, "requeued", intent.intent_id)
            if item.status != "pending":
                raise OpsConflictError("Ops item already has a different terminal decision")
            if intent.status in LIVE_INTENT_STATES:
                # Already moving again (or paid); nothing to retry.
                item.status = "requeued"
                item.reviewer = reviewer
                item.reviewer_note = note
                item.resolved_at = datetime.now(UTC)
                return OpsResult(item_id, "requeued", intent.intent_id)
            if not requeue_failed_intent(session, intent.intent_id, reviewer):
                raise OpsConflictError("The retained payout intent is not in a retryable state")
            item.status = "requeued"
            item.reviewer = reviewer
            item.reviewer_note = note
            item.resolved_at = datetime.now(UTC)
            return OpsResult(item_id, "requeued", intent.intent_id)


def _evidence_for_decision(session: Any, decision: Decision) -> Evidence | None:
    for evidence_id in decision.evidence_ids or []:
        evidence = session.get(Evidence, evidence_id)
        if evidence is not None:
            return evidence
    return session.scalar(
        select(Evidence)
        .where(Evidence.dispute_id == decision.dispute_id)
        .order_by(Evidence.created_at.desc())
        .limit(1)
    )


def _live_intent_id(session: Any, item: OpsItem) -> str | None:
    live = session.scalar(
        select(PayoutIntent).where(
            PayoutIntent.dispute_id == item.dispute_id,
            PayoutIntent.status.in_(LIVE_INTENT_STATES),
        )
    )
    return live.intent_id if live else None


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    return date.fromisoformat(str(value))


def _find_ledger(
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
