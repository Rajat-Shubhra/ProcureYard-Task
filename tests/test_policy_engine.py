from decimal import Decimal
from types import SimpleNamespace

from app.domain.payout import expected_trip_amount


def trip(distance: str, surge: str = "1.0", status: str = "completed") -> SimpleNamespace:
    return SimpleNamespace(
        distance_km=Decimal(distance),
        surge_multiplier=Decimal(surge),
        status=status,
    )


def test_completed_trip_fare_boundaries_and_half_up_rounding() -> None:
    assert expected_trip_amount(trip("2")) == 25
    assert expected_trip_amount(trip("2.5")) == 28
    assert expected_trip_amount(trip("3", "1.5")) == 47


def test_cancellation_policy_amounts() -> None:
    assert expected_trip_amount(trip("0", status="cancelled_by_rider")) == -10
    assert expected_trip_amount(trip("0", status="cancelled_by_customer")) == 0
