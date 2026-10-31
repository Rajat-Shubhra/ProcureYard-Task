"""Durability tests for the PaySwift outbox worker.

Every scenario drives the real ``PaySwiftClient`` + ``PayoutOutboxWorker`` through an
``httpx.MockTransport`` that mimics the sandbox recovered from ``server.pyc`` (error bodies
``{"error", "message"}``, ``GET /v1/payouts`` → ``{"data": [...], "count": n}``, same-key replay
→ 200, lost response → payout written then 504). POSTs are counted on the wire, so "no second
submission" is asserted at the transport, not on a stub flag.
"""

from __future__ import annotations

import json
import logging
import random
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from threading import Event
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from app.application import WORKER_NAME
from app.config import Settings
from app.db import Database
from app.models import (
    Base,
    Conversation,
    Decision,
    Dispute,
    OpsItem,
    PayoutAttempt,
    PayoutIntent,
    Rider,
    TraceEvent,
    WorkerHeartbeat,
)
from app.providers.payswift import (
    PayoutOutboxWorker,
    PaySwiftClient,
    requeue_failed_intent,
)
from app.worker import run_outbox_loop, start_heartbeat_thread

RIDER = "R001"
AMOUNT = 25


def reference_for(n: int) -> str:
    # The worker matches the ledger on reference *equality* only; the format is opaque to it.
    return f"qd:{RIDER}:surge:2026-09-20:deadbeef{n:04d}:day:2026-09-22"


def error_json(error: str, message: str = "x") -> dict[str, str]:
    return {"error": error, "message": message}


ScriptStep = str | Callable[[httpx.Request], httpx.Response]


class FakePaySwift:
    """Scripted sandbox behind ``httpx.MockTransport``.

    ``script`` lists per-POST behaviours consumed in order (a string action or a callable
    returning the response); once exhausted the fake behaves like the healthy sandbox:
    201 on a new key, 200 replay on same key/same body, 409 ``idempotency_key_reused`` on same
    key/different body.
    """

    def __init__(
        self,
        script: list[ScriptStep] | None = None,
        ledger: list[dict[str, Any]] | None = None,
        *,
        ledger_visible: bool = True,
    ) -> None:
        self.script = list(script or [])
        self.ledger: list[dict[str, Any]] = list(ledger or [])
        self.hidden: list[dict[str, Any]] = []
        self.idem: dict[str, tuple[str, dict[str, Any]]] = {}
        self.posts: list[dict[str, Any]] = []
        self.post_statuses: list[int | str] = []
        self.gets = 0
        self.seq = 0
        self.ledger_visible = ledger_visible
        self.get_response: Callable[[httpx.Request], httpx.Response] | None = None
        self.on_post: Callable[[httpx.Request], None] | None = None

    def reveal(self) -> None:
        """Make payouts written by an in-flight twin request visible on the ledger."""

        self.ledger.extend(self.hidden)
        self.hidden.clear()

    def _write(self, body: dict[str, Any], key: str | None, *, hidden: bool = False) -> dict:
        self.seq += 1
        payout = {
            "payout_id": f"pout_{self.seq:03d}",
            "rider_id": body["rider_id"],
            "amount": body["amount"],
            "reference": body["reference"],
            "status": "processed",
            "created_at": "2026-10-31T00:00:00Z",
        }
        (self.hidden if hidden else self.ledger).append(payout)
        if key:
            self.idem[key] = (json.dumps(body, sort_keys=True), payout)
        return payout

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/payouts":
            self.gets += 1
            if self.get_response is not None:
                return self.get_response(request)
            rider = request.url.params.get("rider_id")
            data = [p for p in self.ledger if p["rider_id"] == rider] if self.ledger_visible else []
            return httpx.Response(200, json={"data": data, "count": len(data)})
        if request.method == "POST" and request.url.path == "/v1/payouts":
            response = self._post(request)
            self.post_statuses.append(response.status_code)
            return response
        return httpx.Response(404, json=error_json("not_found"))

    def _post(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        key = request.headers.get("Idempotency-Key")
        self.posts.append({"key": key, "body": body})
        if self.on_post is not None:
            self.on_post(request)
        action: ScriptStep = self.script.pop(0) if self.script else "normal"
        if callable(action):
            return action(request)
        if action == "timeout":
            self.post_statuses.append("timeout")
            raise httpx.ReadTimeout("simulated read timeout", request=request)
        if action == "timeout_after_write":
            self._write(body, key)
            self.post_statuses.append("timeout")
            raise httpx.ReadTimeout("simulated read timeout", request=request)
        if action == "lost":
            self._write(body, key)
            return httpx.Response(504, json=error_json("gateway_timeout", "lost"))
        if action == "503":
            return httpx.Response(503, json=error_json("service_unavailable"))
        if action == "429":
            return httpx.Response(429, json=error_json("account_blocked", "Too many requests."))
        if action == "400":
            return httpx.Response(400, json=error_json("invalid_amount"))
        if action == "409_in_progress":
            # The twin request holding the key completes later; see ``reveal``.
            self._write(body, key, hidden=True)
            return httpx.Response(409, json=error_json("request_in_progress", "Retry shortly."))
        if action == "409_reused":
            return httpx.Response(409, json=error_json("idempotency_key_reused", "different body"))
        if action == "502_html":
            return httpx.Response(502, text="<html>bad gateway</html>")
        if key and key in self.idem:
            stored_fingerprint, payout = self.idem[key]
            if stored_fingerprint != json.dumps(body, sort_keys=True):
                return httpx.Response(409, json=error_json("idempotency_key_reused", "diff"))
            return httpx.Response(200, json=payout)
        return httpx.Response(201, json=self._write(body, key))


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def build_database(path: Path, *, intents: int = 1) -> Database:
    database = Database(f"sqlite+pysqlite:///{path}")
    Base.metadata.create_all(database.engine)
    due = datetime.now(UTC) - timedelta(seconds=1)
    with database.session() as session, session.begin():
        session.add(
            Rider(
                rider_id=RIDER,
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
                rider_id=RIDER,
                state="payout_processing",
                turn_number=1,
                memory_version=1,
                language="en",
                memory={},
            )
        )
        for n in range(1, intents + 1):
            session.add(
                Dispute(
                    dispute_id=f"dsp_{n}",
                    conversation_id="conv_1",
                    rider_id=RIDER,
                    state="payout_processing",
                    dispute_type="surge",
                    service_date_ist=date(2026, 9, 20),
                    policy_cause="surge",
                    grouping_key=f"test-{n}",
                )
            )
            session.add(
                Decision(
                    decision_id=f"dec_{n}",
                    dispute_id=f"dsp_{n}",
                    decision_version=1,
                    decision_class="auto_pay",
                    amount=AMOUNT,
                    reason_codes=["verified_shortfall"],
                    evidence_ids=[],
                    policy_version="v1",
                )
            )
            session.add(
                PayoutIntent(
                    intent_id=f"intent_{n}",
                    rider_id=RIDER,
                    dispute_id=f"dsp_{n}",
                    decision_id=f"dec_{n}",
                    idempotency_key=f"payout:dsp_{n}:1",
                    request_fingerprint="fp",
                    amount=AMOUNT,
                    provider_reference=reference_for(n),
                    status="queued",
                    next_attempt_at=due,
                )
            )
    return database


@pytest.fixture
def make_database(tmp_path: Path) -> Any:
    created: list[Database] = []

    def factory(*, intents: int = 1) -> Database:
        database = build_database(tmp_path / f"outbox_{len(created)}.db", intents=intents)
        created.append(database)
        return database

    try:
        yield factory
    finally:
        for database in created:
            database.dispose()


def make_worker(database: Database, fake: FakePaySwift, **overrides: Any) -> PayoutOutboxWorker:
    settings = Settings(
        _env_file=None,
        app_env="test",
        llm_provider="fake",
        **{"worker_batch_size": 10, **overrides},
    )
    client = PaySwiftClient(
        settings,
        client=httpx.Client(
            base_url="http://payswift.test", transport=httpx.MockTransport(fake.handler)
        ),
    )
    return PayoutOutboxWorker(database, settings, client)


def install_clock(worker: PayoutOutboxWorker) -> FakeClock:
    clock = FakeClock()
    worker.clock = clock
    return clock


def aware(value: datetime | None) -> datetime:
    assert value is not None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def snapshot(database: Database, intent_id: str = "intent_1") -> dict[str, Any]:
    with database.session() as session:
        intent = session.get(PayoutIntent, intent_id)
        assert intent is not None
        attempts = session.scalars(
            select(PayoutAttempt)
            .where(PayoutAttempt.intent_id == intent_id)
            .order_by(PayoutAttempt.attempt_number)
        ).all()
        ops = session.scalars(select(OpsItem).order_by(OpsItem.logical_key)).all()
        dispute = session.get(Dispute, intent.dispute_id)
        events = session.scalars(select(TraceEvent).order_by(TraceEvent.id)).all()
        return {
            "status": intent.status,
            "attempt_count": intent.attempt_count,
            "provider_payout_id": intent.provider_payout_id,
            "last_error": intent.last_error,
            "lease_until": intent.lease_until,
            "next_attempt_at": intent.next_attempt_at,
            "dispute_state": dispute.state if dispute else None,
            "attempts": [(a.attempt_number, a.outcome, a.error_code) for a in attempts],
            "ops_items": [(o.item_type, o.logical_key, o.status) for o in ops],
            "ops_reasons": [o.reason for o in ops],
            "trace": [(e.event_type, e.name) for e in events],
        }


def statuses(database: Database) -> dict[str, str]:
    with database.session() as session:
        rows = session.scalars(select(PayoutIntent).order_by(PayoutIntent.intent_id)).all()
        return {row.intent_id: row.status for row in rows}


def trace_events(database: Database, name: str) -> list[TraceEvent]:
    with database.session() as session:
        return session.scalars(
            select(TraceEvent).where(TraceEvent.name == name).order_by(TraceEvent.id)
        ).all()


def heartbeat_age_seconds(database: Database) -> float:
    with database.session() as session:
        row = session.get(WorkerHeartbeat, WORKER_NAME)
        assert row is not None
        return (datetime.now(UTC) - aware(row.updated_at)).total_seconds()


# --------------------------------------------------------------------------- response matrix


def test_201_confirms_intent_and_writes_audit_row_before_the_post(make_database: Any) -> None:
    database = make_database()
    fake = FakePaySwift()
    seen_during_post: list[list[tuple[int, str]]] = []

    def on_post(_request: httpx.Request) -> None:
        with database.session() as session:
            rows = session.scalars(
                select(PayoutAttempt).where(PayoutAttempt.intent_id == "intent_1")
            ).all()
            seen_during_post.append([(row.attempt_number, row.outcome) for row in rows])

    fake.on_post = on_post
    worker = make_worker(database, fake)

    assert worker.tick() == 1

    snap = snapshot(database)
    assert snap["status"] == "confirmed"
    assert snap["provider_payout_id"] == "pout_001"
    assert snap["dispute_state"] == "paid"
    assert snap["lease_until"] is None
    assert snap["attempts"] == [(1, "processed", None)]
    # The audit row is committed *before* the network call and updated afterwards.
    assert seen_during_post == [[(1, "sent")]]
    assert len(fake.posts) == 1 and fake.gets == 1
    assert fake.posts[0] == {
        "key": "payout:dsp_1:1",
        "body": {"rider_id": RIDER, "amount": AMOUNT, "reference": reference_for(1)},
    }
    assert ("tool_call", "payswift_reconciliation") in snap["trace"]
    # Confirmed intents are never picked up again.
    assert worker.tick() == 0
    assert len(fake.posts) == 1


def test_same_key_same_body_replay_returns_200_and_confirms(make_database: Any) -> None:
    database = make_database()
    # The first response is lost after the sandbox stored the payout, and the ledger is not
    # (yet) showing it: the only safe move is to replay the *same* key and body.
    fake = FakePaySwift(script=["timeout_after_write"], ledger_visible=False)
    worker = make_worker(database, fake)
    clock = install_clock(worker)

    assert worker.tick() == 1
    snap = snapshot(database)
    assert snap["status"] == "ambiguous"
    assert snap["attempt_count"] == 1
    assert snap["last_error"] == "ReadTimeout"

    clock.advance(60)
    assert worker.tick() == 1
    snap = snapshot(database)
    assert snap["status"] == "confirmed"
    assert snap["provider_payout_id"] == "pout_001"
    assert fake.post_statuses == ["timeout", 200]
    assert len(fake.posts) == 2
    assert fake.posts[0] == fake.posts[1]
    assert [a[1] for a in snap["attempts"]] == ["transport_ambiguous", "processed"]
    assert len(fake.ledger) == 1  # one payout, not two


def test_400_is_terminal_with_one_escalation_error_trace_and_escalated_dispute(
    make_database: Any,
) -> None:
    database = make_database()
    fake = FakePaySwift(script=["400", "400"])
    worker = make_worker(database, fake)
    clock = install_clock(worker)

    assert worker.tick() == 1
    clock.advance(120)
    assert worker.tick() == 0  # failed intents are retained but never re-claimed on their own

    snap = snapshot(database)
    assert snap["status"] == "failed"
    assert snap["attempt_count"] == 1
    assert snap["last_error"] == "http_400"
    assert snap["lease_until"] is None
    assert len(fake.posts) == 1
    assert snap["attempts"] == [(1, "validation_error", "http_400")]
    assert snap["dispute_state"] == "escalated"
    assert snap["ops_items"] == [("escalation", "provider-failure:intent_1", "pending")]
    assert snap["ops_reasons"] == [
        "PaySwift http_400 after 1 attempt(s); intent intent_1 retained for operator retry"
    ]
    with database.session() as session:
        item = session.scalar(select(OpsItem))
        assert item is not None and item.amount is None and item.decision_id == "dec_1"

    errors = trace_events(database, "payswift_failure")
    assert len(errors) == 1
    assert errors[0].event_type == "error"
    assert errors[0].dispute_id == "dsp_1"
    assert errors[0].input_payload == {"intent_id": "intent_1", "attempt": 1}
    assert errors[0].output_payload["error_code"] == "http_400"
    assert errors[0].output_payload["outcome"] == "validation_error"
    assert errors[0].output_payload["terminal"] is True
    decisions = trace_events(database, "provider_failure_escalation")
    assert len(decisions) == 1
    assert decisions[0].event_type == "decision"
    assert decisions[0].output_payload["logical_key"] == "provider-failure:intent_1"
    assert decisions[0].output_payload["dispute_state"] == "escalated"
    assert snap["trace"].index(("error", "payswift_failure")) < snap["trace"].index(
        ("decision", "provider_failure_escalation")
    )


def test_409_idempotency_key_reused_is_terminal(make_database: Any) -> None:
    database = make_database()
    fake = FakePaySwift(script=["409_reused", "409_reused"])
    worker = make_worker(database, fake)
    clock = install_clock(worker)

    assert worker.tick() == 1
    clock.advance(120)
    assert worker.tick() == 0

    snap = snapshot(database)
    assert snap["status"] == "failed"
    assert snap["last_error"] == "idempotency_key_reused"
    assert snap["attempts"] == [(1, "idempotency_conflict", "idempotency_key_reused")]
    assert len(fake.posts) == 1
    assert snap["dispute_state"] == "escalated"
    assert snap["ops_items"] == [("escalation", "provider-failure:intent_1", "pending")]
    assert snap["ops_reasons"][0].startswith("PaySwift idempotency_key_reused after 1 attempt(s)")


def test_409_request_in_progress_is_ambiguous_then_confirmed_from_ledger(
    make_database: Any,
) -> None:
    database = make_database()
    fake = FakePaySwift(script=["409_in_progress"])
    worker = make_worker(database, fake)
    clock = install_clock(worker)

    assert worker.tick() == 1
    snap = snapshot(database)
    assert snap["status"] == "ambiguous"
    assert snap["last_error"] == "http_409"
    assert snap["attempts"] == [(1, "conflict_ambiguous", "http_409")]
    assert fake.gets == 2  # reconcile before submit and again after the ambiguous result

    fake.reveal()  # the twin request completed
    clock.advance(60)
    assert worker.tick() == 1
    snap = snapshot(database)
    assert snap["status"] == "confirmed"
    assert snap["provider_payout_id"] == "pout_001"
    assert snap["dispute_state"] == "paid"
    assert len(fake.posts) == 1  # no new key, no second submission
    confirmations = trace_events(database, "payswift_reconciliation")
    assert confirmations[-1].output_payload["reason"] == "reconciled_before_submit"


@pytest.mark.parametrize(
    ("retry_after", "expected_cooldown"),
    [(None, 60.0), ("120", 120.0), ("5", 60.0)],
)
def test_429_pauses_the_outbox_without_consuming_the_attempt_budget(
    make_database: Any, retry_after: str | None, expected_cooldown: float
) -> None:
    database = make_database(intents=2)
    headers = {"Retry-After": retry_after} if retry_after else {}

    def rate_limited(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429, json=error_json("account_blocked", "Too many requests."), headers=headers
        )

    fake = FakePaySwift(script=[rate_limited])
    worker = make_worker(database, fake)
    clock = install_clock(worker)

    assert worker.tick() == 1  # intent_1 hit the 429; intent_2 was not even attempted
    assert len(fake.posts) == 1
    snap = snapshot(database)
    assert snap["status"] == "retry"
    assert snap["attempt_count"] == 0  # budget refunded
    assert snap["last_error"] == "http_429"
    assert snap["attempts"] == [(1, "rate_limited", "http_429")]
    delay = (aware(snap["next_attempt_at"]) - clock.now).total_seconds()
    assert delay == pytest.approx(expected_cooldown, abs=0.001)
    assert worker.blocked_until == clock.now + timedelta(seconds=expected_cooldown)
    assert statuses(database)["intent_2"] == "queued"

    # Inside the cooldown nothing is sent, not even the other due intent.
    clock.advance(expected_cooldown - 1)
    assert worker.tick() == 0
    assert len(fake.posts) == 1

    # After the cooldown both go through; intent_1 replays the same key and body.
    clock.advance(2)
    assert worker.blocked_until is None
    assert worker.tick() == 2
    assert len(fake.posts) == 3
    assert fake.posts[0] == fake.posts[1]
    assert statuses(database) == {"intent_1": "confirmed", "intent_2": "confirmed"}
    snap = snapshot(database)
    assert snap["attempt_count"] == 1  # only the successful call counted
    assert snap["attempts"] == [(1, "rate_limited", "http_429"), (2, "processed", None)]


def test_429_never_exhausts_the_retry_budget(make_database: Any) -> None:
    database = make_database()
    fake = FakePaySwift(script=["429"] * 8)
    worker = make_worker(database, fake, payswift_max_retry_attempts=2)
    clock = install_clock(worker)

    for _ in range(8):
        assert worker.tick() == 1
        clock.advance(61)

    snap = snapshot(database)
    assert snap["status"] == "retry"
    assert snap["attempt_count"] == 0
    assert snap["ops_items"] == []
    assert len(fake.posts) == 8
    # One rate-limit error event per attempt, none terminal.
    errors = trace_events(database, "payswift_failure")
    assert len(errors) == 8
    assert all(event.output_payload["terminal"] is False for event in errors)


def test_503_reschedules_with_jittered_exponential_backoff(make_database: Any) -> None:
    database = make_database()
    fake = FakePaySwift(script=["503"] * 3)
    worker = make_worker(
        database, fake, payswift_retry_base_seconds=10, payswift_retry_max_seconds=30
    )
    worker.rng = random.Random(7)
    clock = install_clock(worker)

    ceilings = [10.0, 20.0, 30.0]
    observed: list[float] = []
    for attempt, ceiling in enumerate(ceilings, start=1):
        assert worker.tick() == 1
        snap = snapshot(database)
        assert snap["status"] == "ambiguous"  # a 5xx may have moved money
        assert snap["attempt_count"] == attempt
        assert snap["lease_until"] is None
        delay = (aware(snap["next_attempt_at"]) - clock.now).total_seconds()
        assert 0.5 * ceiling <= delay <= ceiling
        observed.append(delay)
        clock.advance(ceiling)

    assert observed != ceilings  # jitter actually applied
    assert len(fake.posts) == 3
    assert fake.gets == 6  # ledger checked before every submit and after every ambiguous result
    assert snap["attempts"] == [(n, "provider_retryable", "http_503") for n in (1, 2, 3)]
    errors = trace_events(database, "payswift_failure")
    assert [event.input_payload["attempt"] for event in errors] == [1, 2, 3]
    assert all(event.output_payload["terminal"] is False for event in errors)


def test_retry_after_header_is_honoured_on_503(make_database: Any) -> None:
    database = make_database()
    http_date = format_datetime(datetime.now(UTC) + timedelta(seconds=100), usegmt=True)

    def with_header(value: str) -> Callable[[httpx.Request], httpx.Response]:
        def respond(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                503, json=error_json("service_unavailable"), headers={"Retry-After": value}
            )

        return respond

    fake = FakePaySwift(script=[with_header("25"), with_header(http_date)])
    worker = make_worker(database, fake)  # default base 0.5 s: jitter alone would be <= 0.5 s
    clock = install_clock(worker)

    assert worker.tick() == 1
    first = (aware(snapshot(database)["next_attempt_at"]) - clock.now).total_seconds()
    assert first == pytest.approx(25.0, abs=0.001)

    clock.advance(30)
    assert worker.tick() == 1
    second = (aware(snapshot(database)["next_attempt_at"]) - clock.now).total_seconds()
    assert 90 <= second <= 100


def test_retry_budget_exhaustion_escalates_once_and_retains_the_intent(
    make_database: Any,
) -> None:
    database = make_database()
    fake = FakePaySwift(script=["503"] * 10)
    worker = make_worker(database, fake, payswift_max_retry_attempts=3)
    clock = install_clock(worker)

    for _ in range(6):
        worker.tick()
        clock.advance(60)

    snap = snapshot(database)
    assert snap["status"] == "failed"
    assert snap["attempt_count"] == 3
    assert len(fake.posts) == 3
    assert snap["dispute_state"] == "escalated"
    assert snap["ops_items"] == [("escalation", "provider-failure:intent_1", "pending")]
    assert snap["ops_reasons"] == [
        "PaySwift http_503 after 3 attempt(s); intent intent_1 retained for operator retry"
    ]
    errors = trace_events(database, "payswift_failure")
    assert [event.output_payload["terminal"] for event in errors] == [False, False, True]
    assert len(trace_events(database, "provider_failure_escalation")) == 1


def test_504_lost_response_is_confirmed_from_ledger_with_one_post(make_database: Any) -> None:
    database = make_database()
    fake = FakePaySwift(script=["lost"])
    worker = make_worker(database, fake)

    assert worker.tick() == 1
    assert worker.tick() == 0

    snap = snapshot(database)
    assert snap["status"] == "confirmed"
    assert snap["provider_payout_id"] == "pout_001"
    assert snap["dispute_state"] == "paid"
    assert snap["attempts"] == [(1, "provider_retryable", "http_504")]
    assert len(fake.posts) == 1
    assert len(fake.ledger) == 1
    confirmations = trace_events(database, "payswift_reconciliation")
    assert confirmations[0].output_payload["reason"] == "reconciled_after_ambiguous_result"


def test_different_payout_with_same_amount_is_not_mistaken_for_ours(make_database: Any) -> None:
    database = make_database()
    other = {
        "payout_id": "pout_other",
        "rider_id": RIDER,
        "amount": AMOUNT,
        "reference": "qd:R001:surge:2026-09-19:cafebabe0001:day:2026-09-21",
        "status": "processed",
    }
    pending_ours = {
        "payout_id": "pout_pending",
        "rider_id": RIDER,
        "amount": AMOUNT,
        "reference": reference_for(1),
        "status": "pending",
    }
    fake = FakePaySwift(ledger=[other, pending_ours])
    worker = make_worker(database, fake)

    assert worker.tick() == 1

    snap = snapshot(database)
    assert snap["status"] == "confirmed"
    assert snap["provider_payout_id"] == "pout_001"  # a new payout was (correctly) submitted
    assert len(fake.posts) == 1


# --------------------------------------------------------------------------- crash recovery


def test_crash_between_post_and_confirm_recovers_with_zero_extra_posts(
    make_database: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = make_database()
    fake = FakePaySwift()
    worker = make_worker(database, fake)
    clock = install_clock(worker)
    original_confirm = worker._confirm
    reasons: list[str] = []

    def crashing_confirm(intent_id: str, provider_payout_id: str | None, reason: str) -> None:
        reasons.append(reason)
        if len(reasons) == 1:
            raise RuntimeError("simulated worker crash before local persistence")
        original_confirm(intent_id, provider_payout_id, reason)

    monkeypatch.setattr(worker, "_confirm", crashing_confirm)

    assert worker.tick() == 1  # the exception is contained
    snap = snapshot(database)
    assert snap["status"] == "ambiguous"  # never stranded in ``processing``
    assert snap["lease_until"] is None
    assert snap["last_error"] == "RuntimeError"
    assert snap["attempts"] == [(1, "processed", None)]
    assert len(fake.posts) == 1 and len(fake.ledger) == 1
    errors = trace_events(database, "payswift_failure")
    assert errors[0].output_payload == {
        "error_code": "RuntimeError",
        "outcome": "worker_error",
        "terminal": False,
        "status": "ambiguous",
        "next_attempt_at": aware(snap["next_attempt_at"]).isoformat(),
    }

    clock.advance(60)
    assert worker.tick() == 1
    snap = snapshot(database)
    assert snap["status"] == "confirmed"
    assert snap["provider_payout_id"] == "pout_001"
    assert snap["dispute_state"] == "paid"
    assert len(fake.posts) == 1  # recovered from the ledger, no second submission
    assert reasons == ["provider_processed", "reconciled_before_submit"]


def test_processing_row_with_expired_lease_is_reclaimed_without_a_new_post(
    make_database: Any,
) -> None:
    database = make_database()
    moved = {
        "payout_id": "pout_prev",
        "rider_id": RIDER,
        "amount": AMOUNT,
        "reference": reference_for(1),
        "status": "processed",
    }
    fake = FakePaySwift(ledger=[moved])  # money moved before the previous process was killed
    worker = make_worker(database, fake, worker_job_lease_seconds=30)
    clock = install_clock(worker)
    with database.session() as session, session.begin():
        intent = session.get(PayoutIntent, "intent_1")
        assert intent is not None
        intent.status = "processing"
        intent.attempt_count = 1
        intent.lease_until = clock.now + timedelta(seconds=30)

    assert worker.tick() == 0  # lease still held by the (dead) previous holder
    assert snapshot(database)["status"] == "processing"

    clock.advance(31)
    assert worker.tick() == 1
    snap = snapshot(database)
    assert snap["status"] == "confirmed"
    assert snap["provider_payout_id"] == "pout_prev"
    assert snap["dispute_state"] == "paid"
    assert len(fake.posts) == 0
    assert fake.gets == 1


def test_worker_error_before_the_post_is_rescheduled_not_stranded(
    make_database: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = make_database()
    fake = FakePaySwift()
    worker = make_worker(database, fake)
    clock = install_clock(worker)

    def broken_begin_attempt(_intent_id: str) -> str:
        raise OSError("database connection dropped")

    monkeypatch.setattr(worker, "_begin_attempt", broken_begin_attempt)
    assert worker.tick() == 1
    snap = snapshot(database)
    assert snap["status"] == "ambiguous"
    assert snap["last_error"] == "OSError"
    assert snap["attempt_count"] == 0  # no POST happened, no budget consumed
    assert len(fake.posts) == 0

    monkeypatch.undo()
    clock.advance(60)
    assert worker.tick() == 1
    assert snapshot(database)["status"] == "confirmed"
    assert len(fake.posts) == 1


def test_non_json_ledger_body_does_not_strand_the_intent(make_database: Any) -> None:
    database = make_database()
    fake = FakePaySwift(script=["503"])
    fake.get_response = lambda _request: httpx.Response(200, text="<html>maintenance</html>")
    worker = make_worker(database, fake)
    clock = install_clock(worker)

    assert worker.tick() == 1  # JSONDecodeError (a ValueError) is contained
    snap = snapshot(database)
    assert snap["status"] == "ambiguous"
    assert snap["attempt_count"] == 1
    assert snap["lease_until"] is None
    assert aware(snap["next_attempt_at"]) > clock.now
    assert fake.gets == 2

    fake.get_response = None  # ledger readable again
    clock.advance(60)
    assert worker.tick() == 1
    snap = snapshot(database)
    assert snap["status"] == "confirmed"
    assert len(fake.posts) == 2
    assert fake.posts[0] == fake.posts[1]


# --------------------------------------------------------------------------- operator requeue


def test_requeue_resets_failed_intent_and_next_tick_posts_once(make_database: Any) -> None:
    database = make_database()
    fake = FakePaySwift(script=["400"])
    worker = make_worker(database, fake)

    assert worker.tick() == 1
    assert snapshot(database)["status"] == "failed"

    with database.session() as session, session.begin():
        assert worker.requeue(session, "intent_1", reviewer="ops@quickdrop") is True
        assert worker.requeue(session, "intent_1", reviewer="ops@quickdrop") is False
        assert worker.requeue(session, "intent_missing", reviewer="ops@quickdrop") is False

    snap = snapshot(database)
    assert snap["status"] == "queued"
    assert snap["attempt_count"] == 0
    assert snap["last_error"] is None
    assert snap["lease_until"] is None
    assert snap["dispute_state"] == "payout_processing"
    requeued = trace_events(database, "provider_failure_requeued")
    assert len(requeued) == 1
    assert requeued[0].event_type == "decision"
    assert requeued[0].input_payload == {"intent_id": "intent_1", "reviewer": "ops@quickdrop"}
    assert len(fake.posts) == 1

    assert worker.tick() == 1
    snap = snapshot(database)
    assert snap["status"] == "confirmed"
    assert snap["dispute_state"] == "paid"
    assert len(fake.posts) == 2
    assert fake.posts[0] == fake.posts[1]  # same key, same body after the operator retry
    assert snap["attempts"] == [(1, "validation_error", "http_400"), (2, "processed", None)]
    assert snap["attempt_count"] == 1


def test_requeue_rejects_non_failed_intents(make_database: Any) -> None:
    database = make_database()
    with database.session() as session, session.begin():
        assert requeue_failed_intent(session, "intent_1", "ops@quickdrop") is False
    snap = snapshot(database)
    assert snap["status"] == "queued"
    assert snap["trace"] == []


def test_failure_after_requeue_escalates_again_only_when_previous_item_is_closed(
    make_database: Any,
) -> None:
    database = make_database()
    fake = FakePaySwift(script=["400", "400", "400"])
    worker = make_worker(database, fake)

    assert worker.tick() == 1
    assert snapshot(database)["ops_items"] == [
        ("escalation", "provider-failure:intent_1", "pending")
    ]

    # Requeue while the escalation is still pending: a second failure must not duplicate it.
    with database.session() as session, session.begin():
        assert requeue_failed_intent(session, "intent_1", reviewer="ops@quickdrop") is True
    assert worker.tick() == 1
    assert snapshot(database)["ops_items"] == [
        ("escalation", "provider-failure:intent_1", "pending")
    ]

    # Operator closes the escalation and retries again: a fresh pending item is required.
    with database.session() as session, session.begin():
        item = session.scalar(select(OpsItem))
        assert item is not None
        item.status = "retried"
        assert requeue_failed_intent(session, "intent_1", reviewer="ops@quickdrop") is True
    assert worker.tick() == 1
    snap = snapshot(database)
    assert snap["status"] == "failed"
    assert snap["ops_items"] == [
        ("escalation", "provider-failure:intent_1", "retried"),
        ("escalation", "provider-failure:intent_1:2", "pending"),
    ]
    assert len(fake.posts) == 3
    assert [a[0] for a in snap["attempts"]] == [1, 2, 3]


# --------------------------------------------------------------------------- worker process


def test_heartbeat_thread_keeps_beating_while_a_tick_is_blocked(make_database: Any) -> None:
    database = make_database()
    fake = FakePaySwift()
    ages: list[float] = []

    def slow_post(_request: httpx.Request) -> None:
        for _ in range(4):  # the provider takes ~2 s to answer
            time.sleep(0.5)
            ages.append(heartbeat_age_seconds(database))

    fake.on_post = slow_post
    worker = make_worker(database, fake)
    stop = Event()
    thread = start_heartbeat_thread(database, stop, interval_seconds=0.1)
    try:
        assert worker.tick() == 1
    finally:
        stop.set()
        thread.join(timeout=2)

    assert not thread.is_alive()
    assert len(ages) == 4
    assert max(ages) < 1.0  # far below the 20 s TTL although the tick blocked for ~2 s
    assert snapshot(database)["status"] == "confirmed"


def test_tick_checks_should_stop_between_intents(make_database: Any) -> None:
    database = make_database(intents=3)
    fake = FakePaySwift()
    stop = Event()
    fake.on_post = lambda _request: stop.set()  # SIGTERM lands while the first intent is in flight
    worker = make_worker(database, fake)

    assert worker.tick(should_stop=stop.is_set) == 1

    assert len(fake.posts) == 1
    assert statuses(database) == {
        "intent_1": "confirmed",
        "intent_2": "queued",
        "intent_3": "queued",
    }


def test_outbox_loop_drains_queue_and_stops_on_signal(make_database: Any) -> None:
    database = make_database(intents=3)
    fake = FakePaySwift()
    worker = make_worker(database, fake, worker_batch_size=1)
    stop = Event()

    def stop_when_drained(_request: httpx.Request) -> None:
        if len(fake.posts) == 3:
            stop.set()

    fake.on_post = stop_when_drained
    run_outbox_loop(worker, stop, poll_interval_seconds=0.01)

    assert len(fake.posts) == 3
    assert set(statuses(database).values()) == {"confirmed"}


def test_outbox_loop_logs_tick_failure_and_keeps_running(caplog: pytest.LogCaptureFixture) -> None:
    stop = Event()
    calls: list[int] = []

    class FlakyOutbox:
        def tick(self, *, should_stop: Callable[[], bool] | None = None) -> int:
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("database hiccup")
            stop.set()
            return 0

    with caplog.at_level(logging.ERROR, logger="app.worker"):
        run_outbox_loop(FlakyOutbox(), stop, poll_interval_seconds=0.01)  # type: ignore[arg-type]

    assert len(calls) == 2
    messages = [record.getMessage() for record in caplog.records]
    assert "worker_tick_failed" in messages
    assert "worker_heartbeat_failed" not in messages
