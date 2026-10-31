from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import NormalizedTrip, PayoutLine, SourceFinding

IST = ZoneInfo("Asia/Kolkata")
POLICY_VERSION = "payout-policy-v1"

Ownership = Literal["verified", "mismatch", "unknown"]
PolicyWindow = Literal["eligible", "expired", "future", "unknown"]


@dataclass(frozen=True, slots=True)
class MoneyComponent:
    kind: str
    reference_id: str | None
    amount: int


@dataclass(frozen=True, slots=True)
class DisputeEvaluation:
    complaint_type: str
    rider_id: str
    service_date_ist: date | None
    trip_ids: tuple[str, ...]
    ownership: Ownership
    policy_window: PolicyWindow
    expected_components: tuple[MoneyComponent, ...]
    actual_components: tuple[MoneyComponent, ...]
    expected_total: int | None
    actual_total: int | None
    verified_shortfall: int | None
    completed_trip_count: int | None
    evidence_complete: bool
    source_ambiguous: bool
    reason_codes: tuple[str, ...]
    data_quality_flags: tuple[str, ...]


def expected_trip_amount(trip: NormalizedTrip) -> int:
    if trip.status == "cancelled_by_rider":
        return -10
    if trip.status == "cancelled_by_customer":
        return 0
    distance = Decimal(trip.distance_km)
    surge = Decimal(trip.surge_multiplier)
    fare = (Decimal(25) + max(distance - Decimal(2), Decimal(0)) * Decimal(6)) * surge
    return int(fare.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def policy_window(service_date: date | None, received_at: datetime) -> PolicyWindow:
    if service_date is None:
        return "unknown"
    received_date = received_at.astimezone(IST).date()
    age = (received_date - service_date).days
    if age < 0:
        return "future"
    return "eligible" if age <= 6 else "expired"


class PayoutPolicyEngine:
    def evaluate(
        self,
        session: Session,
        *,
        rider_id: str,
        complaint_type: str,
        service_date_ist: date | None,
        trip_ids: tuple[str, ...],
        received_at: datetime,
    ) -> DisputeEvaluation:
        trips, ownership, source_conflict = self._load_trips(
            session,
            rider_id=rider_id,
            service_date_ist=service_date_ist,
            trip_ids=trip_ids,
            complaint_type=complaint_type,
        )
        if source_conflict:
            # A named trip exists in the raw export but was rejected/quarantined at import
            # (conflicting rows). Money is blocked and a human must look at the source.
            return DisputeEvaluation(
                complaint_type=complaint_type,
                rider_id=rider_id,
                service_date_ist=service_date_ist,
                trip_ids=trip_ids,
                ownership="unknown",
                policy_window="unknown",
                expected_components=(),
                actual_components=(),
                expected_total=None,
                actual_total=None,
                verified_shortfall=None,
                completed_trip_count=None,
                evidence_complete=True,
                source_ambiguous=True,
                reason_codes=("conflicting_source_rows",),
                data_quality_flags=("source_conflict",),
            )
        extra_flags: tuple[str, ...] = ()
        if trip_ids and trips and ownership == "verified":
            # Evidence decides the service date; the rider's claimed date is only traced.
            evidence_dates = {trip.service_date_ist for trip in trips}
            if len(evidence_dates) != 1:
                return self._unverified(
                    complaint_type,
                    rider_id,
                    None,
                    trip_ids,
                    ownership,
                    "unknown",
                    reason="multiple_service_dates",
                )
            effective_date = next(iter(evidence_dates))
            if service_date_ist is not None and service_date_ist != effective_date:
                extra_flags = ("claimed_date_mismatch",)
        else:
            effective_date = self._effective_date(service_date_ist, trips)
        window = policy_window(effective_date, received_at)

        if complaint_type == "daily_incentive":
            return self._daily_incentive(session, rider_id, effective_date, received_at, ownership)
        if complaint_type in {"distance", "record_disagreement", "policy_exception"}:
            return DisputeEvaluation(
                complaint_type=complaint_type,
                rider_id=rider_id,
                service_date_ist=effective_date,
                trip_ids=tuple(trip.trip_id for trip in trips) or trip_ids,
                ownership=ownership,
                policy_window=window,
                expected_components=(),
                actual_components=(),
                expected_total=None,
                actual_total=None,
                verified_shortfall=None,
                completed_trip_count=None,
                evidence_complete=ownership == "verified" and bool(trips),
                source_ambiguous=False,
                reason_codes=(
                    "policy_exception_requested"
                    if complaint_type == "policy_exception"
                    else "distance_disputed"
                    if complaint_type == "distance"
                    else "record_disagreement",
                ),
                data_quality_flags=_quality_flags(trips) + extra_flags,
            )
        if ownership != "verified" or not trips:
            return self._unverified(
                complaint_type, rider_id, effective_date, trip_ids, ownership, window
            )
        if complaint_type == "cancellation_penalty":
            relevant = [trip for trip in trips if trip.status == "cancelled_by_rider"]
            line_type = "cancellation_penalty"
            component_kind = "cancellation_penalty"
        else:
            relevant = [trip for trip in trips if trip.status == "completed"]
            line_type = "trip"
            component_kind = "trip_fare"

        expected: list[MoneyComponent] = []
        actual: list[MoneyComponent] = []
        selected_ids: list[str] = []
        for trip in relevant:
            trip_lines = list(
                session.scalars(
                    select(PayoutLine).where(
                        PayoutLine.rider_id == rider_id,
                        PayoutLine.trip_id == trip.trip_id,
                        PayoutLine.line_type == line_type,
                    )
                )
            )
            expected_amount = expected_trip_amount(trip)
            actual_amount = sum(line.amount for line in trip_lines)
            shortfall = max(expected_amount - actual_amount, 0)
            if trip_ids or shortfall > 0:
                selected_ids.append(trip.trip_id)
                expected.append(MoneyComponent(component_kind, trip.trip_id, expected_amount))
                actual.extend(
                    MoneyComponent(component_kind, line.line_id, line.amount) for line in trip_lines
                )

        expected_total = sum(item.amount for item in expected)
        actual_total = sum(item.amount for item in actual)
        shortfall = max(expected_total - actual_total, 0)
        # A date-only claim whose trips are all correctly paid is a verified zero shortfall,
        # not missing grounding: the rider gets an explanation, not another question.
        evidence_complete = (bool(selected_ids) or bool(relevant)) and window != "unknown"
        if shortfall:
            reasons: tuple[str, ...] = ("verified_shortfall",)
        elif relevant:
            reasons = ("no_shortfall",)
        else:
            reasons = ("no_matching_trips",)
        return DisputeEvaluation(
            complaint_type=complaint_type,
            rider_id=rider_id,
            service_date_ist=effective_date,
            trip_ids=tuple(selected_ids),
            ownership=ownership,
            policy_window=window,
            expected_components=tuple(expected),
            actual_components=tuple(actual),
            expected_total=expected_total,
            actual_total=actual_total,
            verified_shortfall=shortfall,
            completed_trip_count=None,
            evidence_complete=evidence_complete,
            source_ambiguous=False,
            reason_codes=reasons,
            data_quality_flags=_quality_flags(relevant) + extra_flags,
        )

    def _load_trips(
        self,
        session: Session,
        *,
        rider_id: str,
        service_date_ist: date | None,
        trip_ids: tuple[str, ...],
        complaint_type: str,
    ) -> tuple[list[NormalizedTrip], Ownership, bool]:
        if trip_ids:
            trips = list(
                session.scalars(select(NormalizedTrip).where(NormalizedTrip.trip_id.in_(trip_ids)))
            )
            if len(trips) != len(set(trip_ids)):
                missing = set(trip_ids) - {trip.trip_id for trip in trips}
                conflicted = session.scalar(
                    select(SourceFinding.finding_id).where(
                        SourceFinding.requires_escalation.is_(True),
                        SourceFinding.record_key.in_(missing),
                    )
                )
                return trips, "unknown", conflicted is not None
            if any(trip.rider_id != rider_id for trip in trips):
                return [], "mismatch", False
            return trips, "verified", False
        if service_date_ist is None:
            return [], "unknown", False
        status_filter = (
            NormalizedTrip.status == "cancelled_by_rider"
            if complaint_type in {"cancellation_penalty", "policy_exception"}
            else NormalizedTrip.status == "completed"
        )
        trips = list(
            session.scalars(
                select(NormalizedTrip).where(
                    NormalizedTrip.rider_id == rider_id,
                    NormalizedTrip.service_date_ist == service_date_ist,
                    status_filter,
                )
            )
        )
        return trips, "verified", False

    def _daily_incentive(
        self,
        session: Session,
        rider_id: str,
        service_date: date | None,
        received_at: datetime,
        ownership: Ownership,
    ) -> DisputeEvaluation:
        if service_date is None:
            return self._unverified("daily_incentive", rider_id, None, (), "unknown", "unknown")
        completed = list(
            session.scalars(
                select(NormalizedTrip).where(
                    NormalizedTrip.rider_id == rider_id,
                    NormalizedTrip.service_date_ist == service_date,
                    NormalizedTrip.status == "completed",
                )
            )
        )
        lines = list(
            session.scalars(
                select(PayoutLine).where(
                    PayoutLine.rider_id == rider_id,
                    PayoutLine.payout_date == service_date,
                    PayoutLine.line_type == "daily_incentive",
                )
            )
        )
        expected_amount = 150 if len(completed) >= 12 else 0
        actual_amount = sum(line.amount for line in lines)
        shortfall = max(expected_amount - actual_amount, 0)
        expected = (MoneyComponent("daily_incentive", service_date.isoformat(), expected_amount),)
        actual = tuple(
            MoneyComponent("daily_incentive", line.line_id, line.amount) for line in lines
        )
        return DisputeEvaluation(
            complaint_type="daily_incentive",
            rider_id=rider_id,
            service_date_ist=service_date,
            trip_ids=tuple(trip.trip_id for trip in completed),
            ownership=ownership,
            policy_window=policy_window(service_date, received_at),
            expected_components=expected,
            actual_components=actual,
            expected_total=expected_amount,
            actual_total=actual_amount,
            verified_shortfall=shortfall,
            completed_trip_count=len(completed),
            evidence_complete=True,
            source_ambiguous=False,
            reason_codes=("verified_shortfall",) if shortfall else ("no_shortfall",),
            data_quality_flags=_quality_flags(completed),
        )

    @staticmethod
    def _effective_date(claimed_date: date | None, trips: list[NormalizedTrip]) -> date | None:
        if claimed_date is not None:
            return claimed_date
        dates = {trip.service_date_ist for trip in trips}
        return dates.pop() if len(dates) == 1 else None

    @staticmethod
    def _unverified(
        complaint_type: str,
        rider_id: str,
        service_date: date | None,
        trip_ids: tuple[str, ...],
        ownership: Ownership,
        window: PolicyWindow,
        *,
        reason: str | None = None,
    ) -> DisputeEvaluation:
        if reason is None:
            reason = "ownership_mismatch" if ownership == "mismatch" else "missing_grounding"
        return DisputeEvaluation(
            complaint_type=complaint_type,
            rider_id=rider_id,
            service_date_ist=service_date,
            trip_ids=trip_ids,
            ownership=ownership,
            policy_window=window,
            expected_components=(),
            actual_components=(),
            expected_total=None,
            actual_total=None,
            verified_shortfall=None,
            completed_trip_count=None,
            evidence_complete=False,
            source_ambiguous=True,
            reason_codes=(reason,),
            data_quality_flags=("missing_trip",),
        )


def _quality_flags(trips: list[NormalizedTrip]) -> tuple[str, ...]:
    flags: list[str] = []
    if any(trip.exact_duplicate_row_count for trip in trips):
        flags.append("exact_duplicate_trip")
    for name in ("normalized_rider_id", "normalized_distance_unit"):
        if any(name in trip.quality_flags for trip in trips):
            flags.append(name)
    return tuple(flags or ["none"])
