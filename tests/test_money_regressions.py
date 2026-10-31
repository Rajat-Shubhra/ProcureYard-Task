"""Regression tests for the money-path defects found in the repository audit.

Every test drives the real ``ConversationService`` (deterministic fallback provider) against the
real source export on an isolated SQLite copy, and asserts on payout intents, ops items and the
PaySwift ledger — i.e. on money, not on wording.
"""

from __future__ import annotations

import shutil
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from app.agent.contracts import MessageEnvelope
from app.agent.fallback import HeuristicInterpretationProvider
from app.config import Settings
from app.conversation.service import ConversationService
from app.db import Database
from app.domain.decision import plan_decision
from app.domain.payout import DisputeEvaluation, PayoutPolicyEngine
from app.ingestion.service import ImportService
from app.models import Base, Complaint, Decision, Dispute, OpsItem, PayoutIntent
from app.ops.service import OpsService

ROOT = Path(__file__).resolve().parents[1]


class FakeLedger:
    """Minimal PaySwift stand-in: an in-memory ledger the tests can seed."""

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def list_payouts(self, rider_id: str) -> list[dict[str, Any]]:
        return [item for item in self.items if item["rider_id"] == rider_id]


@pytest.fixture(scope="module")
def source_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("regression-source") / "source.db"
    database = Database(f"sqlite+pysqlite:///{path}")
    Base.metadata.create_all(database.engine)
    ImportService(
        database,
        snapshot_version="assignment-export-v1",
        normalization_version="source-normalization-v1",
    ).import_directory(ROOT / "data")
    database.dispose()
    return path


@pytest.fixture
def stack(source_database: Path, tmp_path: Path):
    database_path = tmp_path / "case.db"
    shutil.copyfile(source_database, database_path)
    database = Database(f"sqlite+pysqlite:///{database_path}")
    settings = Settings(
        _env_file=None,
        app_env="test",
        llm_provider="fake",
        database_url=str(database.engine.url),
    )
    ledger = FakeLedger()
    service = ConversationService(
        database,
        settings,
        interpretation_provider=HeuristicInterpretationProvider(),
        payswift_client=ledger,  # type: ignore[arg-type]
    )
    ops = OpsService(database, settings, ledger)  # type: ignore[arg-type]
    try:
        yield database, service, ops, ledger
    finally:
        database.dispose()


def envelope(message_id: str, rider_id: str, text: str, received_at: str) -> MessageEnvelope:
    return MessageEnvelope(
        message_id=message_id,
        rider_id=rider_id,
        text=text,
        received_at=datetime.fromisoformat(received_at),
    )


def confirm_in_payswift(database: Database, ledger: FakeLedger, payout_id: str = "po_1") -> None:
    """Simulate the worker: the queued intent is confirmed and visible in the PaySwift ledger."""
    with database.session() as session, session.begin():
        intent = session.scalar(select(PayoutIntent).where(PayoutIntent.status == "queued"))
        assert intent is not None
        intent.status = "confirmed"
        intent.provider_payout_id = payout_id
        ledger.items.append(
            {
                "payout_id": payout_id,
                "rider_id": intent.rider_id,
                "amount": intent.amount,
                "reference": intent.provider_reference,
                "status": "processed",
            }
        )


def money(database: Database) -> dict[str, Any]:
    with database.session() as session:
        intents = list(session.scalars(select(PayoutIntent)))
        items = list(session.scalars(select(OpsItem)))
        decisions = session.scalar(select(func.count()).select_from(Decision))
    return {
        "intents": [(i.amount, i.status) for i in intents],
        "intent_total": sum(i.amount for i in intents),
        "pending_approvals": [
            i.amount for i in items if i.status == "pending" and i.item_type == "approval"
        ],
        "escalations": [i for i in items if i.item_type == "escalation"],
        "ops": [(i.item_type, i.amount, i.status) for i in items],
        "decisions": decisions,
    }


# --- C1: one verified shortfall is paid exactly once


def test_resent_complaint_on_a_later_day_is_not_paid_twice(stack) -> None:
    database, service, _, ledger = stack
    first = service.process(
        envelope(
            "w1",
            "R003",
            "Bhai order T926334 ka surge nahi mila, 20 tarikh wala",
            "2026-09-22T09:05:00+05:30",
        )
    )
    assert "₹25" in first.reply and money(database)["intents"] == [(25, "queued")]
    confirm_in_payswift(database, ledger)

    second = service.process(
        envelope(
            "w2",
            "R003",
            "Bhai order T926334 ka surge nahi mila, 20 tarikh wala",
            "2026-09-23T09:05:00+05:30",
        )
    )
    state = money(database)
    assert state["intents"] == [(25, "confirmed")], state
    assert state["pending_approvals"] == [] and state["decisions"] == 1
    assert "pehle hi" in second.reply and "₹25" in second.reply


def test_verified_trip_date_groups_wrong_date_then_undated_follow_up(stack) -> None:
    database, service, _, _ = stack

    first = service.process(
        envelope(
            "w1",
            "R035",
            "21 ko order T176205 ka surge nahi mila",
            "2026-09-22T11:00:00+05:30",
        )
    )
    second = service.process(
        envelope(
            "w2",
            "R035",
            "order T176205 ka surge nahi mila",
            "2026-09-22T11:05:00+05:30",
        )
    )

    state = money(database)
    assert "₹37" in first.reply and "processing" in second.reply
    assert state["intents"] == [(37, "queued")]
    assert state["decisions"] == 1
    with database.session() as session:
        disputes = list(session.scalars(select(Dispute)))
        complaint_dispute_ids = list(
            session.scalars(select(Complaint.dispute_id).order_by(Complaint.created_at))
        )
        intent = session.scalar(select(PayoutIntent))
    assert len(disputes) == 1
    assert disputes[0].service_date_ist == date(2026, 9, 20)
    assert disputes[0].state == "payout_processing"
    assert complaint_dispute_ids == [disputes[0].dispute_id, disputes[0].dispute_id]
    assert intent is not None and intent.dispute_id == disputes[0].dispute_id


def test_resend_on_the_same_day_creates_no_duplicate_approval(stack) -> None:
    database, service, _, _ = stack
    text = "Bhai order T926334 ka surge nahi mila, 20 tarikh wala"
    service.process(envelope("w1", "R003", text, "2026-09-22T09:05:00+05:30"))
    reply = service.process(envelope("w2", "R003", text, "2026-09-22T09:30:00+05:30")).reply
    state = money(database)
    assert state["intents"] == [(25, "queued")] and state["pending_approvals"] == [], state
    assert "processing" in reply


def test_follow_up_chatter_creates_no_new_decisions_or_approvals(stack) -> None:
    database, service, _, _ = stack
    service.process(
        envelope(
            "w1",
            "R003",
            "Bhai order T926334 ka surge nahi mila, 20 tarikh wala",
            "2026-09-22T09:05:00+05:30",
        )
    )
    for index, text in enumerate(
        ("kitna time lagega bhai?", "ok", "thanks", "hmm theek hai"), start=2
    ):
        reply = service.process(
            envelope(f"w{index}", "R003", text, f"2026-09-22T09:{10 + index}:00+05:30")
        ).reply
        assert "approval" not in reply, reply
    state = money(database)
    assert state["intents"] == [(25, "queued")] and state["ops"] == [] and state["decisions"] == 1


def test_duplicate_delivery_with_new_message_id_yields_one_approval(stack) -> None:
    database, service, ops, _ = stack
    text = "19 sept ke 5 orders ka paisa hi nahi aaya!! jaldi karo"
    service.process(envelope("w1", "R016", text, "2026-09-22T11:30:00+05:30"))
    service.process(envelope("w2", "R016", text, "2026-09-22T11:31:00+05:30"))
    state = money(database)
    assert state["pending_approvals"] == [425], state
    item_id = next(i for i in state["ops"] if i[0] == "approval")  # noqa: F841 - shape check
    with database.session() as session:
        item = session.scalar(select(OpsItem).where(OpsItem.status == "pending"))
    assert ops.approve(item.item_id, "ops-user", None).status == "approved"
    assert money(database)["intent_total"] == 425


def test_different_wording_about_a_paid_trip_is_reconciled_not_repaid(stack) -> None:
    database, service, _, ledger = stack
    service.process(
        envelope("w1", "R003", "T926334 ka surge nahi mila", "2026-09-22T09:05:00+05:30")
    )
    confirm_in_payswift(database, ledger)
    # Date-only phrasing creates a differently-keyed dispute; trip-level coverage must still apply.
    reply = service.process(
        envelope("w2", "R003", "20 ko payout kam aaya tha", "2026-09-24T09:05:00+05:30")
    ).reply
    state = money(database)
    assert state["intent_total"] == 25 and state["pending_approvals"] == [], state
    assert "pehle hi" in reply


def test_split_shortfall_across_days_hits_the_cumulative_auto_limit(stack) -> None:
    database, service, _, ledger = stack
    # R034's ₹249 (91 + 85 + 73) sent one trip per day must not become three auto-pays.
    for day, trip in ((23, "T954299"), (24, "T409395"), (25, "T121056")):
        service.process(
            envelope(
                f"w{day}",
                "R034",
                f"order {trip} ka payment nahi aaya",
                f"2026-09-{day}T11:30:00+05:30",
            )
        )
        if day < 25:
            confirm_in_payswift(database, ledger, payout_id=f"po_{day}")
    state = money(database)
    assert sorted(i[0] for i in state["intents"]) == [85, 91], state
    assert state["pending_approvals"] == [73], state


def test_rejected_claim_is_reported_not_re_decided(stack) -> None:
    database, service, ops, _ = stack
    text = "19 sept ke 5 orders ka paisa hi nahi aaya!! jaldi karo"
    service.process(envelope("w1", "R016", text, "2026-09-22T11:30:00+05:30"))
    with database.session() as session:
        item = session.scalar(select(OpsItem).where(OpsItem.status == "pending"))
    ops.reject(item.item_id, "ops-user", "trips not verified with dispatch")
    reply = service.process(envelope("w2", "R016", text, "2026-09-23T11:30:00+05:30")).reply
    state = money(database)
    assert state["intents"] == [] and state["pending_approvals"] == [], state
    assert "decline" in reply and "₹425" in reply


# --- C4: the human approval path cannot pay again


def test_ops_approval_closes_stale_when_payswift_already_holds_the_payout(stack) -> None:
    database, service, ops, ledger = stack
    service.process(
        envelope(
            "w1",
            "R034",
            "Sep 19 ko 3 orders ka paisa missing hai payout mein",
            "2026-09-23T11:30:00+05:30",
        )
    )
    with database.session() as session:
        item = session.scalar(select(OpsItem).where(OpsItem.status == "pending"))
        dispute = session.get(Dispute, item.dispute_id)
    # Money already moved for this logical dispute (e.g. before a database restore): the
    # ledger knows it under the logical reference prefix, the local DB does not.
    from app.domain.coverage import logical_reference

    with database.session() as session:
        decision = session.get(Decision, item.decision_id)
        from app.models import Evidence

        payload = session.get(Evidence, decision.evidence_ids[0]).payload
    prefix = logical_reference(
        "R034",
        payload["complaint_type"],
        date.fromisoformat(payload["service_date_ist"]),
        tuple(payload["trip_ids"]),
    )
    ledger.items.append(
        {
            "payout_id": "po_old",
            "rider_id": "R034",
            "amount": 249,
            "reference": f"{prefix}:day:2026-09-20",
            "status": "processed",
        }
    )

    result = ops.approve(item.item_id, "ops-user", None)
    assert result.status == "closed_stale"
    assert money(database)["intents"] == []
    with database.session() as session:
        assert session.get(OpsItem, item.item_id).status == "closed_stale"
        assert session.get(Dispute, dispute.dispute_id).state == "paid"


def test_ops_approval_uses_the_decision_evidence_and_still_pays_once(stack) -> None:
    database, service, ops, _ = stack
    service.process(
        envelope(
            "w1",
            "R016",
            "19 sept ke 5 orders ka paisa hi nahi aaya!! jaldi karo",
            "2026-09-22T11:30:00+05:30",
        )
    )
    with database.session() as session:
        item = session.scalar(select(OpsItem).where(OpsItem.status == "pending"))
    first = ops.approve(item.item_id, "ops-user", None)
    second = ops.approve(item.item_id, "ops-user", None)
    assert first.status == second.status == "approved"
    assert first.payout_intent_id == second.payout_intent_id
    assert money(database)["intents"] == [(425, "queued")]


# --- C2: the seven-day window is decided by evidence, not by the claimed date


def test_claimed_recent_date_cannot_move_an_expired_trip_into_the_window(stack) -> None:
    database, service, _, _ = stack
    reply = service.process(
        envelope(
            "w1",
            "R022",
            "20 tarikh ko order T502951 ka surge nahi mila",
            "2026-09-23T09:00:00+05:30",
        )
    ).reply
    assert money(database)["intents"] == [] and money(database)["ops"] == []
    assert "7-day" in reply


def test_engine_uses_trip_service_date_for_the_window(source_database: Path) -> None:
    database = Database(f"sqlite+pysqlite:///{source_database}")
    try:
        with database.session() as session:
            evaluation = PayoutPolicyEngine().evaluate(
                session,
                rider_id="R022",
                complaint_type="surge",
                service_date_ist=date(2026, 9, 20),
                trip_ids=("T502951",),
                received_at=datetime.fromisoformat("2026-09-23T09:00:00+05:30"),
            )
    finally:
        database.dispose()
    assert evaluation.policy_window == "expired"
    assert evaluation.service_date_ist == date(2026, 9, 13)
    assert "claimed_date_mismatch" in evaluation.data_quality_flags


# --- C3: unclear or shady cases reach ops


def test_cross_account_trip_escalates_without_disclosing_the_owner(stack) -> None:
    database, service, _, _ = stack
    reply = service.process(
        envelope("w1", "R021", "order T482410 ka payment nahi aaya", "2026-09-22T12:30:00+05:30")
    ).reply
    state = money(database)
    assert state["intents"] == [] and len(state["escalations"]) == 1
    assert state["escalations"][0].amount is None
    assert "R030" not in reply and "R030" not in state["escalations"][0].reason


def test_pushback_after_a_verified_result_escalates_with_the_rechecked_count(stack) -> None:
    database, service, _, _ = stack
    service.process(
        envelope("w1", "R011", "19 ko incentive nahi mila", "2026-09-22T10:10:00+05:30")
    )
    reply = service.process(
        envelope(
            "w2", "R011", "nahi nahi 12 kiye the, dobara check karo", "2026-09-22T10:12:00+05:30"
        )
    ).reply
    state = money(database)
    assert state["intents"] == [] and len(state["escalations"]) == 1, state
    assert "10" in reply and "7-day" not in reply
    with database.session() as session:
        disputes = list(session.scalars(select(Dispute)))
    assert len(disputes) == 1 and disputes[0].service_date_ist == date(2026, 9, 19)


def test_r027_follow_up_reports_the_pending_approval_without_re_dating(stack) -> None:
    database, service, _, _ = stack
    service.process(
        envelope(
            "w1",
            "R027",
            "20 ko surge nahi mila aur 21 ko penalty do baar kata",
            "2026-09-23T10:30:00+05:30",
        )
    )
    before = money(database)
    assert before["intents"] == [(15, "queued")] and before["pending_approvals"] == [10]
    reply = service.process(
        envelope("w2", "R027", "10 wala bhi abhi de do na", "2026-09-23T10:31:00+05:30")
    ).reply
    after = money(database)
    assert after["decisions"] == before["decisions"] and after["pending_approvals"] == [10]
    assert "₹10" in reply and "approval" in reply and "7-day" not in reply
    with database.session() as session:
        penalty = session.scalar(
            select(Dispute).where(Dispute.dispute_type == "cancellation_penalty")
        )
    assert penalty.service_date_ist == date(2026, 9, 21) and penalty.state == "awaiting_approval"


# --- decision boundaries and ledger-only prior auto-pay


def _evaluation(shortfall: int) -> DisputeEvaluation:
    return DisputeEvaluation(
        complaint_type="surge",
        rider_id="R003",
        service_date_ist=date(2026, 9, 20),
        trip_ids=("T926334",),
        ownership="verified",
        policy_window="eligible",
        expected_components=(),
        actual_components=(),
        expected_total=shortfall,
        actual_total=0,
        verified_shortfall=shortfall,
        completed_trip_count=None,
        evidence_complete=True,
        source_ambiguous=False,
        reason_codes=("verified_shortfall",),
        data_quality_flags=("none",),
    )


@pytest.mark.parametrize(
    ("shortfall", "committed", "prior", "expected"),
    [
        (199, 0, False, "auto_pay"),
        (200, 0, False, "auto_pay"),
        (201, 0, False, "seek_approval"),
        (100, 101, False, "seek_approval"),  # cumulative cap
        (50, 0, True, "seek_approval"),  # daily limit
        (0, 0, False, "explain_no_shortfall"),
    ],
)
def test_decision_boundaries(shortfall: int, committed: int, prior: bool, expected: str) -> None:
    plan = plan_decision(
        _evaluation(shortfall), prior_auto_payment_today=prior, committed_amount=committed
    )
    assert plan.outcome == expected


def test_ownership_mismatch_and_record_disagreement_escalate() -> None:
    mismatch = replace(_evaluation(25), ownership="mismatch", verified_shortfall=None)
    assert plan_decision(mismatch, prior_auto_payment_today=False).outcome == "escalate"
    disagreement = replace(
        _evaluation(0),
        complaint_type="record_disagreement",
        source_ambiguous=True,
        verified_shortfall=None,
    )
    assert plan_decision(disagreement, prior_auto_payment_today=False).outcome == "escalate"


def test_prior_auto_payment_known_only_to_payswift_forces_approval(stack) -> None:
    database, service, _, ledger = stack
    ledger.items.append(
        {
            "payout_id": "po_ext",
            "rider_id": "R005",
            "amount": 40,
            "reference": "qd:R005:surge:2026-09-20:deadbeefcafe:day:2026-09-22",
            "status": "processed",
        }
    )
    reply = service.process(
        envelope(
            "w1",
            "R005",
            "kal ka payout kam aaya hai. 12 se zyada order kiye the maine",
            "2026-09-22T09:20:00+05:30",
        )
    ).reply
    state = money(database)
    assert state["intents"] == [] and state["pending_approvals"] == [150], state
    assert "approval" in reply


# --- PR #1 review regressions (lock ordering, follow-up date grouping, escalation reasons, ₹None)


def test_ops_approve_reject_and_retry_payout_acquire_rider_lock_before_ops_row(
    stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, service, ops, _ = stack
    events: list[str] = []

    from sqlalchemy import event

    import app.ops.service as ops_module

    real_lock_rider = ops_module.lock_rider

    def traced_lock_rider(session: Any, rider_id: str) -> None:
        events.append(f"lock_rider:{rider_id}")
        real_lock_rider(session, rider_id)

    @event.listens_for(database.engine, "before_cursor_execute")
    def record_sql(_conn: Any, _cursor: Any, statement: str, _params: Any, *_rest: Any) -> None:
        normalized = " ".join(statement.split()).upper()
        if "FROM OPS_ITEMS" in normalized:
            events.append("select_ops_item")

    monkeypatch.setattr(ops_module, "lock_rider", traced_lock_rider)

    # 1. Approve path
    service.process(
        envelope(
            "w_lock_1",
            "R016",
            "19 sept ke 5 orders ka paisa hi nahi aaya!! jaldi karo",
            "2026-09-22T11:30:00+05:30",
        )
    )
    with database.session() as session:
        approval_item = session.scalar(
            select(OpsItem).where(OpsItem.rider_id == "R016", OpsItem.status == "pending")
        )
        assert approval_item is not None
        approval_id = approval_item.item_id

    events.clear()
    assert ops.approve(approval_id, "ops-user", None).status == "approved"
    # Pre-fetch runs outside write tx; inside write tx lock_rider precedes ops_items.
    lock_idx = events.index("lock_rider:R016")
    assert "select_ops_item" in events[lock_idx + 1 :], events

    # 2. Reject path
    service.process(
        envelope(
            "w_lock_2",
            "R034",
            "Sep 19 ko 3 orders ka paisa missing hai payout mein",
            "2026-09-23T11:30:00+05:30",
        )
    )
    with database.session() as session:
        reject_item = session.scalar(
            select(OpsItem).where(OpsItem.rider_id == "R034", OpsItem.status == "pending")
        )
        assert reject_item is not None
        reject_id = reject_item.item_id

    events.clear()
    assert ops.reject(reject_id, "ops-user", "declined").status == "rejected"
    lock_idx = events.index("lock_rider:R034")
    assert "select_ops_item" in events[lock_idx + 1 :], events

    # 3. Retry-payout path on a provider-failure escalation
    with database.session() as session, session.begin():
        intent = session.scalar(select(PayoutIntent).where(PayoutIntent.rider_id == "R016"))
        assert intent is not None
        intent.status = "failed"
        retry_esc = OpsItem(
            item_id="ops_provider_fail_1",
            logical_key=f"provider-failure:{intent.intent_id}",
            rider_id="R016",
            dispute_id=intent.dispute_id,
            decision_id=intent.decision_id,
            item_type="escalation",
            amount=None,
            reason="PaySwift timeout exhausted",
            status="pending",
        )
        session.add(retry_esc)

    events.clear()
    assert ops.retry_payout("ops_provider_fail_1", "ops-user", "retry").status == "requeued"
    lock_idx = events.index("lock_rider:R016")
    assert "select_ops_item" in events[lock_idx + 1 :], events


def test_new_date_surge_claim_opens_a_separate_dispute_instead_of_attaching_to_prior_date(
    stack,
) -> None:
    database, service, _, ledger = stack
    first = service.process(
        envelope("w1", "R035", "20 ko surge nahi mila", "2026-09-22T11:00:00+05:30")
    )
    assert "₹37" in first.reply
    confirm_in_payswift(database, ledger, payout_id="po_sep20")

    second = service.process(
        envelope("w2", "R035", "21 ko surge nahi mila", "2026-09-23T11:45:00+05:30")
    )
    assert "₹25" in second.reply
    state = money(database)
    assert sorted(state["intents"]) == [(25, "queued"), (37, "confirmed")], state
    with database.session() as session:
        disputes = list(
            session.scalars(select(Dispute).order_by(Dispute.service_date_ist))
        )
    assert [d.service_date_ist for d in disputes] == [date(2026, 9, 20), date(2026, 9, 21)]


def test_new_trip_claim_without_date_opens_separate_dispute_from_prior_date(stack) -> None:
    database, service, _, ledger = stack
    first = service.process(
        envelope(
            "w1",
            "R035",
            "order T176205 ka surge nahi mila",
            "2026-09-22T11:00:00+05:30",
        )
    )
    assert "₹37" in first.reply
    confirm_in_payswift(database, ledger, payout_id="po_t176205")

    second = service.process(
        envelope(
            "w2",
            "R035",
            "order T980582 ka surge nahi mila",
            "2026-09-23T11:45:00+05:30",
        )
    )
    assert "₹25" in second.reply
    with database.session() as session:
        disputes = list(
            session.scalars(select(Dispute).order_by(Dispute.service_date_ist))
        )
    assert [d.service_date_ist for d in disputes] == [date(2026, 9, 20), date(2026, 9, 21)]


def test_service_rejects_conflicting_follow_up_target_even_from_model_payload(
    source_database: Path, tmp_path: Path
) -> None:
    from app.agent.contracts import InterpretationResult, ModelInterpretation

    database_path = tmp_path / "forced_link.db"
    shutil.copyfile(source_database, database_path)
    database = Database(f"sqlite+pysqlite:///{database_path}")
    settings = Settings(
        _env_file=None,
        app_env="test",
        llm_provider="fake",
        database_url=str(database.engine.url),
    )
    ledger = FakeLedger()
    heuristic = HeuristicInterpretationProvider()

    class MislinkingProvider:
        def interpret(self, context: Any) -> InterpretationResult:
            if not context.open_disputes:
                return heuristic.interpret(context)
            wrong_target = context.open_disputes[0]["dispute_id"]
            payload = {
                "schema_version": "2.0.0",
                "language": "hinglish",
                "message_intent": "follow_up",
                "requested_status": "not_applicable",
                "safety_flags": ["none"],
                "complaints": [
                    {
                        "complaint_index": 0,
                        "complaint_type": "surge",
                        "summary": "21 Sep surge claim wrongly linked to 20 Sep dispute.",
                        "service_date_ist": "2026-09-21",
                        "trip_ids": ["T980582"],
                        "alleged_amount_rupees": None,
                        "grounding": {"status": "grounded", "missing_fields": []},
                        "relation": {
                            "kind": "follow_up",
                            "target_dispute_id": wrong_target,
                            "contradicts_prior": False,
                        },
                        "proposed_group": {
                            "policy_cause": "surge",
                            "service_date_ist": "2026-09-21",
                            "message_context": "current_message",
                        },
                        "confidence": 0.9,
                    }
                ],
            }
            return InterpretationResult(
                interpretation=ModelInterpretation.from_payload(payload),
                provider="test",
                model="test",
                prompt_version="strict-rules-v2",
                attempt_count=1,
            )

    try:
        service = ConversationService(
            database,
            settings,
            interpretation_provider=MislinkingProvider(),
            payswift_client=ledger,  # type: ignore[arg-type]
        )
        service.process(
            envelope("w1", "R035", "20 ko surge nahi mila", "2026-09-22T11:00:00+05:30")
        )
        confirm_in_payswift(database, ledger, payout_id="po_1")
        second = service.process(
            envelope(
                "w2",
                "R035",
                "21 ko order T980582 ka surge nahi mila",
                "2026-09-23T11:45:00+05:30",
            )
        )
        assert "₹25" in second.reply
        with database.session() as session:
            disputes = list(
                session.scalars(select(Dispute).order_by(Dispute.service_date_ist))
            )
        assert [d.service_date_ist for d in disputes] == [date(2026, 9, 20), date(2026, 9, 21)]
    finally:
        database.dispose()


def test_policy_exception_and_distance_escalations_record_specific_reasons_not_source_conflict(
    stack,
) -> None:
    database, service, _, _ = stack
    r028_reply = service.process(
        envelope(
            "w_r028",
            "R028",
            "accident hua tha isliye order T849302 cancel kiya, "
            "penalty kyun kaata? bahut problem hai",
            "2026-09-23T12:15:00+05:30",
        )
    ).reply
    r036_reply = service.process(
        envelope(
            "w_r036",
            "R036",
            "18 ko order T990831 ka distance galat laga hai",
            "2026-09-23T12:00:00+05:30",
        )
    ).reply

    with database.session() as session:
        r028_item = session.scalar(select(OpsItem).where(OpsItem.rider_id == "R028"))
        r036_item = session.scalar(select(OpsItem).where(OpsItem.rider_id == "R036"))
        assert r028_item is not None and r036_item is not None
        assert "policy exception" in r028_item.reason.lower()
        assert "conflicting" not in r028_item.reason.lower()
        assert "distance" in r036_item.reason.lower()
        assert "conflicting" not in r036_item.reason.lower()

    assert "exception" in r028_reply.lower() and "conflict" not in r028_reply.lower()
    assert "distance" in r036_reply.lower() and "conflict" not in r036_reply.lower()

    # True source conflicts still take the data_quality escalation path.
    conflict_eval = replace(
        _evaluation(0),
        complaint_type="policy_exception",
        verified_shortfall=None,
        source_ambiguous=True,
        reason_codes=("conflicting_source_rows",),
    )
    conflict_plan = plan_decision(conflict_eval, prior_auto_payment_today=False)
    assert conflict_plan.outcome == "escalate"
    assert conflict_plan.escalation_category == "data_quality"


def test_rejected_escalation_status_and_resend_never_render_rupee_none(stack) -> None:
    database, service, ops, _ = stack
    claim = (
        "accident hua tha isliye order T849302 cancel kiya, "
        "penalty kyun kaata? bahut problem hai"
    )
    service.process(envelope("w1", "R028", claim, "2026-09-23T12:15:00+05:30"))
    with database.session() as session:
        item = session.scalar(select(OpsItem).where(OpsItem.rider_id == "R028"))
        assert item is not None and item.item_type == "escalation" and item.amount is None
        item_id = item.item_id

    ops.reject(item_id, "ops-user", "policy penalty stands")

    status_reply = service.process(
        envelope("w2", "R028", "status kya hai? kab tak aayega?", "2026-09-23T13:00:00+05:30")
    ).reply
    resend_reply = service.process(
        envelope("w3", "R028", claim, "2026-09-23T13:05:00+05:30")
    ).reply

    for reply in (status_reply, resend_reply):
        assert "None" not in reply, reply
        assert "₹None" not in reply, reply
        assert "policy penalty stands" in reply, reply

    state = money(database)
    assert state["intents"] == []
    assert [op for op in state["ops"] if op[2] == "pending"] == []
