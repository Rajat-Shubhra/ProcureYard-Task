"""Black-box HTTP contract tests for docs/API_CONTRACT.md (``api-contract-v1``).

The app is built with ``create_app`` against a SQLite copy of the imported source data,
a stubbed readiness probe and an empty PaySwift ledger, so every test exercises the real
middleware, exception handlers and routes without network access.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from app.application import create_app
from app.config import Settings
from app.db import Database
from app.errors import RetryableProcessingError
from app.health import ReadinessState
from app.ingestion.service import ImportService
from app.models import Base
from app.ops.service import OpsService

ROOT = Path(__file__).resolve().parents[1]
TRACE_HEADER = "x-trace-id"
FROZEN_CODES = {
    "invalid_json",
    "validation_error",
    "not_found",
    "conflict",
    "service_not_ready",
    "database_unavailable",
    "internal_error",
}
# The envelope example code; used for client errors outside the frozen list (e.g. 405).
FALLBACK_CODE = "invalid_request"
TRACE_TYPES = {"message_in", "tool_call", "decision", "reply", "error"}
DB_DOWN_URL = "postgresql+psycopg://u:p@127.0.0.1:1/db"

READY = ReadinessState(ready=True, database="ready", data="ready", worker="ready")
WORKER_MISSING = ReadinessState(
    ready=False,
    database="ready",
    data="ready",
    worker="missing",
    reason="worker_heartbeat_missing",
)

VALID_MESSAGE = {
    "message_id": "wamid.HTTPCONTRACT01",
    "rider_id": "R003",
    "text": "Bhai order T926334 ka surge nahi mila, 20 tarikh wala",
    "received_at": "2026-09-22T09:05:00+05:30",
}
APPROVAL_MESSAGE = {
    "message_id": "wamid.HTTPAPPROVAL01",
    "rider_id": "R016",
    "text": "19 sept ke 5 orders ka paisa hi nahi aaya!! jaldi karo",
    "received_at": "2026-09-22T11:30:00+05:30",
}
REVIEW = {"reviewer": "ops-user", "note": "contract test"}


class StubProbe:
    def __init__(self, state: ReadinessState) -> None:
        self.state = state

    def check(self) -> ReadinessState:
        return self.state


class EmptyLedger:
    def list_payouts(self, _rider_id: str) -> list[dict[str, object]]:
        return []


@pytest.fixture(scope="module")
def source_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("http-contract") / "source.db"
    database = Database(f"sqlite+pysqlite:///{path}")
    try:
        Base.metadata.create_all(database.engine)
        ImportService(
            database,
            snapshot_version="assignment-export-v1",
            normalization_version="source-normalization-v1",
        ).import_directory(ROOT / "data")
    finally:
        database.dispose()
    return path


@contextmanager
def http_client(
    database_url: str, *, readiness: ReadinessState = READY, **settings_overrides: Any
) -> Iterator[TestClient]:
    database = Database(database_url)
    settings = Settings(
        _env_file=None,
        app_env="test",
        llm_provider="fake",
        database_url=database_url,
        **settings_overrides,
    )
    app = create_app(
        settings,
        database=database,
        readiness_probe=StubProbe(readiness),
        payswift_client=EmptyLedger(),  # type: ignore[arg-type]
    )
    try:
        with TestClient(app) as client:
            yield client
    finally:
        database.dispose()


@pytest.fixture()
def client(source_database: Path, tmp_path: Path) -> Iterator[TestClient]:
    database_path = tmp_path / "http.db"
    shutil.copyfile(source_database, database_path)
    with http_client(f"sqlite+pysqlite:///{database_path}") as test_client:
        yield test_client


def fastapi_app(client: TestClient) -> FastAPI:
    return cast(FastAPI, client.app)


def assert_envelope(response: Any, *, status: int, code: str, retryable: bool) -> dict[str, Any]:
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith("application/json")
    error = response.json()["error"]
    assert error["code"] == code
    assert error["code"] in FROZEN_CODES | {FALLBACK_CODE}
    assert error["retryable"] is retryable
    assert isinstance(error["message"], str) and error["message"]
    assert isinstance(error["trace_id"], str) and error["trace_id"]
    assert response.headers[TRACE_HEADER] == error["trace_id"]
    assert "Traceback" not in response.text
    return error


def create_pending_approval(client: TestClient) -> dict[str, Any]:
    response = client.post("/messages", json=APPROVAL_MESSAGE)
    assert response.status_code == 200, response.text
    pending = client.get("/ops/pending")
    assert pending.status_code == 200
    items = [
        item
        for item in pending.json()
        if item["type"] == "approval" and item["rider_id"] == APPROVAL_MESSAGE["rider_id"]
    ]
    assert items, pending.json()
    return items[0]


# --- GET /health -----------------------------------------------------------------------


def test_health_ready_returns_contract_body(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {
        "status": "ok",
        "database": "ready",
        "data": "ready",
        "worker": "ready",
    }
    assert response.headers[TRACE_HEADER].startswith("trace_")


def test_health_not_ready_uses_error_envelope_with_trace_header(tmp_path: Path) -> None:
    with http_client(
        f"sqlite+pysqlite:///{tmp_path / 'empty.db'}", readiness=WORKER_MISSING
    ) as client:
        response = client.get("/health")

    assert_envelope(response, status=503, code="service_not_ready", retryable=True)
    assert response.json()["worker"] == "missing"
    assert response.json()["reason"] == "worker_heartbeat_missing"


# --- POST /messages validation ---------------------------------------------------------


@pytest.mark.parametrize("body", [b"{not json", b"", b'{"message_id": }', b"{"])
def test_invalid_json_body_returns_400(client: TestClient, body: bytes) -> None:
    response = client.post("/messages", content=body, headers={"content-type": "application/json"})

    assert_envelope(response, status=400, code="invalid_json", retryable=False)


def test_json_body_that_is_not_an_object_returns_422(client: TestClient) -> None:
    response = client.post("/messages", json=["not", "an", "object"])

    assert_envelope(response, status=422, code="validation_error", retryable=False)


@pytest.mark.parametrize("missing", ["message_id", "rider_id", "text", "received_at"])
def test_missing_field_returns_422(client: TestClient, missing: str) -> None:
    payload = {key: value for key, value in VALID_MESSAGE.items() if key != missing}

    response = client.post("/messages", json=payload)

    assert_envelope(response, status=422, code="validation_error", retryable=False)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("text", "   "),
        ("text", ""),
        ("text", "\n\t "),
        ("text", "x" * 4001),
        ("text", 123),
        ("message_id", "   "),
        ("message_id", ""),
        ("message_id", None),
        ("rider_id", "   "),
        ("rider_id", ""),
        ("rider_id", 7),
    ],
)
def test_blank_or_non_string_field_returns_422(
    client: TestClient, field: str, value: object
) -> None:
    response = client.post("/messages", json={**VALID_MESSAGE, field: value})

    assert_envelope(response, status=422, code="validation_error", retryable=False)


@pytest.mark.parametrize(
    "received_at",
    [
        "2026-09-22T09:05:00",  # naive
        "2026-09-22 09:05",  # naive, space separated
        1758510300,  # epoch integer
        1758510300.5,
        "not-a-timestamp",
        "",
        None,
        True,
        {"$date": "2026-09-22T09:05:00+05:30"},
    ],
)
def test_received_at_must_be_a_timezone_aware_iso_string(
    client: TestClient, received_at: object
) -> None:
    response = client.post("/messages", json={**VALID_MESSAGE, "received_at": received_at})

    assert_envelope(response, status=422, code="validation_error", retryable=False)


def test_text_over_the_configured_limit_returns_422_not_a_retryable_5xx(
    source_database: Path, tmp_path: Path
) -> None:
    # The request model caps text at 4000 characters; a lower configured limit is enforced
    # by the conversation service (ValueError) and must still surface as a client error.
    database_path = tmp_path / "limit.db"
    shutil.copyfile(source_database, database_path)
    with http_client(f"sqlite+pysqlite:///{database_path}", message_max_length=10) as client:
        response = client.post("/messages", json={**VALID_MESSAGE, "text": "x" * 11})

    assert_envelope(response, status=422, code="validation_error", retryable=False)


@pytest.mark.parametrize(
    "received_at",
    ["2026-09-22T03:35:00Z", "2026-09-22T09:05:00+05:30", "2026-09-22T09:05:00.250000+05:30"],
)
def test_timezone_aware_received_at_formats_are_accepted(
    client: TestClient, received_at: str
) -> None:
    payload = {
        **VALID_MESSAGE,
        "message_id": f"wamid.TZ{abs(hash(received_at))}",
        "received_at": received_at,
    }

    response = client.post("/messages", json=payload)

    assert response.status_code == 200, response.text
    assert response.json()["reply"].strip()


def test_unknown_rider_with_unusual_id_gets_a_safe_reply(client: TestClient) -> None:
    payload = {
        **VALID_MESSAGE,
        "message_id": "wamid.UNKNOWNRIDER01",
        "rider_id": "r-unknown_rider-99",
    }

    response = client.post("/messages", json=payload)

    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body["reply"], str) and body["reply"].strip()
    assert body["message_id"] == "wamid.UNKNOWNRIDER01"
    assert response.headers[TRACE_HEADER]
    # No private lookup happened and no conversation was opened for the unknown identity.
    assert client.get("/trace/R-UNKNOWN_RIDER-99").json() == []


def test_duplicate_message_returns_the_stored_reply_without_reprocessing(
    client: TestClient,
) -> None:
    first = client.post("/messages", json=VALID_MESSAGE)
    second = client.post("/messages", json=VALID_MESSAGE)

    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["reply"].strip()
    assert second.json()["reply"] == first.json()["reply"]
    assert second.json()["duplicate"] is True
    events = client.get("/trace/R003").json()
    assert sum(1 for event in events if event["type"] == "message_in") == 1


# --- GET /trace/{rider_id} -------------------------------------------------------------


def test_trace_is_empty_for_unknown_rider_and_ordered_after_processing(
    client: TestClient,
) -> None:
    empty = client.get("/trace/R999")
    assert empty.status_code == 200
    assert empty.json() == []

    # The rider ID is normalised (stripped, upper-cased) before it becomes the identity.
    processed = client.post("/messages", json={**VALID_MESSAGE, "rider_id": " r003 "})
    assert processed.status_code == 200, processed.text
    assert processed.json()["conversation_id"]

    events = client.get("/trace/R003").json()
    assert events
    assert events[0]["type"] == "message_in"
    assert events[-1]["type"] == "reply"
    for event in events:
        assert {"at", "type", "name", "input", "output"} <= set(event)
        assert event["type"] in TRACE_TYPES
        assert isinstance(event["name"], str) and event["name"]
        assert datetime.fromisoformat(event["at"]).tzinfo is not None
    assert events[-1]["output"]["reply"] == processed.json()["reply"]


# --- Error envelope and trace propagation ----------------------------------------------


def test_unknown_route_returns_not_found_envelope(client: TestClient) -> None:
    response = client.get("/definitely-not-a-route")

    assert_envelope(response, status=404, code="not_found", retryable=False)


def test_wrong_method_returns_invalid_request_envelope(client: TestClient) -> None:
    response = client.get("/messages")

    assert_envelope(response, status=405, code=FALLBACK_CODE, retryable=False)
    assert "POST" in response.headers.get("allow", "")


def test_supplied_trace_id_is_echoed_and_unsafe_values_are_replaced(
    client: TestClient,
) -> None:
    echoed = client.get("/health", headers={TRACE_HEADER: "vendor-trace_01:abc"})
    assert echoed.headers[TRACE_HEADER] == "vendor-trace_01:abc"

    error = client.get("/nope", headers={TRACE_HEADER: "vendor-trace_02"})
    assert error.headers[TRACE_HEADER] == "vendor-trace_02"
    assert error.json()["error"]["trace_id"] == "vendor-trace_02"

    for unsafe in ("has spaces and/slashes", "a" * 129, ""):
        replaced = client.get("/health", headers={TRACE_HEADER: unsafe})
        assert replaced.headers[TRACE_HEADER].startswith("trace_")


def test_unexpected_exception_returns_retryable_503_with_trace_header(
    client: TestClient,
) -> None:
    def boom() -> dict[str, object]:
        raise RuntimeError("secret internal detail")

    fastapi_app(client).add_api_route("/__boom", boom, methods=["GET"])

    response = client.get("/__boom", headers={TRACE_HEADER: "trace-from-vendor-1"})

    error = assert_envelope(response, status=503, code="internal_error", retryable=True)
    assert error["trace_id"] == "trace-from-vendor-1"
    assert "secret internal detail" not in response.text


@pytest.mark.parametrize("raised_status", [500, 502, 503])
def test_server_http_exception_normalises_to_retryable_503_internal_error(
    client: TestClient, raised_status: int
) -> None:
    def http_boom() -> dict[str, object]:
        raise HTTPException(status_code=raised_status, detail="internal http detail")

    route_path = f"/__http_boom_{raised_status}"
    fastapi_app(client).add_api_route(route_path, http_boom, methods=["GET"])

    response = client.get(route_path, headers={TRACE_HEADER: f"trace-http-{raised_status}"})

    error = assert_envelope(response, status=503, code="internal_error", retryable=True)
    assert error["trace_id"] == f"trace-http-{raised_status}"
    assert "internal http detail" not in response.text


def test_retryable_processing_error_returns_resume_envelope(client: TestClient) -> None:
    def stuck() -> dict[str, object]:
        raise RetryableProcessingError("provider exploded after durable receipt")

    fastapi_app(client).add_api_route("/__stuck", stuck, methods=["GET"])

    response = client.get("/__stuck")

    error = assert_envelope(response, status=503, code="internal_error", retryable=True)
    assert error["message"] == (
        "Processing did not complete; the message will be resumed on the next delivery."
    )
    assert "exploded" not in response.text


def test_fallback_exception_handler_emits_retryable_503_with_header(
    client: TestClient,
) -> None:
    handler = fastapi_app(client).exception_handlers[Exception]
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/x",
        "headers": [],
        "state": {"trace_id": "trace_fallback"},
    }

    response = asyncio.run(handler(Request(scope), RuntimeError("boom")))

    assert response.status_code == 503
    assert response.headers[TRACE_HEADER] == "trace_fallback"
    assert json.loads(response.body)["error"] == {
        "code": "internal_error",
        "message": "The service could not complete the request.",
        "retryable": True,
        "trace_id": "trace_fallback",
    }


def test_database_unavailable_returns_retryable_503() -> None:
    with http_client(DB_DOWN_URL) as client:
        message = client.post("/messages", json=VALID_MESSAGE)
        pending = client.get("/ops/pending")
        trace = client.get("/trace/R003")

    for response in (message, pending, trace):
        assert_envelope(response, status=503, code="database_unavailable", retryable=True)
        assert "psycopg" not in response.text
        assert "127.0.0.1" not in response.text


# --- Ops endpoints ---------------------------------------------------------------------


def test_ops_pending_lists_items_in_contract_shape(client: TestClient) -> None:
    initial = client.get("/ops/pending")
    assert initial.status_code == 200
    assert initial.json() == []

    item = create_pending_approval(client)

    assert {"id", "rider_id", "type", "amount", "reason", "created_at"} <= set(item)
    assert item["type"] == "approval"
    assert isinstance(item["amount"], int) and item["amount"] > 0
    assert isinstance(item["reason"], str) and item["reason"]
    assert datetime.fromisoformat(item["created_at"]).tzinfo is not None

    page = client.get("/ops")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert item["id"] in page.text


def test_ops_item_page_shows_conversation_trace_and_review_controls(client: TestClient) -> None:
    item = create_pending_approval(client)

    page = client.get(f"/ops/items/{item['id']}")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    # BRIEF: ops can see the conversation and what the agent did, and approve or reject.
    assert "Conversation" in page.text and "What the agent did" in page.text
    assert "vendor_message_received" in page.text and "deterministic_action_gate" in page.text
    assert "review(" in page.text and ">Approve<" in page.text and ">Reject<" in page.text
    assert "Evidence used for this decision" in page.text and "verified_shortfall" in page.text
    assert client.get("/ops/items/ops_missing").status_code == 404

    client.post(f"/ops/items/{item['id']}/reject", json={"reviewer": "ops", "note": "no"})
    closed = client.get(f"/ops/items/{item['id']}")
    assert closed.status_code == 200 and ">Approve<" not in closed.text


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_ops_mutation_on_unknown_item_returns_404(client: TestClient, action: str) -> None:
    response = client.post(f"/ops/items/does-not-exist/{action}", json=REVIEW)

    assert_envelope(response, status=404, code="not_found", retryable=False)


def test_ops_mutation_without_reviewer_returns_422(client: TestClient) -> None:
    response = client.post("/ops/items/does-not-exist/reject", json={"note": "no reviewer"})

    assert_envelope(response, status=422, code="validation_error", retryable=False)


def test_ops_reject_is_idempotent_and_a_later_approval_conflicts(client: TestClient) -> None:
    item = create_pending_approval(client)

    first = client.post(f"/ops/items/{item['id']}/reject", json=REVIEW)
    second = client.post(f"/ops/items/{item['id']}/reject", json=REVIEW)
    approve = client.post(f"/ops/items/{item['id']}/approve", json=REVIEW)

    assert first.status_code == 200, first.text
    assert first.json() == {"id": item["id"], "status": "rejected"}
    assert second.status_code == 200
    assert second.json() == first.json()
    assert_envelope(approve, status=409, code="conflict", retryable=False)
    assert item["id"] not in {pending["id"] for pending in client.get("/ops/pending").json()}


def test_ops_approve_is_idempotent_and_a_later_rejection_conflicts(
    client: TestClient,
) -> None:
    item = create_pending_approval(client)

    first = client.post(f"/ops/items/{item['id']}/approve", json=REVIEW)
    second = client.post(f"/ops/items/{item['id']}/approve", json=REVIEW)
    reject = client.post(f"/ops/items/{item['id']}/reject", json=REVIEW)

    assert first.status_code == 200, first.text
    assert first.json()["id"] == item["id"]
    assert first.json()["status"] == "approved"
    assert first.json()["payout_intent_id"]
    assert second.status_code == 200
    assert second.json() == first.json()
    assert_envelope(reject, status=409, code="conflict", retryable=False)


@pytest.mark.skipif(
    not hasattr(OpsService, "retry_payout"),
    reason="OpsService.retry_payout is being added separately; the route test activates with it",
)
def test_ops_retry_payout_route_maps_results_and_errors(client: TestClient) -> None:
    missing = client.post("/ops/items/does-not-exist/retry-payout", json=REVIEW)
    assert_envelope(missing, status=404, code="not_found", retryable=False)

    item = create_pending_approval(client)
    approved = client.post(f"/ops/items/{item['id']}/approve", json=REVIEW)
    assert approved.status_code == 200, approved.text

    response = client.post(f"/ops/items/{item['id']}/retry-payout", json=REVIEW)

    assert response.status_code in {200, 409}, response.text
    if response.status_code == 200:
        body = response.json()
        assert set(body) == {"id", "status", "payout_intent_id"}
        assert body["id"] == item["id"]
        assert isinstance(body["status"], str) and body["status"]
    else:
        assert_envelope(response, status=409, code="conflict", retryable=False)
