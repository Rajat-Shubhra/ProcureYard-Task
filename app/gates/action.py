from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.gates.reports import GateReport, RuleResult

ActionKind = Literal[
    "none",
    "reply_only",
    "create_approval",
    "create_escalation",
    "submit_payout",
]


@dataclass(frozen=True, slots=True)
class ActionProposal:
    kind: ActionKind
    origin: Literal["deterministic_policy_engine", "human_ops", "model"]
    envelope_rider_id: str
    dispute_rider_id: str
    ownership: Literal["verified", "mismatch", "unknown"]
    policy_window: Literal["eligible", "expired", "future", "unknown"]
    evidence_complete: bool
    source_ambiguous: bool
    verified_shortfall: int | None
    proposed_amount: int | None
    prior_auto_payment_today: bool
    payswift_reconciled: bool
    idempotency_fingerprint_matches: bool
    escalation_category: str | None = None
    escalation_reason: str | None = None
    human_approval_present: bool = False


class ActionGate:
    checker_version = "action-gate-v1"

    def check(self, proposal: ActionProposal) -> GateReport:
        monetary = proposal.kind in {"submit_payout", "create_approval"}
        escalation = proposal.kind == "create_escalation"
        results = [
            _required(
                "action.non_model_authority",
                proposal.origin != "model",
                "Action origin is deterministic runtime or human ops.",
                "Model output cannot authorize actions.",
            ),
            _required(
                "identity.envelope_matches_dispute",
                proposal.envelope_rider_id == proposal.dispute_rider_id,
                "Dispute is bound to the envelope rider.",
                "Envelope and dispute rider do not match.",
            ),
        ]
        if monetary:
            results.extend(
                [
                    _required(
                        "identity.ownership_verified",
                        proposal.ownership == "verified",
                        "Trip ownership is verified.",
                        "Ownership is not verified.",
                    ),
                    _required(
                        "policy.window_eligible",
                        proposal.policy_window == "eligible",
                        "Dispute is inside the eligible IST window.",
                        "Dispute is outside or lacks an eligible policy window.",
                    ),
                    _required(
                        "evidence.complete_and_unambiguous",
                        proposal.evidence_complete and not proposal.source_ambiguous,
                        "Required evidence is complete and unambiguous.",
                        "Evidence is missing, conflicting, or ambiguous.",
                    ),
                    _required(
                        "money.positive_verified_shortfall",
                        proposal.verified_shortfall is not None and proposal.verified_shortfall > 0,
                        "A positive shortfall was deterministically verified.",
                        "No positive verified shortfall exists.",
                    ),
                    _required(
                        "money.proposal_matches_shortfall",
                        proposal.proposed_amount == proposal.verified_shortfall,
                        "Proposed amount exactly matches the verified shortfall.",
                        "Proposed amount differs from the verified shortfall.",
                    ),
                    _required(
                        "provider.amount_bounds",
                        proposal.proposed_amount is not None
                        and 1 <= proposal.proposed_amount <= 10000,
                        "Amount is inside PaySwift's whole-rupee bounds.",
                        "Amount is outside PaySwift's 1–10,000 rupee bounds.",
                    ),
                ]
            )
        else:
            results.extend(
                _not_applicable(rule_id)
                for rule_id in (
                    "identity.ownership_verified",
                    "policy.window_eligible",
                    "evidence.complete_and_unambiguous",
                    "money.positive_verified_shortfall",
                    "money.proposal_matches_shortfall",
                    "provider.amount_bounds",
                )
            )

        if proposal.kind == "submit_payout":
            if proposal.origin == "human_ops":
                results.extend(
                    (
                        _not_applicable("money.auto_limit"),
                        _not_applicable("money.daily_auto_limit"),
                        _required(
                            "human.approval_present",
                            proposal.human_approval_present,
                            "A persisted human approval authorizes the guardrail exception.",
                            "Human payout execution lacks an approved ops item.",
                        ),
                    )
                )
            else:
                results.extend(
                    [
                        _required(
                            "money.auto_limit",
                            proposal.proposed_amount is not None
                            and proposal.proposed_amount <= 200,
                            "Amount is within the ₹200 automatic-payment limit.",
                            "Amount exceeds the automatic-payment limit.",
                        ),
                        _required(
                            "money.daily_auto_limit",
                            not proposal.prior_auto_payment_today,
                            "No earlier auto-payment exists for the rider today.",
                            "The rider already has an auto-payment today.",
                        ),
                        _not_applicable("human.approval_present"),
                    ]
                )
            results.extend(
                [
                    _required(
                        "provider.reconciled",
                        proposal.payswift_reconciled,
                        "PaySwift ledger was reconciled before action.",
                        "PaySwift ledger has not been reconciled.",
                    ),
                    _required(
                        "provider.idempotency_fingerprint",
                        proposal.idempotency_fingerprint_matches,
                        "Idempotency key is bound to the same immutable request.",
                        "Idempotency fingerprint conflicts with an existing request.",
                    ),
                ]
            )
        elif proposal.kind == "create_approval":
            approval_needed = proposal.proposed_amount is not None and (
                proposal.proposed_amount > 200 or proposal.prior_auto_payment_today
            )
            results.append(
                _required(
                    "approval.guardrail_trigger",
                    approval_needed,
                    "A known amount is blocked by an approval guardrail.",
                    "Approval was requested without an amount/daily guardrail trigger.",
                )
            )
            results.extend(
                _not_applicable(rule_id)
                for rule_id in (
                    "provider.reconciled",
                    "provider.idempotency_fingerprint",
                )
            )
        else:
            results.extend(
                _not_applicable(rule_id)
                for rule_id in (
                    "money.auto_limit",
                    "money.daily_auto_limit",
                    "human.approval_present",
                    "approval.guardrail_trigger",
                    "provider.reconciled",
                    "provider.idempotency_fingerprint",
                )
            )

        if escalation:
            actual_reason = bool(
                proposal.source_ambiguous
                or proposal.ownership in {"mismatch", "unknown"}
                or proposal.policy_window == "unknown"
                or proposal.escalation_category
            )
            results.extend(
                [
                    _required(
                        "escalation.amount_is_null",
                        proposal.proposed_amount is None,
                        "Escalation carries no guessed amount.",
                        "Escalation cannot carry an unverified amount.",
                    ),
                    _required(
                        "escalation.necessary",
                        actual_reason and bool(proposal.escalation_reason),
                        "A concrete unsafe/unclear condition requires human review.",
                        "No necessary escalation condition or reason was supplied.",
                    ),
                ]
            )
        else:
            results.extend(
                _not_applicable(rule_id)
                for rule_id in ("escalation.amount_is_null", "escalation.necessary")
            )

        blocking = tuple(item.rule_id for item in results if item.status == "fail")
        return GateReport(
            gate="action",
            checker_version=self.checker_version,
            results=tuple(results),
            blocking_reasons=blocking,
        )


def _required(rule_id: str, passed: bool, passed_detail: str, failed_detail: str) -> RuleResult:
    return RuleResult(
        rule_id=rule_id,
        status="pass" if passed else "fail",
        detail=passed_detail if passed else failed_detail,
    )


def _not_applicable(rule_id: str) -> RuleResult:
    return RuleResult(rule_id=rule_id, status="not_applicable", detail="Not applicable.")
