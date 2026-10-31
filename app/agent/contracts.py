from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal

ComplaintType = Literal[
    "trip_payment",
    "surge",
    "distance",
    "daily_incentive",
    "cancellation_penalty",
    "record_disagreement",
    "policy_exception",
    "unknown",
]
TRIP_ID_RE = re.compile(r"^T\d+$")


@dataclass(frozen=True, slots=True)
class MessageEnvelope:
    message_id: str
    rider_id: str
    text: str
    received_at: datetime


@dataclass(frozen=True, slots=True)
class InterpretationContext:
    envelope: MessageEnvelope
    open_disputes: tuple[dict[str, Any], ...] = ()
    recent_turns: tuple[dict[str, Any], ...] = ()

    def as_prompt_payload(self) -> dict[str, Any]:
        return {
            "envelope_rider_id": self.envelope.rider_id,
            "message_id": self.envelope.message_id,
            "text": self.envelope.text,
            "received_at": self.envelope.received_at.isoformat(),
            "open_disputes": list(self.open_disputes),
            "recent_turns": list(self.recent_turns),
        }


@dataclass(frozen=True, slots=True)
class ExtractedComplaint:
    complaint_index: int
    complaint_type: ComplaintType
    summary: str
    service_date_ist: date | None
    trip_ids: tuple[str, ...]
    alleged_amount_rupees: int | None
    grounding_status: str
    missing_fields: tuple[str, ...]
    relation_kind: str
    target_dispute_id: str | None
    contradicts_prior: bool
    proposed_policy_cause: str
    proposed_service_date_ist: date | None
    message_context: str
    confidence: float
    raw_payload: dict[str, Any]

    @classmethod
    def from_payload(cls, value: dict[str, Any]) -> ExtractedComplaint:
        service_date = value["service_date_ist"]
        proposed_date = value["proposed_group"]["service_date_ist"]
        return cls(
            complaint_index=value["complaint_index"],
            complaint_type=value["complaint_type"],
            summary=value["summary"],
            service_date_ist=date.fromisoformat(service_date) if service_date else None,
            trip_ids=tuple(value["trip_ids"]),
            alleged_amount_rupees=value["alleged_amount_rupees"],
            grounding_status=value["grounding"]["status"],
            missing_fields=tuple(value["grounding"]["missing_fields"]),
            relation_kind=value["relation"]["kind"],
            target_dispute_id=value["relation"]["target_dispute_id"],
            contradicts_prior=value["relation"]["contradicts_prior"],
            proposed_policy_cause=value["proposed_group"]["policy_cause"],
            proposed_service_date_ist=(
                date.fromisoformat(proposed_date) if proposed_date else None
            ),
            message_context=value["proposed_group"]["message_context"],
            confidence=float(value["confidence"]),
            raw_payload=value,
        )


@dataclass(frozen=True, slots=True)
class ModelInterpretation:
    schema_version: str
    language: str
    message_intent: str
    requested_status: str
    safety_flags: tuple[str, ...]
    complaints: tuple[ExtractedComplaint, ...]
    raw_payload: dict[str, Any]

    @classmethod
    def from_payload(cls, value: dict[str, Any]) -> ModelInterpretation:
        return cls(
            schema_version=value["schema_version"],
            language=value["language"],
            message_intent=value["message_intent"],
            requested_status=value["requested_status"],
            safety_flags=tuple(value["safety_flags"]),
            complaints=tuple(ExtractedComplaint.from_payload(item) for item in value["complaints"]),
            raw_payload=value,
        )


@dataclass(frozen=True, slots=True)
class InterpretationResult:
    interpretation: ModelInterpretation
    provider: str
    model: str
    prompt_version: str
    attempt_count: int
    provider_request_id: str | None = None


def validate_interpretation_semantics(value: dict[str, Any]) -> None:
    for complaint in value["complaints"]:
        if complaint["complaint_index"] < 0:
            raise ValueError("complaint_index must be non-negative")
        if not complaint["summary"].strip() or len(complaint["summary"]) > 500:
            raise ValueError("complaint summary is blank or too long")
        if not all(TRIP_ID_RE.fullmatch(item) for item in complaint["trip_ids"]):
            raise ValueError("trip_ids must be canonical trip IDs")
        if not 0 <= complaint["confidence"] <= 1:
            raise ValueError("complaint confidence must be between zero and one")
        for date_value in (
            complaint["service_date_ist"],
            complaint["proposed_group"]["service_date_ist"],
        ):
            if date_value is not None:
                date.fromisoformat(date_value)
