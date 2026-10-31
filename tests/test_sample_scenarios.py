import json
import shutil
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.agent.contracts import MessageEnvelope
from app.agent.fallback import HeuristicInterpretationProvider
from app.config import Settings
from app.conversation.service import ConversationService
from app.db import Database
from app.ingestion.service import ImportService
from app.models import Base, OpsItem, PayoutIntent

ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads((ROOT / "data/conversations.json").read_text(encoding="utf-8"))


class EmptyLedger:
    def list_payouts(self, _rider_id: str) -> list[dict[str, object]]:
        return []


@pytest.fixture(scope="session")
def source_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("scenario-source") / "source.db"
    database = Database(f"sqlite+pysqlite:///{path}")
    Base.metadata.create_all(database.engine)
    ImportService(
        database,
        snapshot_version="assignment-export-v1",
        normalization_version="source-normalization-v1",
    ).import_directory(ROOT / "data")
    database.dispose()
    return path


@pytest.mark.parametrize("case_index", range(len(CASES)))
def test_supplied_scenario_reaches_an_accepted_money_and_reply_outcome(
    case_index: int,
    source_database: Path,
    tmp_path: Path,
) -> None:
    case = CASES[case_index]
    database_path = tmp_path / "case.db"
    shutil.copyfile(source_database, database_path)
    database = Database(f"sqlite+pysqlite:///{database_path}")
    settings = Settings(
        _env_file=None,
        app_env="test",
        llm_provider="fake",
        database_url=str(database.engine.url),
    )
    service = ConversationService(
        database,
        settings,
        interpretation_provider=HeuristicInterpretationProvider(),
        payswift_client=EmptyLedger(),  # type: ignore[arg-type]
    )
    replies: list[str] = []
    for turn in case["turns"]:
        if turn.get("from") != "rider":
            continue
        result = service.process(
            MessageEnvelope(
                turn["message_id"],
                case["rider_id"],
                turn["text"],
                datetime.fromisoformat(turn["received_at"]),
            )
        )
        replies.append(result.reply)

    with database.session() as session:
        payout = int(
            session.scalar(
                select(func.coalesce(func.sum(PayoutIntent.amount), 0)).where(
                    PayoutIntent.rider_id == case["rider_id"]
                )
            )
            or 0
        )
        approval_value = session.scalar(
            select(func.sum(OpsItem.amount)).where(
                OpsItem.rider_id == case["rider_id"],
                OpsItem.item_type == "approval",
                OpsItem.status == "pending",
            )
        )
        approval = int(approval_value) if approval_value is not None else None
        escalation = bool(
            session.scalar(
                select(func.count())
                .select_from(OpsItem)
                .where(
                    OpsItem.rider_id == case["rider_id"],
                    OpsItem.item_type == "escalation",
                    OpsItem.status == "pending",
                )
            )
        )
    expected = case["expected"]
    acceptable = expected.get("one_of") or [expected]
    assert any(
        payout == option.get("payout", 0) and approval == option.get("approval")
        for option in acceptable
    ), (case_index, payout, approval, acceptable, replies)
    if expected.get("escalation") == "required":
        assert escalation
    if expected.get("escalation") == "no":
        assert not escalation
    combined = " ".join(replies).lower()
    for mention in expected.get("reply_mentions", []):
        assert str(mention).lower() in combined, (case_index, mention, replies)
    database.dispose()
