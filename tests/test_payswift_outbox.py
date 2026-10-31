from datetime import UTC, date, datetime
from pathlib import Path

import httpx
from sqlalchemy import select

from app.config import Settings
from app.db import Database
from app.models import (
    Base,
    Conversation,
    Decision,
    Dispute,
    PayoutAttempt,
    PayoutIntent,
    Rider,
)
from app.providers.payswift import PayoutOutboxWorker, PaySwiftClient, ProviderResult


class LostResponsePaySwift:
    def __init__(self) -> None:
        self.created = False

    def list_payouts(self, rider_id: str) -> list[dict[str, object]]:
        if not self.created:
            return []
        return [
            {
                "payout_id": "pout_1",
                "rider_id": rider_id,
                "amount": 25,
                "reference": "qd:test",
                "status": "processed",
            }
        ]

    def create_payout(self, **_kwargs) -> ProviderResult:  # type: ignore[no-untyped-def]
        self.created = True
        return ProviderResult(
            "transport_ambiguous",
            None,
            retryable=True,
            ambiguous=True,
            error_code="timeout",
        )


def test_payswift_list_adapter_reads_sandbox_data_envelope() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["rider_id"] == "R001"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "payout_id": "pout_1",
                        "rider_id": "R001",
                        "amount": 25,
                        "reference": "qd:test",
                        "status": "processed",
                    }
                ],
                "count": 1,
            },
        )

    settings = Settings(_env_file=None, app_env="test", llm_provider="fake")
    client = PaySwiftClient(
        settings,
        client=httpx.Client(
            base_url="https://payswift.test",
            transport=httpx.MockTransport(handler),
        ),
    )

    assert client.list_payouts("R001")[0]["payout_id"] == "pout_1"


def test_lost_provider_response_is_reconciled_without_second_payout(tmp_path: Path) -> None:
    database = Database(f"sqlite+pysqlite:///{tmp_path / 'outbox.db'}")
    Base.metadata.create_all(database.engine)
    with database.session() as session, session.begin():
        session.add(
            Rider(
                rider_id="R001",
                name="Rider",
                city="City",
                joined_on=date(2024, 1, 1),
                source_snapshot_version="test",
                raw_payload={},
            )
        )
        session.add(
            Conversation(
                conversation_id="conv_1",
                rider_id="R001",
                state="payout_processing",
                turn_number=1,
                memory_version=1,
                language="en",
                memory={},
            )
        )
        session.add(
            Dispute(
                dispute_id="dsp_1",
                conversation_id="conv_1",
                rider_id="R001",
                state="payout_processing",
                dispute_type="surge",
                service_date_ist=date(2026, 9, 20),
                policy_cause="surge",
                grouping_key="test",
            )
        )
        session.add(
            Decision(
                decision_id="dec_1",
                dispute_id="dsp_1",
                decision_version=1,
                decision_class="auto_pay",
                amount=25,
                reason_codes=["verified_shortfall"],
                evidence_ids=[],
                policy_version="v1",
            )
        )
        session.add(
            PayoutIntent(
                intent_id="intent_1",
                rider_id="R001",
                dispute_id="dsp_1",
                decision_id="dec_1",
                idempotency_key="payout:dsp_1:1",
                request_fingerprint="fingerprint",
                amount=25,
                provider_reference="qd:test",
                status="queued",
                next_attempt_at=datetime.now(UTC),
            )
        )
    settings = Settings(
        _env_file=None,
        app_env="test",
        llm_provider="fake",
        worker_batch_size=1,
    )
    worker = PayoutOutboxWorker(
        database,
        settings,
        LostResponsePaySwift(),  # type: ignore[arg-type]
    )

    assert worker.tick() == 1

    with database.session() as session:
        intent = session.get(PayoutIntent, "intent_1")
        assert intent is not None and intent.status == "confirmed"
        assert intent.provider_payout_id == "pout_1"
        assert session.scalar(select(PayoutAttempt)) is not None
