"""Deterministic "already covered" reconciliation (Gate B rule 9).

Before any monetary or ops action the runtime must know how much of a verified shortfall is
already queued, paid, awaiting approval, or rejected — in the local payout ledger *and* in
PaySwift. This module answers that question from evidence, not from message or dispute ids, so
re-sent complaints, follow-up turns, differently-grouped disputes and provider restarts cannot
move the same rupees twice.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Decision, Evidence, OpsItem, PayoutIntent

LIVE_INTENT_STATES = frozenset({"queued", "processing", "retry", "ambiguous", "confirmed"})


def logical_reference(
    rider_id: str,
    complaint_type: str,
    service_date_ist: date | None,
    trip_ids: tuple[str, ...] | list[str],
) -> str:
    """Stable PaySwift reference prefix for one logical dispute.

    It encodes rider, policy cause, IST service date and the evidence trip set — never a
    dispute id, decision version or action day — so the same shortfall always produces the
    same prefix and can be recognised in the provider ledger after local state or the
    sandbox has been reset.
    """
    digest = hashlib.sha1("|".join(sorted(set(trip_ids))).encode()).hexdigest()[:12]
    date_token = service_date_ist.isoformat() if service_date_ist else "nodate"
    return f"qd:{rider_id}:{complaint_type}:{date_token}:{digest}"


@dataclass(frozen=True, slots=True)
class Commitment:
    kind: str  # "intent" | "approval" | "escalation" | "ledger"
    status: str
    amount: int
    dispute_id: str | None
    reference: str | None = None


@dataclass(slots=True)
class CommitmentSummary:
    # Money already moving for evidence that *overlaps* the current evaluation (deducted).
    committed_amount: int = 0
    # Money already moving for the same rider, IST service date and cause family, whether or
    # not the trips overlap (feeds the cumulative ₹200 automatic-payment cap).
    related_amount: int = 0
    commitments: list[Commitment] = field(default_factory=list)
    same_dispute_intent: PayoutIntent | None = None
    same_dispute_pending: OpsItem | None = None
    rejected_item: OpsItem | None = None
    known_trip_ids: frozenset[str] = frozenset()

    @property
    def paid_amount(self) -> int:
        return sum(
            item.amount
            for item in self.commitments
            if item.kind in {"intent", "ledger"} and item.status in {"confirmed", "processed"}
        )

    @property
    def processing_amount(self) -> int:
        return sum(
            item.amount
            for item in self.commitments
            if item.kind == "intent" and item.status != "confirmed"
        )

    @property
    def approval_amount(self) -> int:
        return sum(item.amount for item in self.commitments if item.kind == "approval")


def summarize_commitments(
    session: Session,
    *,
    rider_id: str,
    dispute_id: str | None,
    complaint_type: str,
    service_date_ist: date | None,
    trip_ids: tuple[str, ...] | list[str],
    ledger: list[dict[str, Any]],
) -> CommitmentSummary:
    """Collect every prior commitment that overlaps the evidence of the current evaluation.

    Overlap rules (deterministic, versioned with the policy engine):
    * trip-based causes overlap when their evidence trip sets intersect, regardless of the
      wording of the cause (surge vs trip_payment describe the same per-trip shortfall);
    * daily-incentive claims overlap only with daily-incentive evidence for the same IST date;
    * anything recorded against the same dispute id overlaps by definition.
    """
    summary = CommitmentSummary()
    current_trips = frozenset(trip_ids)
    is_incentive = complaint_type == "daily_incentive"
    counted_decisions: set[str] = set()
    local_references: set[str] = set()

    intents = list(
        session.scalars(
            select(PayoutIntent).where(
                PayoutIntent.rider_id == rider_id,
                PayoutIntent.status.in_(LIVE_INTENT_STATES),
            )
        )
    )
    items = list(
        session.scalars(
            select(OpsItem).where(
                OpsItem.rider_id == rider_id,
                OpsItem.status.in_({"pending", "approved", "rejected"}),
            )
        )
    )

    for intent in intents:
        local_references.add(intent.provider_reference)
        evidence = _evidence_for_decision(session, intent.decision_id)
        if intent.dispute_id == dispute_id:
            summary.known_trip_ids |= _evidence_trips(evidence)
            if summary.same_dispute_intent is None or intent.status == "confirmed":
                summary.same_dispute_intent = intent
        if _related(evidence, service_date_ist, is_incentive):
            summary.related_amount += intent.amount
        if _overlaps(evidence, complaint_type, service_date_ist, current_trips, is_incentive):
            counted_decisions.add(intent.decision_id)
            summary.commitments.append(
                Commitment(
                    "intent",
                    intent.status,
                    intent.amount,
                    intent.dispute_id,
                    intent.provider_reference,
                )
            )

    for item in items:
        evidence = _evidence_for_decision(session, item.decision_id)
        same_dispute = item.dispute_id == dispute_id
        if same_dispute:
            summary.known_trip_ids |= _evidence_trips(evidence)
            if item.status == "pending" and summary.same_dispute_pending is None:
                summary.same_dispute_pending = item
        overlapping = _overlaps(
            evidence, complaint_type, service_date_ist, current_trips, is_incentive
        )
        if item.item_type != "approval" or item.amount is None:
            if same_dispute and item.status == "rejected" and summary.rejected_item is None:
                summary.rejected_item = item
            continue
        if item.status == "rejected":
            if overlapping and summary.rejected_item is None:
                summary.rejected_item = item
            continue
        if item.status == "approved" and item.decision_id in counted_decisions:
            continue  # the approval already turned into a live intent counted above
        if _related(evidence, service_date_ist, is_incentive) and not (
            item.status == "approved" and any(i.decision_id == item.decision_id for i in intents)
        ):
            summary.related_amount += item.amount
        if overlapping:
            summary.commitments.append(
                Commitment("approval", item.status, item.amount, item.dispute_id)
            )

    # Decisions of this dispute that produced no money action still define its known evidence.
    if dispute_id is not None:
        for decision in session.scalars(select(Decision).where(Decision.dispute_id == dispute_id)):
            summary.known_trip_ids |= _evidence_trips(
                _evidence_for_decision(session, decision.decision_id)
            )

    # PaySwift is the authority for money that already moved: recognise our own logical
    # reference even when the local intent row is gone (database reset) or was never written.
    prefix = logical_reference(rider_id, complaint_type, service_date_ist, trip_ids)
    for row in ledger:
        reference = str(row.get("reference") or "")
        if (
            row.get("rider_id") == rider_id
            and row.get("status") == "processed"
            and reference.startswith(prefix)
            and reference not in local_references
        ):
            amount = int(row.get("amount") or 0)
            summary.commitments.append(Commitment("ledger", "processed", amount, None, reference))

    summary.committed_amount = sum(item.amount for item in summary.commitments)
    return summary


def _evidence_for_decision(session: Session, decision_id: str) -> dict[str, Any]:
    decision = session.get(Decision, decision_id)
    if decision is None:
        return {}
    for evidence_id in decision.evidence_ids or []:
        evidence = session.get(Evidence, evidence_id)
        if evidence is not None and evidence.kind == "payout_reconciliation":
            return evidence.payload
    return {}


def _evidence_trips(payload: dict[str, Any]) -> frozenset[str]:
    if payload.get("complaint_type") == "daily_incentive":
        return frozenset()
    return frozenset(str(value) for value in payload.get("trip_ids") or [])


def _related(payload: dict[str, Any], service_date_ist: date | None, is_incentive: bool) -> bool:
    """Same rider (caller-scoped), same IST service date, same cause family."""
    if not payload or service_date_ist is None:
        return False
    prior_incentive = payload.get("complaint_type") == "daily_incentive"
    if prior_incentive != is_incentive:
        return False
    return payload.get("service_date_ist") == service_date_ist.isoformat()


def _overlaps(
    payload: dict[str, Any],
    complaint_type: str,
    service_date_ist: date | None,
    current_trips: frozenset[str],
    is_incentive: bool,
) -> bool:
    if not payload:
        return False
    prior_incentive = payload.get("complaint_type") == "daily_incentive"
    if is_incentive or prior_incentive:
        if not (is_incentive and prior_incentive):
            return False
        prior_date = payload.get("service_date_ist")
        return service_date_ist is not None and prior_date == service_date_ist.isoformat()
    prior_trips = frozenset(str(value) for value in payload.get("trip_ids") or [])
    return bool(prior_trips & current_trips)
