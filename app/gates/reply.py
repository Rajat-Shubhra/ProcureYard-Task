from __future__ import annotations

import re
from dataclasses import dataclass

from app.gates.reports import GateReport, RuleResult

TRIP_RE = re.compile(r"\bT\d+\b", re.IGNORECASE)
RIDER_RE = re.compile(r"\bR\d{3}\b", re.IGNORECASE)
RUPEE_RE = re.compile(r"(?:₹\s*|\bRs\.?\s*)(\d+)", re.IGNORECASE)
PAID_RE = re.compile(
    r"\b(paid|processed|sent)\b|bhej\s+diya|process(?:ed)?\s+ho\s+gaya",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ReplyProposal:
    reply: str
    envelope_rider_id: str
    allowed_trip_ids: frozenset[str]
    supported_amounts: frozenset[int]
    claims_payment_status: str
    provider_confirmed: bool
    approval_pending: bool
    escalation_pending: bool


class ReplyGate:
    checker_version = "reply-gate-v1"

    def check(self, proposal: ReplyProposal) -> GateReport:
        rider_ids = {value.upper() for value in RIDER_RE.findall(proposal.reply)}
        trip_ids = {value.upper() for value in TRIP_RE.findall(proposal.reply)}
        amounts = {int(value) for value in RUPEE_RE.findall(proposal.reply)}
        paid_language = bool(PAID_RE.search(proposal.reply))
        results = (
            _check("reply.non_empty", bool(proposal.reply.strip()), "Reply is non-empty."),
            _check(
                "reply.identity_privacy",
                not rider_ids or rider_ids <= {proposal.envelope_rider_id},
                "Reply contains no other-rider identifier.",
            ),
            _check(
                "reply.trip_ids_supported",
                trip_ids <= proposal.allowed_trip_ids,
                "Every mentioned trip is supported by this rider's evidence.",
            ),
            _check(
                "reply.amounts_supported",
                amounts <= proposal.supported_amounts,
                "Every explicit rupee amount is supported by a verified outcome.",
            ),
            _check(
                "reply.payment_claim",
                not (proposal.claims_payment_status == "paid" or paid_language)
                or proposal.provider_confirmed,
                "Paid/processed wording is backed by the PaySwift ledger.",
            ),
            _check(
                "reply.approval_claim",
                proposal.claims_payment_status != "awaiting_approval" or proposal.approval_pending,
                "Approval wording is backed by a pending approval item.",
            ),
            _check(
                "reply.escalation_claim",
                proposal.claims_payment_status != "escalated" or proposal.escalation_pending,
                "Escalation wording is backed by a pending escalation item.",
            ),
        )
        blocking = tuple(item.rule_id for item in results if item.status == "fail")
        return GateReport(
            gate="reply",
            checker_version=self.checker_version,
            results=results,
            blocking_reasons=blocking,
        )


def _check(rule_id: str, passed: bool, passed_detail: str) -> RuleResult:
    return RuleResult(
        rule_id=rule_id,
        status="pass" if passed else "fail",
        detail=passed_detail if passed else f"Blocked by {rule_id}.",
    )
