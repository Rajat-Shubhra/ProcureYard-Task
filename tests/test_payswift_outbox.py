import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

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
from app.providers.payswift import PayoutOutboxWorker, PaySwiftClient


class LostResponseSandbox:
    """PaySwift sandbox lost-response path: the payout is written, then the response is a 504.

    POSTs are counted on the wire so the test proves there was exactly one submission.
    """

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.ledger: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            assert request.url.path == "/v1/payouts"
            return httpx.Response(200, json={"data": self.ledger, "count": len(self.ledger)})
        assert request.method == "POST" and request.url.path == "/v1/payouts"
        body = json.loads(request.content)
        self.posts.append({"key": request.headers["Idempotency-Key"], "body": body})
        self.ledger.append(
            {
                "payout_id": f"pout_{len(self.ledger) + 1}",
                "rider_id": body["rider_id"],
                "amount": body["amount"],
                "reference": body["reference"],
                "status": "processed",
            }
        )
        return httpx.Response(504, json={"error": "gateway_timeout", "message": "lost"})


def seed_queued_intent(database: Database) -> None:
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
    try:
        Base.metadata.create_all(database.engine)
        seed_queued_intent(database)
        sandbox = LostResponseSandbox()
        settings = Settings(
            _env_file=None,
            app_env="test",
            llm_provider="fake",
            worker_batch_size=1,
        )
        client = PaySwiftClient(
            settings,
            client=httpx.Client(
                base_url="https://payswift.test",
                transport=httpx.MockTransport(sandbox.handler),
            ),
        )
        worker = PayoutOutboxWorker(database, settings, client)

        assert worker.tick() == 1
        assert worker.tick() == 0

        # Exactly one submission across both ticks; the payout was recovered from the ledger.
        assert len(sandbox.posts) == 1
        assert sandbox.posts[0] == {
            "key": "payout:dsp_1:1",
            "body": {"rider_id": "R001", "amount": 25, "reference": "qd:test"},
        }
        assert len(sandbox.ledger) == 1
        with database.session() as session:
            intent = session.get(PayoutIntent, "intent_1")
            assert intent is not None and intent.status == "confirmed"
            assert intent.provider_payout_id == "pout_1"
            assert intent.lease_until is None
            dispute = session.get(Dispute, "dsp_1")
            assert dispute is not None and dispute.state == "paid"
            attempts = session.scalars(select(PayoutAttempt)).all()
            assert [(a.attempt_number, a.outcome, a.error_code) for a in attempts] == [
                (1, "provider_retryable", "http_504")
            ]
    finally:
        database.dispose()
