from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.agent.contracts import InterpretationResult, MessageEnvelope, ModelInterpretation
from app.agent.fallback import HeuristicInterpretationProvider
from app.config import Settings
from app.conversation.service import ConversationService
from app.db import Database
from app.ingestion.service import ImportService
from app.models import Base, Complaint, Dispute, OpsItem, PayoutIntent, TraceEvent

ROOT = Path(__file__).resolve().parents[1]


class EmptyLedger:
    def list_payouts(self, _rider_id: str) -> list[dict[str, object]]:
        return []


class StaticProvider:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def interpret(self, _context) -> InterpretationResult:  # type: ignore[no-untyped-def]
        return InterpretationResult(
            interpretation=ModelInterpretation.from_payload(self.payload),
            provider="test",
            model="test",
            prompt_version="strict-rules-v2",
            attempt_count=1,
        )


@pytest.fixture()
def service(tmp_path: Path) -> tuple[ConversationService, Database]:
    database = Database(f"sqlite+pysqlite:///{tmp_path / 'conversation.db'}")
    Base.metadata.create_all(database.engine)
    ImportService(
        database,
        snapshot_version="assignment-export-v1",
        normalization_version="source-normalization-v1",
    ).import_directory(ROOT / "data")
    settings = Settings(
        _env_file=None,
        app_env="test",
        llm_provider="fake",
        database_url=str(database.engine.url),
    )
    conversation = ConversationService(
        database,
        settings,
        interpretation_provider=HeuristicInterpretationProvider(),
        payswift_client=EmptyLedger(),  # type: ignore[arg-type]
    )
    return conversation, database


def test_two_complaints_are_stored_and_decided_independently(
    service: tuple[ConversationService, Database],
) -> None:
    conversation, database = service
    envelope = MessageEnvelope(
        "wamid.CONV2A",
        "R027",
        "20 ko surge nahi mila aur 21 ko penalty do baar kata",
        datetime.fromisoformat("2026-09-23T10:30:00+05:30"),
    )

    result = conversation.process(envelope)
    duplicate = conversation.process(envelope)

    assert "₹15" in result.reply and "₹10" in result.reply
    assert duplicate.duplicate and duplicate.reply == result.reply
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(Complaint)) == 2
        assert session.scalar(select(func.count()).select_from(Dispute)) == 2
        assert session.scalar(select(func.count()).select_from(PayoutIntent)) == 1
        approval = session.scalar(select(OpsItem).where(OpsItem.item_type == "approval"))
        assert approval is not None and approval.amount == 10
        assert session.scalar(select(func.count()).select_from(TraceEvent)) >= 6


def test_vague_complaint_then_date_revises_one_dispute(
    service: tuple[ConversationService, Database],
) -> None:
    conversation, database = service
    first = conversation.process(
        MessageEnvelope(
            "wamid.CONV1A",
            "R003",
            "bhai payout galat aaya hai",
            datetime.fromisoformat("2026-09-22T09:00:00+05:30"),
        )
    )
    second = conversation.process(
        MessageEnvelope(
            "wamid.CONV1B",
            "R003",
            "20 wala",
            datetime.fromisoformat("2026-09-22T09:02:00+05:30"),
        )
    )

    assert "date" in first.reply.lower() or "trip" in first.reply.lower()
    assert "₹25" in second.reply
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(Dispute)) == 1
        assert session.scalar(select(func.count()).select_from(Complaint)) == 2


def test_compatible_complaints_remain_individual_inside_one_grouped_dispute(
    service: tuple[ConversationService, Database],
) -> None:
    _, database = service
    base = {
        "complaint_type": "surge",
        "summary": "Surge missing.",
        "service_date_ist": "2026-09-20",
        "trip_ids": ["T926334"],
        "alleged_amount_rupees": None,
        "grounding": {"status": "grounded", "missing_fields": []},
        "relation": {
            "kind": "new",
            "target_dispute_id": None,
            "contradicts_prior": False,
        },
        "proposed_group": {
            "policy_cause": "surge",
            "service_date_ist": "2026-09-20",
            "message_context": "current_message",
        },
        "confidence": 0.99,
    }
    payload = {
        "schema_version": "2.0.0",
        "language": "en",
        "message_intent": "new_complaint",
        "requested_status": "not_applicable",
        "safety_flags": ["none"],
        "complaints": [
            {**base, "complaint_index": 0, "summary": "First surge allegation."},
            {**base, "complaint_index": 1, "summary": "Second surge allegation."},
        ],
    }
    settings = Settings(
        _env_file=None,
        app_env="test",
        llm_provider="fake",
        database_url=str(database.engine.url),
    )
    conversation = ConversationService(
        database,
        settings,
        interpretation_provider=StaticProvider(payload),  # type: ignore[arg-type]
        payswift_client=EmptyLedger(),  # type: ignore[arg-type]
    )

    conversation.process(
        MessageEnvelope(
            "msg-grouped",
            "R003",
            "two surge complaints",
            datetime.fromisoformat("2026-09-22T09:00:00+05:30"),
        )
    )

    with database.session() as session:
        complaints = list(
            session.scalars(select(Complaint).where(Complaint.source_message_id == "msg-grouped"))
        )
        assert len(complaints) == 2
        assert len({item.dispute_id for item in complaints}) == 1
