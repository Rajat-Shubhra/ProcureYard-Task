from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy.orm import Session

from app.models import TraceEvent

TraceType = Literal["message_in", "tool_call", "decision", "reply", "error"]

SENSITIVE_KEY_RE = re.compile(
    r"^(?:.*_)?(?:api_key|apikey|secret|password|passwd|access_token|"
    r"refresh_token|auth_token|bearer_token|token|authorization|cookie|private_key)s?$",
    re.IGNORECASE,
)
BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
API_KEY_TOKEN_RE = re.compile(r"\b(?:gsk_[A-Za-z0-9_-]{8,}|(?:gsk|sk)-[A-Za-z0-9_-]{8,})\b")
DB_URL_CRED_RE = re.compile(
    r"\b(postgres(?:ql)?(?:\+\w+)?://[^:\s/@]+:)[^@\s]+(@)",
    re.IGNORECASE,
)
RIDER_ID_RE = re.compile(r"\bR\d{3}\b", re.IGNORECASE)
PRESERVED_ID_KEYS = frozenset({"message_id", "conversation_id", "dispute_id", "decision_id"})


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
    sanitized_input = redact_trace_payload(input_payload or {}, rider_id=rider_id)
    sanitized_output = redact_trace_payload(_json_output(output_payload), rider_id=rider_id)
    event = TraceEvent(
        rider_id=rider_id,
        conversation_id=conversation_id,
        message_id=message_id,
        dispute_id=dispute_id,
        event_type=event_type,
        name=name,
        input_payload=sanitized_input,
        output_payload=sanitized_output,
        created_at=datetime.now(UTC),
    )
    session.add(event)
    return event


def redact_trace_payload(value: Any, *, rider_id: str) -> Any:
    """Recursively scrub credentials, tokens, and cross-rider IDs from trace payloads."""

    envelope_rider = rider_id.strip().upper()
    return _redact_value(value, envelope_rider=envelope_rider, preserve_rider_tokens=False)


def _redact_value(
    value: Any, *, envelope_rider: str, preserve_rider_tokens: bool
) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if SENSITIVE_KEY_RE.match(key):
                sanitized[key] = "[REDACTED]"
            else:
                sanitized[key] = _redact_value(
                    item,
                    envelope_rider=envelope_rider,
                    preserve_rider_tokens=key in PRESERVED_ID_KEYS,
                )
        return sanitized
    if isinstance(value, list | tuple):
        return [
            _redact_value(
                item,
                envelope_rider=envelope_rider,
                preserve_rider_tokens=preserve_rider_tokens,
            )
            for item in value
        ]
    if isinstance(value, str):
        return _redact_text(
            value,
            envelope_rider=envelope_rider,
            preserve_rider_tokens=preserve_rider_tokens,
        )
    return value


def _redact_text(
    text: str, *, envelope_rider: str, preserve_rider_tokens: bool
) -> str:
    redacted = BEARER_RE.sub("Bearer [REDACTED]", text)
    redacted = API_KEY_TOKEN_RE.sub("[REDACTED_KEY]", redacted)
    redacted = DB_URL_CRED_RE.sub(r"\1[REDACTED]\2", redacted)
    if not preserve_rider_tokens:
        redacted = RIDER_ID_RE.sub(
            lambda match: (
                match.group(0)
                if match.group(0).upper() == envelope_rider
                else "[REDACTED_RIDER]"
            ),
            redacted,
        )
    return redacted


def _json_output(value: dict[str, Any] | str | None) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if value is None:
        return {"value": None}
    return {"value": value}
