from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, field_validator

from app.agent.contracts import MessageEnvelope
from app.conversation.service import ConversationService

router = APIRouter(tags=["messages"])


class MessageRequest(BaseModel):
    message_id: str = Field(min_length=1, max_length=150)
    rider_id: str = Field(pattern=r"^R\d{3}$")
    text: str = Field(min_length=1, max_length=4000)
    received_at: datetime

    @field_validator("received_at")
    @classmethod
    def received_at_must_have_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("received_at must include a timezone")
        return value


@router.post("/messages")
def receive_message(payload: MessageRequest, request: Request) -> dict[str, object]:
    service: ConversationService = request.app.state.conversation_service
    result = service.process(
        MessageEnvelope(
            message_id=payload.message_id,
            rider_id=payload.rider_id,
            text=payload.text,
            received_at=payload.received_at,
        )
    )
    return {
        "reply": result.reply,
        "message_id": result.message_id,
        "conversation_id": result.conversation_id,
        "state": result.state,
        "duplicate": result.duplicate,
    }
