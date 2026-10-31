import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.api.health import health
from app.db import Database
from app.errors import error_response
from app.health import DatabaseReadinessProbe, ReadinessState
from app.models import Base, ImportBatch, WorkerHeartbeat


class StubProbe:
    def __init__(self, state: ReadinessState) -> None:
        self.state = state

    def check(self) -> ReadinessState:
        return self.state


def request_for(state: ReadinessState, trace_id: str = "trace_test") -> Any:
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(readiness_probe=StubProbe(state))),
        state=SimpleNamespace(trace_id=trace_id),
    )


def response_json(response) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    return json.loads(response.body)


def test_health_is_ready_only_when_database_and_worker_are_ready() -> None:
    response = health(
        request_for(ReadinessState(ready=True, database="ready", data="ready", worker="ready"))
    )

    assert response.status_code == 200
    assert response_json(response) == {
        "status": "ok",
        "database": "ready",
        "data": "ready",
        "worker": "ready",
    }


def test_health_returns_contract_error_when_worker_is_missing() -> None:
    response = health(
        request_for(
            ReadinessState(
                ready=False,
                database="ready",
                data="ready",
                worker="missing",
                reason="worker_heartbeat_missing",
            )
        )
    )

    assert response.status_code == 503
    assert response_json(response) == {
        "error": {
            "code": "service_not_ready",
            "message": "The service is not ready to accept work.",
            "retryable": True,
            "trace_id": "trace_test",
        },
        "database": "ready",
        "data": "ready",
        "worker": "missing",
        "reason": "worker_heartbeat_missing",
    }


def test_common_error_envelope_is_safe_and_stable() -> None:
    response = error_response(
        status_code=404,
        code="not_found",
        message="Not found.",
        retryable=False,
        trace_id="trace_test",
    )

    assert response.status_code == 404
    assert response_json(response) == {
        "error": {
            "code": "not_found",
            "message": "Not found.",
            "retryable": False,
            "trace_id": "trace_test",
        }
    }


def test_database_probe_requires_a_completed_source_import(tmp_path: Path) -> None:
    database = Database(f"sqlite+pysqlite:///{tmp_path / 'health.db'}")
    Base.metadata.create_all(database.engine)
    probe = DatabaseReadinessProbe(database, "payout-worker", 5)

    missing = probe.check()
    assert not missing.ready
    assert missing.data == "missing"
    assert missing.reason == "source_import_missing"

    with database.session() as session, session.begin():
        session.add(
            ImportBatch(
                batch_id="import_test",
                source_snapshot_version="test-v1",
                source_digest=f"sha256:{'0' * 64}",
                normalization_version="normalization-v1",
                status="completed",
                counts={},
                completed_at=datetime.now(UTC),
            )
        )
        session.add(WorkerHeartbeat(worker_name="payout-worker", updated_at=datetime.now(UTC)))

    ready = probe.check()
    assert ready == ReadinessState(
        ready=True,
        database="ready",
        data="ready",
        worker="ready",
    )
    database.dispose()
