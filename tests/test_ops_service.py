from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select

from app.agent.contracts import MessageEnvelope
from app.agent.fallback import HeuristicInterpretationProvider
from app.config import Settings
from app.conversation.service import ConversationService
from app.db import Database
from app.ingestion.service import ImportService
from app.models import Base, OpsItem, PayoutIntent
from app.ops.service import OpsService

ROOT = Path(__file__).resolve().parents[1]


class EmptyLedger:
    def list_payouts(self, _rider_id: str) -> list[dict[str, object]]:
        return []


def test_ops_approval_revalidates_and_creates_one_intent(tmp_path: Path) -> None:
    database = Database(f"sqlite+pysqlite:///{tmp_path / 'ops.db'}")
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
    ledger = EmptyLedger()
    conversation = ConversationService(
        database,
        settings,
        interpretation_provider=HeuristicInterpretationProvider(),
        payswift_client=ledger,  # type: ignore[arg-type]
    )
    conversation.process(
        MessageEnvelope(
            "wamid.C8G6SSQKDS7PJR",
            "R016",
            "19 sept ke 5 orders ka paisa hi nahi aaya!! jaldi karo",
            datetime.fromisoformat("2026-09-22T11:30:00+05:30"),
        )
    )
    with database.session() as session:
        item = session.scalar(select(OpsItem).where(OpsItem.item_type == "approval"))
        assert item is not None and item.amount == 425
        item_id = item.item_id

    ops = OpsService(database, settings, ledger)  # type: ignore[arg-type]
    first = ops.approve(item_id, "reviewer", "verified")
    second = ops.approve(item_id, "reviewer", "verified")

    assert first.payout_intent_id == second.payout_intent_id
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(PayoutIntent)) == 1
        intent = session.scalar(select(PayoutIntent))
        assert intent is not None and intent.amount == 425 and intent.status == "queued"
    database.dispose()
