from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy.orm import Session

from app.models import TraceEvent

TraceType = Literal["message_in", "tool_call", "decision", "reply", "error"]


def add_trace_event(
    session: Session,
    *,
    rider_id: str,
    event_type: TraceType,
    name: str,
    input_payload: dict[str, Any] | None,
    output_payload: dict[str, Any] | str | None,
    conversation_id: str | None = None,
    message_id: str | None = None,
    dispute_id: str | None = None,
) -> TraceEvent:
    event = TraceEvent(
        rider_id=rider_id,
        conversation_id=conversation_id,
        message_id=message_id,
        dispute_id=dispute_id,
        event_type=event_type,
        name=name,
        input_payload=input_payload or {},
        output_payload=_json_output(output_payload),
        created_at=datetime.now(UTC),
    )
    session.add(event)
    return event


def _json_output(value: dict[str, Any] | str | None) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if value is None:
        return {"value": None}
    return {"value": value}
