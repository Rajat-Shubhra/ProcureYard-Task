from __future__ import annotations

from dataclasses import dataclass

from app.domain.payout import DisputeEvaluation

AUTO_PAY_LIMIT_RUPEES = 200

_ESCALATION_TEXT = {
    "distance": (
        "distance",
        "Rider disputes the recorded trip distance; the export cannot prove the real route.",
    ),
    "record_disagreement": (
        "record_disagreement",
        "Rider disagrees with the recorded result after a deterministic re-check.",
    ),
    "policy_exception": (
        "policy_exception",
        "Rider requests a policy exception for a recorded rider cancellation.",
    ),
}


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
    committed_amount: int = 0,
) -> DecisionPlan:
    """Classify a deterministic evaluation into exactly one decision class.

    ``committed_amount`` is money already queued/paid/awaiting approval for overlapping
    evidence (Gate B rule 9); the ₹200 automatic limit applies to the cumulative total so a
    shortfall cannot be split across messages to stay under the limit.
    """
    if evaluation.ownership == "mismatch":
        return DecisionPlan(
            "escalate",
            "create_escalation",
            None,
            ("ownership_mismatch",),
            escalation_category="ownership",
            escalation_reason=(
                "A named trip is not linked to the envelope rider's account; "
                "no owner details were disclosed."
            ),
        )
    if evaluation.policy_window in {"expired", "future"}:
        return DecisionPlan(
            "explain_no_shortfall",
            "reply_only",
            None,
            ("outside_policy_window",),
        )
    if (
        evaluation.source_ambiguous
        and evaluation.evidence_complete
        and "conflicting_source_rows" in evaluation.reason_codes
    ):
        return DecisionPlan(
            "escalate",
            "create_escalation",
            None,
            ("source_data_ambiguous",) + tuple(evaluation.reason_codes),
            escalation_category="data_quality",
            escalation_reason=(
                "Source rows for this evidence are conflicting or were rejected at import; "
                "no single safe interpretation exists."
            ),
        )
    if evaluation.complaint_type in _ESCALATION_TEXT:
        if not evaluation.evidence_complete:
            return DecisionPlan(
                "ask_clarification",
                "reply_only",
                None,
                ("missing_grounding",),
            )
        category, reason = _ESCALATION_TEXT[evaluation.complaint_type]
        return DecisionPlan(
            "escalate",
            "create_escalation",
            None,
            evaluation.reason_codes,
            escalation_category=category,
            escalation_reason=reason,
        )
    if evaluation.source_ambiguous and evaluation.evidence_complete:
        return DecisionPlan(
            "escalate",
            "create_escalation",
            None,
            ("source_data_ambiguous",) + tuple(evaluation.reason_codes),
            escalation_category="data_quality",
            escalation_reason=(
                "Source rows for this evidence are conflicting or were rejected at import; "
                "no single safe interpretation exists."
            ),
        )
    if not evaluation.evidence_complete or evaluation.verified_shortfall is None:
        return DecisionPlan(
            "ask_clarification",
            "reply_only",
            None,
            ("missing_grounding",),
        )
    if evaluation.verified_shortfall <= 0:
        return DecisionPlan(
            "explain_no_shortfall",
            "reply_only",
            None,
            ("no_shortfall",),
        )
    amount = evaluation.verified_shortfall
    if amount > AUTO_PAY_LIMIT_RUPEES or committed_amount + amount > AUTO_PAY_LIMIT_RUPEES:
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
