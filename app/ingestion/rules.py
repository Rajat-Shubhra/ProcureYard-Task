from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from zoneinfo import ZoneInfo

Severity = Literal["info", "warning", "error"]

CANONICAL_RIDER_RE = re.compile(r"^R\d{3}$")
RIDER_ALIAS_RE = re.compile(r"^R(\d+)$", re.IGNORECASE)
TRIP_ID_RE = re.compile(r"^T\d+$")
LINE_ID_RE = re.compile(r"^PL\d+$")
INTEGER_RE = re.compile(r"^-?\d+$")

TRIP_STATUSES = {"completed", "cancelled_by_customer", "cancelled_by_rider"}
PAYOUT_LINE_TYPES = {"trip", "daily_incentive", "cancellation_penalty"}
IST = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True, slots=True)
class RuleFinding:
    rule_id: str
    severity: Severity
    requires_escalation: bool
    source_file: str
    source_row_number: int | None
    record_key: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class NormalizedRiderInput:
    rider_id: str
    name: str
    city: str
    joined_on: date
    raw_payload: dict[str, str]


@dataclass(frozen=True, slots=True)
class NormalizedTripInput:
    trip_id: str
    rider_id: str
    started_at: datetime
    service_date_ist: date
    status: str
    raw_distance_value: Decimal
    distance_km: Decimal
    surge_multiplier: Decimal
    quality_flags: tuple[str, ...]
    raw_payload: dict[str, str]


@dataclass(frozen=True, slots=True)
class NormalizedPayoutLineInput:
    line_id: str
    payout_date: date
    rider_id: str
    line_type: str
    trip_id: str | None
    amount: int
    raw_payload: dict[str, str]


class SourceRuleGate:
    """Deterministic checks that distinguish normalization from escalation.

    A warning is safe, versioned, and reversible. An escalation is emitted only
    when no single safe interpretation exists or a financial/ownership invariant
    is violated.
    """

    def __init__(
        self,
        known_rider_ids: set[str],
        trip_owners: dict[str, str] | None = None,
    ) -> None:
        self.known_rider_ids = known_rider_ids
        self.trip_owners = trip_owners or {}

    def validate_rider(
        self,
        row: dict[str, str],
        row_number: int,
    ) -> tuple[NormalizedRiderInput | None, list[RuleFinding]]:
        findings: list[RuleFinding] = []
        rider_id = row.get("rider_id", "").strip()
        record_key = rider_id or f"row:{row_number}"
        if not CANONICAL_RIDER_RE.fullmatch(rider_id):
            findings.append(
                self._error(
                    "invalid-roster-rider-id-v1",
                    "riders.csv",
                    row_number,
                    record_key,
                    "Roster rider ID is not canonical and cannot define account identity.",
                    {"raw_rider_id": rider_id},
                )
            )

        name = row.get("name", "").strip()
        city = row.get("city", "").strip()
        if not name or not city:
            findings.append(
                self._error(
                    "missing-roster-field-v1",
                    "riders.csv",
                    row_number,
                    record_key,
                    "Roster row is missing name or city.",
                    {"name_present": bool(name), "city_present": bool(city)},
                )
            )

        joined_on = self._parse_date(
            row.get("joined_on", ""),
            findings,
            "riders.csv",
            row_number,
            record_key,
            "invalid-rider-joined-date-v1",
        )
        if findings:
            return None, findings
        return NormalizedRiderInput(rider_id, name, city, joined_on, dict(row)), findings

    def normalize_trip(
        self,
        row: dict[str, str],
        row_number: int,
    ) -> tuple[NormalizedTripInput | None, list[RuleFinding]]:
        findings: list[RuleFinding] = []
        trip_id = row.get("trip_id", "").strip()
        record_key = trip_id or f"row:{row_number}"
        if not TRIP_ID_RE.fullmatch(trip_id):
            findings.append(
                self._error(
                    "invalid-trip-id-v1",
                    "trips.csv",
                    row_number,
                    record_key,
                    "Trip ID is missing or invalid.",
                    {"raw_trip_id": trip_id},
                )
            )

        rider_id, rider_findings = self._canonicalize_rider(
            row.get("rider_id", ""), "trips.csv", row_number, record_key
        )
        findings.extend(rider_findings)

        started_at = self._parse_timestamp(
            row.get("started_at", ""), findings, "trips.csv", row_number, record_key
        )
        status = row.get("status", "").strip()
        if status not in TRIP_STATUSES:
            findings.append(
                self._error(
                    "invalid-trip-status-v1",
                    "trips.csv",
                    row_number,
                    record_key,
                    "Trip status is outside the supported export contract.",
                    {"status": status},
                )
            )

        raw_distance, distance_km, distance_findings = self._normalize_distance(
            row.get("distance_km", ""), row_number, record_key
        )
        findings.extend(distance_findings)
        surge = self._parse_decimal(
            row.get("surge_multiplier", ""),
            findings,
            "trips.csv",
            row_number,
            record_key,
            "invalid-surge-multiplier-v1",
            minimum=Decimal("0.1"),
            maximum=Decimal("10"),
        )

        if any(finding.requires_escalation for finding in findings):
            return None, findings
        assert rider_id is not None
        assert started_at is not None
        assert raw_distance is not None
        assert distance_km is not None
        assert surge is not None
        quality_flags = tuple(
            flag
            for flag, rule_id in (
                ("normalized_rider_id", "normalize-rider-id-v1"),
                ("normalized_distance_unit", "normalize-distance-unit-v1"),
            )
            if any(finding.rule_id == rule_id for finding in findings)
        )
        return (
            NormalizedTripInput(
                trip_id=trip_id,
                rider_id=rider_id,
                started_at=started_at,
                service_date_ist=started_at.astimezone(IST).date(),
                status=status,
                raw_distance_value=raw_distance,
                distance_km=distance_km,
                surge_multiplier=surge,
                quality_flags=quality_flags,
                raw_payload=dict(row),
            ),
            findings,
        )

    def normalize_payout_line(
        self,
        row: dict[str, str],
        row_number: int,
    ) -> tuple[NormalizedPayoutLineInput | None, list[RuleFinding]]:
        findings: list[RuleFinding] = []
        line_id = row.get("line_id", "").strip()
        record_key = line_id or f"row:{row_number}"
        if not LINE_ID_RE.fullmatch(line_id):
            findings.append(
                self._error(
                    "invalid-payout-line-id-v1",
                    "payout_lines.csv",
                    row_number,
                    record_key,
                    "Payout line ID is missing or invalid.",
                    {"raw_line_id": line_id},
                )
            )

        rider_id, rider_findings = self._canonicalize_rider(
            row.get("rider_id", ""), "payout_lines.csv", row_number, record_key
        )
        findings.extend(rider_findings)
        payout_date = self._parse_date(
            row.get("payout_date", ""),
            findings,
            "payout_lines.csv",
            row_number,
            record_key,
            "invalid-payout-date-v1",
        )
        line_type = row.get("line_type", "").strip()
        if line_type not in PAYOUT_LINE_TYPES:
            findings.append(
                self._error(
                    "invalid-payout-line-type-v1",
                    "payout_lines.csv",
                    row_number,
                    record_key,
                    "Payout line type is outside the supported export contract.",
                    {"line_type": line_type},
                )
            )

        amount_text = row.get("amount", "").strip()
        amount = int(amount_text) if INTEGER_RE.fullmatch(amount_text) else None
        if amount is None:
            findings.append(
                self._error(
                    "invalid-payout-amount-v1",
                    "payout_lines.csv",
                    row_number,
                    record_key,
                    "Payout amount is not a whole number of rupees.",
                    {"raw_amount": amount_text},
                )
            )
        elif (line_type in {"trip", "daily_incentive"} and amount < 0) or (
            line_type == "cancellation_penalty" and amount > 0
        ):
            findings.append(
                self._error(
                    "invalid-payout-amount-direction-v1",
                    "payout_lines.csv",
                    row_number,
                    record_key,
                    "Payout amount sign conflicts with the line type.",
                    {"line_type": line_type, "amount": amount},
                )
            )

        trip_id = row.get("trip_id", "").strip() or None
        trip_required = line_type in {"trip", "cancellation_penalty"}
        if trip_required and not trip_id:
            findings.append(
                self._error(
                    "missing-payout-trip-id-v1",
                    "payout_lines.csv",
                    row_number,
                    record_key,
                    "Trip-related payout line has no trip ID.",
                    {"line_type": line_type},
                )
            )
        if trip_id:
            owner = self.trip_owners.get(trip_id)
            if owner is None:
                findings.append(
                    self._error(
                        "unknown-payout-trip-v1",
                        "payout_lines.csv",
                        row_number,
                        record_key,
                        "Payout line references a trip that could not be safely normalized.",
                        {"trip_id": trip_id},
                    )
                )
            elif rider_id is not None and owner != rider_id:
                findings.append(
                    self._error(
                        "payout-trip-owner-mismatch-v1",
                        "payout_lines.csv",
                        row_number,
                        record_key,
                        "Payout rider and normalized trip owner do not match.",
                        {"trip_id": trip_id, "trip_owner": owner, "payout_rider": rider_id},
                    )
                )

        if line_type == "daily_incentive" and trip_id:
            findings.append(
                self._error(
                    "unexpected-incentive-trip-id-v1",
                    "payout_lines.csv",
                    row_number,
                    record_key,
                    "Daily incentive unexpectedly references a trip.",
                    {"trip_id": trip_id},
                )
            )

        if any(finding.requires_escalation for finding in findings):
            return None, findings
        assert rider_id is not None
        assert payout_date is not None
        assert amount is not None
        return (
            NormalizedPayoutLineInput(
                line_id=line_id,
                payout_date=payout_date,
                rider_id=rider_id,
                line_type=line_type,
                trip_id=trip_id,
                amount=amount,
                raw_payload=dict(row),
            ),
            findings,
        )

    def _canonicalize_rider(
        self,
        raw_value: str,
        source_file: str,
        row_number: int,
        record_key: str,
    ) -> tuple[str | None, list[RuleFinding]]:
        raw = raw_value.strip()
        if raw in self.known_rider_ids:
            return raw, []
        match = RIDER_ALIAS_RE.fullmatch(raw)
        candidate = f"R{int(match.group(1)):03d}" if match else None
        if candidate and candidate in self.known_rider_ids:
            return candidate, [
                RuleFinding(
                    rule_id="normalize-rider-id-v1",
                    severity="warning",
                    requires_escalation=False,
                    source_file=source_file,
                    source_row_number=row_number,
                    record_key=record_key,
                    message="Rider ID was normalized through one unambiguous roster match.",
                    details={"raw_rider_id": raw, "normalized_rider_id": candidate},
                )
            ]
        return None, [
            self._error(
                "unknown-or-ambiguous-rider-v1",
                source_file,
                row_number,
                record_key,
                "Rider ID has no single safe roster mapping.",
                {"raw_rider_id": raw, "candidate": candidate},
            )
        ]

    def _normalize_distance(
        self,
        raw_value: str,
        row_number: int,
        record_key: str,
    ) -> tuple[Decimal | None, Decimal | None, list[RuleFinding]]:
        findings: list[RuleFinding] = []
        try:
            raw = Decimal(raw_value.strip())
        except (InvalidOperation, AttributeError):
            return (
                None,
                None,
                [
                    self._error(
                        "invalid-trip-distance-v1",
                        "trips.csv",
                        row_number,
                        record_key,
                        "Trip distance is not numeric.",
                        {"raw_distance": raw_value},
                    )
                ],
            )
        if not raw.is_finite():
            return (
                None,
                None,
                [
                    self._error(
                        "invalid-trip-distance-v1",
                        "trips.csv",
                        row_number,
                        record_key,
                        "Trip distance is not finite.",
                        {"raw_distance": raw_value},
                    )
                ],
            )

        normalized = raw / Decimal(1000) if raw > 100 else raw
        if raw > 100:
            findings.append(
                RuleFinding(
                    rule_id="normalize-distance-unit-v1",
                    severity="warning",
                    requires_escalation=False,
                    source_file="trips.csv",
                    source_row_number=row_number,
                    record_key=record_key,
                    message="Metre-like distance was converted to kilometres.",
                    details={"raw_distance": str(raw), "distance_km": str(normalized)},
                )
            )
        if normalized < Decimal("0.1") or normalized > Decimal("100"):
            findings.append(
                self._error(
                    "implausible-normalized-distance-v1",
                    "trips.csv",
                    row_number,
                    record_key,
                    "Distance cannot be normalized into the approved plausible range.",
                    {"raw_distance": str(raw), "normalized_distance": str(normalized)},
                )
            )
        return raw, normalized, findings

    def _parse_timestamp(
        self,
        raw: str,
        findings: list[RuleFinding],
        source_file: str,
        row_number: int,
        record_key: str,
    ) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is None or parsed.tzinfo is None:
            findings.append(
                self._error(
                    "invalid-trip-timestamp-v1",
                    source_file,
                    row_number,
                    record_key,
                    "Trip timestamp is invalid or lacks a timezone.",
                    {"raw_started_at": raw},
                )
            )
            return None
        return parsed

    def _parse_date(
        self,
        raw: str,
        findings: list[RuleFinding],
        source_file: str,
        row_number: int,
        record_key: str,
        rule_id: str,
    ) -> date | None:
        try:
            return date.fromisoformat(raw.strip())
        except ValueError:
            findings.append(
                self._error(
                    rule_id,
                    source_file,
                    row_number,
                    record_key,
                    "Date field is invalid.",
                    {"raw_date": raw},
                )
            )
            return None

    def _parse_decimal(
        self,
        raw: str,
        findings: list[RuleFinding],
        source_file: str,
        row_number: int,
        record_key: str,
        rule_id: str,
        *,
        minimum: Decimal,
        maximum: Decimal,
    ) -> Decimal | None:
        try:
            value = Decimal(raw.strip())
        except (InvalidOperation, AttributeError):
            value = None
        if value is None or not value.is_finite() or not minimum <= value <= maximum:
            findings.append(
                self._error(
                    rule_id,
                    source_file,
                    row_number,
                    record_key,
                    "Numeric value is invalid or outside the supported range.",
                    {"raw_value": raw, "minimum": str(minimum), "maximum": str(maximum)},
                )
            )
            return None
        return value

    @staticmethod
    def _error(
        rule_id: str,
        source_file: str,
        source_row_number: int | None,
        record_key: str,
        message: str,
        details: dict[str, Any],
    ) -> RuleFinding:
        return RuleFinding(
            rule_id=rule_id,
            severity="error",
            requires_escalation=True,
            source_file=source_file,
            source_row_number=source_row_number,
            record_key=record_key,
            message=message,
            details=details,
        )
