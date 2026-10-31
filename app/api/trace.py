from __future__ import annotations

from datetime import UTC

from fastapi import APIRouter, Request
from sqlalchemy import select

from app.db import Database
from app.models import TraceEvent

router = APIRouter(tags=["trace"])


@router.get("/trace/{rider_id}")
def rider_trace(rider_id: str, request: Request) -> list[dict[str, object]]:
    database: Database = request.app.state.database
    with database.session() as session:
        events = list(
            session.scalars(
                select(TraceEvent)
                .where(TraceEvent.rider_id == rider_id)
                .order_by(TraceEvent.id, TraceEvent.created_at)
            )
        )
    return [
        {
            "at": _iso(event.created_at),
            "type": event.event_type,
            "name": event.name,
            "input": event.input_payload,
            "output": _output(event.output_payload),
        }
        for event in events
    ]


def _iso(value) -> str:  # type: ignore[no-untyped-def]
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def _output(value: dict[str, object]) -> object:
    if set(value) == {"value"}:
        return value["value"]
    return value
