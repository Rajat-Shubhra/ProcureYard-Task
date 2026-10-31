from app.gates.action import ActionGate, ActionProposal
from app.gates.reply import ReplyGate, ReplyProposal


def safe_action(**changes) -> ActionProposal:  # type: ignore[no-untyped-def]
    values = {
        "kind": "submit_payout",
        "origin": "deterministic_policy_engine",
        "envelope_rider_id": "R003",
        "dispute_rider_id": "R003",
        "ownership": "verified",
        "policy_window": "eligible",
        "evidence_complete": True,
        "source_ambiguous": False,
        "verified_shortfall": 25,
        "proposed_amount": 25,
        "prior_auto_payment_today": False,
        "payswift_reconciled": True,
        "idempotency_fingerprint_matches": True,
    }
    values.update(changes)
    return ActionProposal(**values)  # type: ignore[arg-type]


def test_forced_unsafe_model_action_is_blocked() -> None:
    report = ActionGate().check(
        safe_action(origin="model", proposed_amount=300, verified_shortfall=25)
    )

    assert not report.passed
    assert "action.non_model_authority" in report.blocking_reasons
    assert "money.proposal_matches_shortfall" in report.blocking_reasons
    assert "money.auto_limit" in report.blocking_reasons


def test_approval_requires_a_real_guardrail_trigger() -> None:
    unnecessary = ActionGate().check(safe_action(kind="create_approval"))
    necessary = ActionGate().check(
        safe_action(kind="create_approval", proposed_amount=249, verified_shortfall=249)
    )

    assert not unnecessary.passed
    assert necessary.passed


def test_escalation_requires_unknown_or_unsafe_facts_and_no_amount() -> None:
    unnecessary = ActionGate().check(
        safe_action(
            kind="create_escalation",
            proposed_amount=None,
            verified_shortfall=None,
        )
    )
    necessary = ActionGate().check(
        safe_action(
            kind="create_escalation",
            ownership="unknown",
            proposed_amount=None,
            verified_shortfall=None,
            escalation_category="ownership",
            escalation_reason="Ownership cannot be established.",
        )
    )

    assert not unnecessary.passed
    assert necessary.passed


def test_reply_gate_blocks_false_paid_claim_and_other_rider_data() -> None:
    report = ReplyGate().check(
        ReplyProposal(
            reply="R005 ka T999 payment ₹25 processed ho gaya.",
            envelope_rider_id="R003",
            allowed_trip_ids=frozenset({"T100"}),
            supported_amounts=frozenset({25}),
            claims_payment_status="paid",
            provider_confirmed=False,
            approval_pending=False,
            escalation_pending=False,
        )
    )

    assert not report.passed
    assert "reply.identity_privacy" in report.blocking_reasons
    assert "reply.trip_ids_supported" in report.blocking_reasons
    assert "reply.payment_claim" in report.blocking_reasons
