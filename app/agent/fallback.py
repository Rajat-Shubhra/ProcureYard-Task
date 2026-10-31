from __future__ import annotations

import re
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.agent.contracts import (
    InterpretationContext,
    InterpretationResult,
    ModelInterpretation,
)

IST = ZoneInfo("Asia/Kolkata")
TRIP_RE = re.compile(r"\bT\d+\b", re.IGNORECASE)
AMOUNT_RE = re.compile(r"(?:₹|rs\.?|rupay|rupees?)\s*(\d+)|\b(\d+)\s*(?:rupay|rupees?)", re.I)
DAY_RE = re.compile(r"\b([12]?\d|3[01])(?:st|nd|rd|th)?\b", re.I)
GREETING_RE = re.compile(
    r"(?:(?:ok|okay|okk|thik hai|theek hai|thanks|thank you|thankyou|thx|shukriya|dhanyavad|"
    r"dhanyawad|ji|haan|hmm|great|good|👍|🙏)[\s\W]*)+",
    re.IGNORECASE,
)


class HeuristicInterpretationProvider:
    """Deterministic fail-safe parser used when the configured model is unavailable."""

    def interpret(self, context: InterpretationContext) -> InterpretationResult:
        payload = build_fallback_payload(context)
        return InterpretationResult(
            interpretation=ModelInterpretation.from_payload(payload),
            provider="deterministic_fallback",
            model="none",
            prompt_version="strict-rules-v2",
            attempt_count=0,
        )


def build_fallback_payload(context: InterpretationContext) -> dict[str, Any]:
    text = context.envelope.text
    lowered = text.lower()
    safety = _safety_flags(lowered, context.envelope.rider_id)
    requested_status = _requested_status(lowered)
    complaint_specs = _complaint_specs(lowered)
    trip_ids = tuple(dict.fromkeys(value.upper() for value in TRIP_RE.findall(text)))
    service_dates = _dates(lowered, context)
    amount = _amount(text)
    open_disputes = context.open_disputes

    greeting_only = bool(GREETING_RE.fullmatch(lowered.strip()))
    if greeting_only:
        complaint_specs = []
    elif not complaint_specs and open_disputes and requested_status == "not_applicable":
        complaint_specs = [("unknown", "Follow-up on an existing payout complaint.")]

    complaints: list[dict[str, Any]] = []
    for index, (complaint_type, summary) in enumerate(complaint_specs):
        service_date = service_dates[min(index, len(service_dates) - 1)] if service_dates else None
        target = _follow_up_target(
            open_disputes,
            complaint_type,
            service_date=service_date,
            trip_ids=trip_ids,
            amount=amount,
        )
        if target and complaint_type == "unknown":
            prior = next(item for item in open_disputes if item.get("dispute_id") == target)
            complaint_type = str(prior.get("dispute_type") or "unknown")
            summary = f"Follow-up on prior {complaint_type} complaint."
        relation_kind = "follow_up" if target else "new"
        missing: list[str] = []
        if complaint_type == "unknown":
            missing.append("issue_type")
        if service_date is None and not trip_ids and target is None:
            missing.append("date")
        status = "grounded" if not missing else "partial"
        complaint_trip_ids = list(trip_ids)
        complaints.append(
            {
                "complaint_index": index,
                "complaint_type": complaint_type,
                "summary": summary,
                "service_date_ist": service_date,
                "trip_ids": complaint_trip_ids,
                "alleged_amount_rupees": amount,
                "grounding": {"status": status, "missing_fields": missing},
                "relation": {
                    "kind": relation_kind,
                    "target_dispute_id": target,
                    "contradicts_prior": any(
                        token in lowered for token in ("nahi nahi", "dobara", "wrong")
                    ),
                },
                "proposed_group": {
                    "policy_cause": complaint_type,
                    "service_date_ist": service_date,
                    "message_context": "current_message",
                },
                "confidence": 0.65 if not missing else 0.4,
            }
        )

    message_intent = "status_request" if requested_status != "not_applicable" else "new_complaint"
    if greeting_only:
        message_intent = "greeting_or_thanks"
    elif any(item["relation"]["kind"] != "new" for item in complaints):
        message_intent = "follow_up"
    if len(complaints) > 1:
        message_intent = "mixed"
    return {
        "schema_version": "2.0.0",
        "language": "hinglish"
        if any(token in lowered for token in ("bhai", "nahi", "paisa", "ko "))
        else "en",
        "message_intent": message_intent,
        "requested_status": requested_status,
        "safety_flags": safety,
        "complaints": complaints,
    }


def _first_pos(text: str, tokens: tuple[str, ...]) -> int | None:
    positions = [pos for token in tokens if (pos := text.find(token)) >= 0]
    return min(positions) if positions else None


def _complaint_specs(text: str) -> list[tuple[str, str]]:
    positioned: list[tuple[int, str, str]] = []
    if (pos := _first_pos(text, ("surge",))) is not None:
        positioned.append((pos, "surge", "Rider reports missing surge payment."))
    if (pos := _first_pos(text, ("distance", " km"))) is not None and "got only" not in text:
        positioned.append((pos, "distance", "Rider disputes the recorded trip distance."))
    if (pos := _first_pos(text, ("incentive", "12 order", "12 se zyada"))) is not None:
        positioned.append((pos, "daily_incentive", "Rider reports a missing daily incentive."))
    if (pos := _first_pos(text, ("penalty", "cancel"))) is not None:
        complaint_type = (
            "policy_exception"
            if any(token in text for token in ("accident", "exception", "kyun"))
            else "cancellation_penalty"
        )
        positioned.append((pos, complaint_type, "Rider disputes a cancellation penalty."))
    positioned.sort(key=lambda item: item[0])
    specs = [(kind, summary) for _, kind, summary in positioned]
    if (
        any(
            token in text
            for token in (
                "payment nahi",
                "paisa missing",
                "paisa hi nahi",
                "got only",
                "payout kam",
                "payout galat",
                "payout 5000",
            )
        )
        and not specs
    ):
        specs.append(("trip_payment", "Rider reports a missing or incorrect trip payout."))
    if any(token in text for token in ("dobara check", "record", "galat laga")) and not specs:
        specs.append(("record_disagreement", "Rider disagrees with the recorded result."))
    return specs


NOT_A_DAY_AFTER_RE = re.compile(
    r"\s*(?:\.\d|order|trip|rupay|rupaye|rupees?|rs\b|₹|kiye|kiya|baar|"
    r"km\b|kms\b|x\b|%|se\s+zyada|complete)",
    re.IGNORECASE,
)
NOT_A_DAY_BEFORE_RE = re.compile(r"(?:₹|rs\.?|rupay|rupees?)\s*$|[.,:/\d]$", re.IGNORECASE)
WALA_AFTER_RE = re.compile(r"\s*wal[aei]\b", re.IGNORECASE)


def _dates(text: str, context: InterpretationContext) -> list[str]:
    received = context.envelope.received_at.astimezone(IST)
    if re.search(r"\bkal\b", text):
        return [(received.date() - timedelta(days=1)).isoformat()]
    if re.search(r"\baaj\b", text):
        return [received.date().isoformat()]
    if re.search(r"\bparso\b", text):
        return [(received.date() - timedelta(days=2)).isoformat()]
    # "10 wala" is ambiguous: "the ₹10 one" when an open dispute carries that amount,
    # otherwise "the 10th's" (a date).
    open_amounts = {
        int(item["amount"]) for item in context.open_disputes if item.get("amount") is not None
    }
    days = []
    for match in DAY_RE.finditer(text):
        following = text[match.end() : match.end() + 14]
        preceding = text[max(0, match.start() - 8) : match.start()]
        if NOT_A_DAY_AFTER_RE.match(following) or NOT_A_DAY_BEFORE_RE.search(preceding):
            # "12 kiye the", "2 baar", "7.4 km", "Rs 25", "20/9" are not dates.
            continue
        if WALA_AFTER_RE.match(following) and int(match.group(1)) in open_amounts:
            continue
        days.append(int(match.group(1)))
    dates: list[str] = []
    for day in days:
        try:
            candidate = received.date().replace(day=day)
        except ValueError:
            continue
        if candidate > received.date() + timedelta(days=1):
            previous_month = (received.replace(day=1) - timedelta(days=1)).date()
            try:
                candidate = previous_month.replace(day=day)
            except ValueError:
                continue
        value = candidate.isoformat()
        if value not in dates:
            dates.append(value)
    return dates


def _amount(text: str) -> int | None:
    match = AMOUNT_RE.search(text)
    if match:
        return int(match.group(1) or match.group(2))
    follow_up = re.search(r"\b(\d+)\s+wala\b", text, re.IGNORECASE)
    return int(follow_up.group(1)) if follow_up else None


def _requested_status(text: str) -> str:
    if any(token in text for token in ("kab tak", "status", "aayega")):
        return "payment"
    if "approve" in text or "approval" in text:
        return "approval"
    return "not_applicable"


def _safety_flags(text: str, envelope_rider_id: str) -> list[str]:
    flags: list[str] = []
    if any(token in text for token in ("ignore all", "system:", "previous rules", "prompt")):
        flags.append("prompt_injection")
    rider_ids = set(re.findall(r"\bR\d{3}\b", text, re.I))
    if any(rider.upper() != envelope_rider_id for rider in rider_ids):
        flags.append("identity_switch_attempt")
    return flags or ["none"]


def _is_compatible_follow_up(
    candidate: dict[str, Any],
    complaint_type: str,
    service_date: str | None,
    trip_ids: tuple[str, ...],
) -> bool:
    prior_type = candidate.get("dispute_type")
    if complaint_type not in {"unknown", "record_disagreement"} and prior_type not in {
        None,
        "unknown",
        complaint_type,
    }:
        return False
    prior_date = candidate.get("service_date_ist")
    if service_date is not None and prior_date is not None and prior_date != service_date:
        return False
    prior_trips = tuple(str(value) for value in candidate.get("trip_ids") or ())
    if trip_ids:
        if prior_trips:
            if set(trip_ids).isdisjoint(prior_trips):
                return False
        elif candidate.get("state") not in {"draft", "needs_clarification"}:
            return False
    return True


def _follow_up_target(
    open_disputes: tuple[dict[str, Any], ...],
    complaint_type: str,
    *,
    service_date: str | None = None,
    trip_ids: tuple[str, ...] = (),
    amount: int | None = None,
) -> str | None:
    candidates = [
        item
        for item in open_disputes
        if _is_compatible_follow_up(item, complaint_type, service_date, trip_ids)
    ]
    if not candidates:
        return None
    if complaint_type == "record_disagreement":
        explained = [item for item in candidates if item.get("state") == "resolved_explained"]
        if len(explained) == 1:
            return str(explained[0]["dispute_id"])
    if complaint_type != "unknown":
        typed = [item for item in candidates if item.get("dispute_type") == complaint_type]
        if len(typed) == 1:
            return str(typed[0]["dispute_id"])
    if amount is not None:
        matching = [item for item in candidates if item.get("amount") == amount]
        if len(matching) == 1:
            return str(matching[0]["dispute_id"])
    if len(candidates) == 1:
        return str(candidates[0]["dispute_id"])
    return None
