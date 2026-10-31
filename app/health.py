from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select, text

from app.db import Database
from app.models import ImportBatch, WorkerHeartbeat


@dataclass(frozen=True, slots=True)
class ReadinessState:
    ready: bool
    database: str
    data: str
    worker: str
    reason: str | None = None


class ReadinessProbe(Protocol):
    def check(self) -> ReadinessState: ...


class DatabaseReadinessProbe:
    def __init__(self, database: Database, worker_name: str, heartbeat_ttl_seconds: float) -> None:
        self._database = database
        self._worker_name = worker_name
        self._heartbeat_ttl_seconds = heartbeat_ttl_seconds

    def check(self) -> ReadinessState:
        try:
            with self._database.session() as session:
                session.execute(text("SELECT 1"))
                heartbeat = session.scalar(
                    select(WorkerHeartbeat).where(WorkerHeartbeat.worker_name == self._worker_name)
                )
                latest_import = session.scalar(
                    select(ImportBatch).order_by(ImportBatch.started_at.desc()).limit(1)
                )
        except Exception:
            return ReadinessState(
                ready=False,
                database="unavailable",
                data="unknown",
                worker="unknown",
                reason="database_unavailable",
            )

        if latest_import is None:
            return ReadinessState(
                ready=False,
                database="ready",
                data="missing",
                worker="unknown",
                reason="source_import_missing",
            )
        if latest_import.status not in {"completed", "completed_with_escalations"}:
            return ReadinessState(
                ready=False,
                database="ready",
                data=latest_import.status,
                worker="unknown",
                reason="source_import_not_ready",
            )

        if heartbeat is None:
            return ReadinessState(
                ready=False,
                database="ready",
                data="ready",
                worker="missing",
                reason="worker_heartbeat_missing",
            )

        updated_at = heartbeat.updated_at
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=UTC)
        age_seconds = (datetime.now(UTC) - updated_at).total_seconds()
        if age_seconds > self._heartbeat_ttl_seconds:
            return ReadinessState(
                ready=False,
                database="ready",
                data="ready",
                worker="stale",
                reason="worker_heartbeat_stale",
            )

        return ReadinessState(ready=True, database="ready", data="ready", worker="ready")
