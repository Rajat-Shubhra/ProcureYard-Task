from __future__ import annotations

from dataclasses import dataclass

from app.domain.payout import DisputeEvaluation


@dataclass(frozen=True, slots=True)
class DecisionPlan:
    outcome: str
    action_kind: str
    amount: int | None
    reason_codes: tuple[str, ...]
    escalation_category: str | None = None
    escalation_reason: str | None = None


def plan_decision(
    evaluation: DisputeEvaluation,
    *,
    prior_auto_payment_today: bool,
) -> DecisionPlan:
    if evaluation.ownership == "mismatch":
        return DecisionPlan(
            "explain_no_shortfall",
            "reply_only",
            None,
            ("ownership_mismatch",),
        )
    if evaluation.policy_window in {"expired", "future"}:
        return DecisionPlan(
            "explain_no_shortfall",
            "reply_only",
            None,
            ("outside_policy_window",),
        )
    if evaluation.complaint_type in {
        "distance",
        "record_disagreement",
        "policy_exception",
    }:
        return DecisionPlan(
            "explain_no_shortfall",
            "reply_only",
            None,
            evaluation.reason_codes,
        )
    if not evaluation.evidence_complete or evaluation.verified_shortfall is None:
        return DecisionPlan(
            "ask_clarification",
            "reply_only",
            None,
            ("missing_grounding",),
        )
    if evaluation.source_ambiguous:
        return DecisionPlan(
            "escalate",
            "create_escalation",
            None,
            ("source_data_ambiguous",),
            escalation_category="data_quality",
            escalation_reason="Source evidence has no single safe interpretation.",
        )
    if evaluation.verified_shortfall <= 0:
        return DecisionPlan(
            "explain_no_shortfall",
            "reply_only",
            None,
            ("no_shortfall",),
        )
    amount = evaluation.verified_shortfall
    if amount > 200:
        return DecisionPlan(
            "seek_approval",
            "create_approval",
            amount,
            ("amount_over_auto_limit",),
        )
    if prior_auto_payment_today:
        return DecisionPlan(
            "seek_approval",
            "create_approval",
            amount,
            ("daily_auto_limit_reached",),
        )
    return DecisionPlan(
        "auto_pay",
        "submit_payout",
        amount,
        ("verified_shortfall",),
    )
