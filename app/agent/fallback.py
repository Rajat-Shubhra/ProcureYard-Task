from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.agent.contracts import (
    InterpretationContext,
    InterpretationResult,
    ModelInterpretation,
)

IST = ZoneInfo("Asia/Kolkata")
TRIP_RE = re.compile(r"\bT\d+\b", re.IGNORECASE)
AMOUNT_RE = re.compile(
    r"(?:₹|rs\.?|inr|rupay|rupaye|rupees?)\s*(\d+)|\b(\d+)\s*(?:rupay|rupaye|rupees?)",
    re.IGNORECASE,
)
DAY_RE = re.compile(r"\b([12]?\d|3[01])(?:st|nd|rd|th)?\b", re.I)
GREETING_RE = re.compile(
    r"(?:(?:hello|hi|hey|ok|okay|okk|thik hai|theek hai|thanks|thank you|thankyou|thx|"
    r"shukriya|dhanyavad|dhanyawad|ji|haan|hmm|great|good|👍|🙏)[\s\W]*)+",
    re.IGNORECASE,
)
DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]")
HINGLISH_WORD_RE = re.compile(
    r"\b(?:bhai|bhaiya|yaar|mera|meri|mere|mujhe|maine|humare|aapka|nahi|nhi|"
    r"paisa|paise|rupay|rupaye|ko|ka|ki|ke|pe|aur|bhi|abhi|kab|kya|kyun|kaise|"
    r"kitna|tarikh|wala|wali|wale|aaya|aaye|aayi|aayega|mila|mili|mile|kata|kaata|"
    r"kiye|kiya|karo|kardo|karna|chahiye|galat|kam|zyada|dobara|baar|hai|hain|"
    r"tha|thi|hua|hui|isliye|bahut|jaldi|turant|thik|theek|shukriya|dhanyavad|"
    r"dhanyawad|ji|haan|kal|aaj|parso|laga|raha)\b",
    re.IGNORECASE,
)
MONTH_MAP = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
MONTH_PATTERN = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
ISO_DATE_RE = re.compile(r"\b(20\d{2})-(0?[1-9]|1[0-2])-(0?[1-9]|[12]\d|3[01])\b")
DMY_SLASH_RE = re.compile(
    r"\b(0?[1-9]|[12]\d|3[01])[/-](0?[1-9]|1[0-2])(?:[/-](20\d{2}|\d{2}))?\b"
)
DAY_MONTH_RE = re.compile(
    rf"\b([12]?\d|3[01])(?:st|nd|rd|th)?\s+({MONTH_PATTERN})\b", re.IGNORECASE
)
MONTH_DAY_RE = re.compile(
    rf"\b({MONTH_PATTERN})\.?\s+([12]?\d|3[01])(?:st|nd|rd|th)?\b", re.IGNORECASE
)
PARSO_RE = re.compile(r"\b(?:parso|day\s+before\s+yesterday)\b", re.IGNORECASE)
YESTERDAY_RE = re.compile(r"\b(?:kal|yesterday)\b", re.IGNORECASE)
TODAY_RE = re.compile(r"\b(?:aaj|today)\b", re.IGNORECASE)


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
                        token in lowered
                        for token in (
                            "nahi nahi",
                            "dobara",
                            "wrong",
                            "recheck",
                            "re-check",
                            "check again",
                            "disagree",
                            "still incorrect",
                        )
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
        "language": _detect_language(text, lowered),
        "message_intent": message_intent,
        "requested_status": requested_status,
        "safety_flags": safety,
        "complaints": complaints,
    }


def _detect_language(text: str, lowered: str) -> str:
    if DEVANAGARI_RE.search(text) or HINGLISH_WORD_RE.search(lowered):
        return "hinglish"
    return "en"


def _first_pos(text: str, tokens: tuple[str, ...]) -> int | None:
    positions = [pos for token in tokens if (pos := text.find(token)) >= 0]
    return min(positions) if positions else None


def _complaint_specs(text: str) -> list[tuple[str, str]]:
    positioned: list[tuple[int, str, str]] = []
    if (pos := _first_pos(text, ("surge", "peak pay", "peak bonus", "multiplier"))) is not None:
        positioned.append((pos, "surge", "Rider reports missing surge payment."))
    distance_pos = _first_pos(
        text,
        (
            "distance",
            "wrong km",
            "km wrong",
            "km galat",
            "km kam",
            "route was longer",
            "longer route",
            "gps",
        ),
    )
    if distance_pos is None and " km" in text and not any(
        token in text
        for token in ("got only", "received only", "paid only", "underpaid", "shortfall")
    ):
        distance_pos = text.find(" km")
    if distance_pos is not None:
        positioned.append((distance_pos, "distance", "Rider disputes the recorded trip distance."))
    if (
        pos := _first_pos(
            text,
            (
                "incentive",
                "daily bonus",
                "target bonus",
                "daily target",
                "12 order",
                "12 trip",
                "12 deliver",
                "12 rides",
                "12 se zyada",
                "completed 12",
            ),
        )
    ) is not None:
        positioned.append((pos, "daily_incentive", "Rider reports a missing daily incentive."))
    if (pos := _first_pos(text, ("penalty", "cancel", "cancellation fee", "fine"))) is not None:
        double_deduction = any(
            token in text
            for token in ("do baar", "2 baar", "twice", "double", "two times", "2 times")
        )
        complaint_type = (
            "policy_exception"
            if not double_deduction
            and any(
                token in text
                for token in (
                    "accident",
                    "exception",
                    "kyun",
                    "emergency",
                    "medical",
                    "puncture",
                    "breakdown",
                    "hospital",
                    "waive",
                    "not my fault",
                    "wasn't my fault",
                )
            )
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
                "paisa kam",
                "paisa nahi",
                "paise kam",
                "paise nahi",
                "payment kam",
                "payment galat",
                "got only",
                "received only",
                "paid only",
                "underpaid",
                "shortfall",
                "short paid",
                "less payout",
                "less money",
                "wrong payout",
                "incorrect payout",
                "missing payout",
                "payout missing",
                "payment missing",
                "not paid",
                "not received",
                "didn't get",
                "did not get",
                "didn't receive",
                "did not receive",
                "unpaid",
                "missing fare",
                "fare missing",
                "wrong fare",
                "payout kam",
                "payout galat",
                "payout 5000",
            )
        )
        and not specs
    ):
        specs.append(("trip_payment", "Rider reports a missing or incorrect trip payout."))
    if (
        any(
            token in text
            for token in (
                "dobara check",
                "record",
                "galat laga",
                "recheck",
                "re-check",
                "check again",
                "still wrong",
                "disagree",
                "incorrect count",
            )
        )
        and not specs
    ):
        specs.append(("record_disagreement", "Rider disagrees with the recorded result."))
    return specs


NOT_A_DAY_AFTER_RE = re.compile(
    r"\s*(?:\.\d|-\s*\d|order|trip|deliver(?:y|ies)|rides?|rupay|rupaye|rupees?|rs\b|₹|"
    r"kiye|kiya|baar|times?\b|km\b|kms\b|x\b|%|se\s+zyada|complete|hrs?\b|hours?\b|"
    r"mins?\b|minutes?\b|days?\b|am\b|pm\b)",
    re.IGNORECASE,
)
NOT_A_DAY_BEFORE_RE = re.compile(
    r"(?:₹|rs\.?|inr|rupay|rupaye|rupees?)\s*$|[.,:/\d-]$",
    re.IGNORECASE,
)
WALA_AFTER_RE = re.compile(r"\s*wal[aei]\b", re.IGNORECASE)


def _overlaps(span: tuple[int, int], spans: list[tuple[int, int]]) -> bool:
    return any(span[0] < end and start < span[1] for start, end in spans)


def _infer_month_day(reference: date, month: int, day: int, year: int | None = None) -> date | None:
    if year is not None:
        try:
            return date(year, month, day)
        except ValueError:
            return None
    for candidate_year in (reference.year, reference.year - 1):
        try:
            candidate = date(candidate_year, month, day)
        except ValueError:
            continue
        if candidate <= reference + timedelta(days=1) or candidate_year == reference.year - 1:
            return candidate
    return None


def _dates(text: str, context: InterpretationContext) -> list[str]:
    received = context.envelope.received_at.astimezone(IST)
    ref_date = received.date()
    matched_spans: list[tuple[int, int]] = []
    explicit_positioned: list[tuple[int, str]] = []

    for match in ISO_DATE_RE.finditer(text):
        span = match.span()
        parsed = _infer_month_day(
            ref_date, int(match.group(2)), int(match.group(3)), int(match.group(1))
        )
        if parsed is not None:
            matched_spans.append(span)
            explicit_positioned.append((span[0], parsed.isoformat()))

    for match in DMY_SLASH_RE.finditer(text):
        span = match.span()
        if _overlaps(span, matched_spans):
            continue
        following = text[span[1] : span[1] + 16]
        preceding = text[max(0, span[0] - 10) : span[0]]
        if NOT_A_DAY_AFTER_RE.match(following) or NOT_A_DAY_BEFORE_RE.search(preceding):
            continue
        day_val, month_val = int(match.group(1)), int(match.group(2))
        year_raw = match.group(3)
        sep = text[match.end(1)]
        if sep == "-" and year_raw is None and len(match.group(2)) < 2:
            # Bare "2-3" or "10-11" without a zero-padded month or year is usually a numeric range.
            continue
        if (day_val, month_val) == (24, 7) and year_raw is None:
            continue
        year_val = (
            (int(year_raw) + 2000 if len(year_raw) == 2 else int(year_raw))
            if year_raw
            else None
        )
        parsed = _infer_month_day(ref_date, month_val, day_val, year_val)
        if parsed is not None:
            matched_spans.append(span)
            explicit_positioned.append((span[0], parsed.isoformat()))

    for regex, day_group, month_group in ((DAY_MONTH_RE, 1, 2), (MONTH_DAY_RE, 2, 1)):
        for match in regex.finditer(text):
            span = match.span()
            if _overlaps(span, matched_spans):
                continue
            month_val = MONTH_MAP[match.group(month_group)[:3].lower()]
            parsed = _infer_month_day(ref_date, month_val, int(match.group(day_group)))
            if parsed is not None:
                matched_spans.append(span)
                explicit_positioned.append((span[0], parsed.isoformat()))

    relative_positioned: list[tuple[int, str]] = []
    for regex, delta_days in ((PARSO_RE, 2), (YESTERDAY_RE, 1), (TODAY_RE, 0)):
        for match in regex.finditer(text):
            span = match.span()
            if _overlaps(span, matched_spans):
                continue
            matched_spans.append(span)
            iso_val = (ref_date - timedelta(days=delta_days)).isoformat()
            relative_positioned.append((span[0], iso_val))

    if relative_positioned and not explicit_positioned:
        relative_positioned.sort(key=lambda item: item[0])
        return list(dict.fromkeys(value for _, value in relative_positioned))

    # "10 wala" is ambiguous: "the ₹10 one" when an open dispute carries that amount,
    # otherwise "the 10th's" (a date).
    open_amounts = {
        int(item["amount"]) for item in context.open_disputes if item.get("amount") is not None
    }
    bare_positioned: list[tuple[int, str]] = []
    for match in DAY_RE.finditer(text):
        span = match.span()
        if _overlaps(span, matched_spans):
            continue
        following = text[match.end() : match.end() + 16]
        preceding = text[max(0, match.start() - 10) : match.start()]
        if NOT_A_DAY_AFTER_RE.match(following) or NOT_A_DAY_BEFORE_RE.search(preceding):
            continue
        day = int(match.group(1))
        if WALA_AFTER_RE.match(following) and day in open_amounts:
            continue
        try:
            candidate = ref_date.replace(day=day)
        except ValueError:
            continue
        if candidate > ref_date + timedelta(days=1):
            previous_month = (received.replace(day=1) - timedelta(days=1)).date()
            try:
                candidate = previous_month.replace(day=day)
            except ValueError:
                continue
        bare_positioned.append((span[0], candidate.isoformat()))

    combined = explicit_positioned + relative_positioned + bare_positioned
    combined.sort(key=lambda item: item[0])
    return list(dict.fromkeys(value for _, value in combined))


def _amount(text: str) -> int | None:
    match = AMOUNT_RE.search(text)
    if match:
        return int(match.group(1) or match.group(2))
    follow_up = re.search(r"\b(\d+)\s+wala\b", text, re.IGNORECASE)
    return int(follow_up.group(1)) if follow_up else None


def _requested_status(text: str) -> str:
    if any(
        token in text
        for token in (
            "kab tak",
            "status",
            "aayega",
            "kitna time",
            "when will",
            "where is my payout",
            "where is my payment",
            "any update",
            "still waiting",
        )
    ):
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
