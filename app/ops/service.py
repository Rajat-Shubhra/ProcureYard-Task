from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import select

from app.config import Settings
from app.db import Database
from app.gates.action import ActionGate, ActionProposal
from app.gates.reports import persist_gate_report
from app.models import Decision, Dispute, Evidence, OpsItem, PayoutIntent
from app.providers.payswift import PaySwiftClient, payout_request_fingerprint
from app.trace import add_trace_event


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
        except httpx.HTTPError:
            ledger = []
            ledger_ok = False

        with self.database.session() as session, session.begin():
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
            if item.status != "pending" or item.item_type != "approval" or item.amount is None:
                raise OpsConflictError("Ops item is no longer approvable")
            decision = session.get(Decision, item.decision_id)
            dispute = session.get(Dispute, item.dispute_id)
            if decision is None or dispute is None:
                raise OpsConflictError("Approval evidence is no longer available")
            evidence = session.scalar(
                select(Evidence)
                .where(Evidence.dispute_id == dispute.dispute_id)
                .order_by(Evidence.created_at.desc())
                .limit(1)
            )
            if evidence is None:
                raise OpsConflictError("Approval has no reproducible evidence")
            payload = evidence.payload
            reference = f"qd:{dispute.dispute_id}:decision:{decision.decision_version}"
            idempotency_key = f"payout:{dispute.dispute_id}:{decision.decision_version}"
            fingerprint = payout_request_fingerprint(item.rider_id, item.amount, reference)
            existing = session.scalar(
                select(PayoutIntent).where(PayoutIntent.idempotency_key == idempotency_key)
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
                )
            )
            persist_gate_report(
                session,
                report,
                dispute_id=dispute.dispute_id,
                decision_id=decision.decision_id,
            )
            if not report.passed:
                raise OpsConflictError(
                    "Approval failed current deterministic checks: "
                    + ", ".join(report.blocking_reasons)
                )
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
