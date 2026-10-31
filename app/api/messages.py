from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from app.agent.contracts import MessageEnvelope
from app.conversation.service import ConversationService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["messages"])

MESSAGE_ID_MAX_LENGTH = 150
RIDER_ID_MAX_LENGTH = 64
TEXT_MAX_LENGTH = 4000


class MessageRequest(BaseModel):
    """Vendor webhook envelope (docs/API_CONTRACT.md, ``POST /messages``).

    All four fields must be strings; ``message_id``, ``rider_id`` and ``text`` must be
    non-blank and ``received_at`` must be a timezone-aware ISO 8601 timestamp. The rider ID
    is only normalised here: an unknown rider is answered with a safe reply by the
    conversation service, not with a validation error.
    """

    message_id: str = Field(min_length=1, max_length=MESSAGE_ID_MAX_LENGTH)
    rider_id: str = Field(min_length=1, max_length=RIDER_ID_MAX_LENGTH)
    text: str = Field(min_length=1, max_length=TEXT_MAX_LENGTH)
    received_at: datetime

    @field_validator("message_id", "text")
    @classmethod
    def must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("rider_id")
    @classmethod
    def normalise_rider_id(cls, value: str) -> str:
        normalised = value.strip().upper()
        if not normalised:
            raise ValueError("must not be blank")
        return normalised

    @field_validator("received_at", mode="before")
    @classmethod
    def parse_received_at(cls, value: object) -> datetime:
        if not isinstance(value, str):
            raise ValueError("received_at must be an ISO 8601 string")
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError as exc:
            raise ValueError("received_at must be an ISO 8601 timestamp") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("received_at must include a timezone offset")
        return parsed


@router.post("/messages")
def receive_message(payload: MessageRequest, request: Request) -> dict[str, object]:
    service: ConversationService = request.app.state.conversation_service
    envelope = MessageEnvelope(
        message_id=payload.message_id,
        rider_id=payload.rider_id,
        text=payload.text,
        received_at=payload.received_at,
    )
    try:
        result = service.process(envelope)
    except ValueError as exc:
        # The conversation service re-checks the envelope (blank fields, configured text
        # limit, naive timestamp). Those are client errors, not retryable server faults.
        logger.warning(
            "message_envelope_rejected",
            extra={
                "trace_id": request.state.trace_id,
                "error_type": type(exc).__name__,
                "detail": str(exc)[:200],
            },
        )
        raise HTTPException(
            status_code=422, detail="The message envelope failed validation."
        ) from exc
    return {
        "reply": result.reply,
        "message_id": result.message_id,
        "conversation_id": result.conversation_id,
        "state": result.state,
        "duplicate": result.duplicate,
    }
