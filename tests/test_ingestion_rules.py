from decimal import Decimal

from app.ingestion.rules import SourceRuleGate


def trip_row(*, rider_id: str = "R007", distance: str = "6.5") -> dict[str, str]:
    return {
        "trip_id": "T100",
        "rider_id": rider_id,
        "started_at": "2026-09-19T05:07:00Z",
        "status": "completed",
        "distance_km": distance,
        "surge_multiplier": "1.0",
    }


def payout_row(*, rider_id: str = "R007", trip_id: str = "T100") -> dict[str, str]:
    return {
        "line_id": "PL100",
        "payout_date": "2026-09-19",
        "rider_id": rider_id,
        "line_type": "trip",
        "trip_id": trip_id,
        "amount": "50",
    }


def test_unambiguous_rider_alias_is_normalized_without_escalation() -> None:
    normalized, findings = SourceRuleGate({"R007"}).normalize_trip(trip_row(rider_id="R7"), 2)

    assert normalized is not None
    assert normalized.rider_id == "R007"
    assert [finding.rule_id for finding in findings] == ["normalize-rider-id-v1"]
    assert not any(finding.requires_escalation for finding in findings)


def test_unknown_rider_is_rejected_and_escalated() -> None:
    normalized, findings = SourceRuleGate({"R007"}).normalize_trip(trip_row(rider_id="R999"), 2)

    assert normalized is None
    assert any(finding.rule_id == "unknown-or-ambiguous-rider-v1" for finding in findings)
    assert all(finding.requires_escalation for finding in findings)


def test_metre_like_distance_is_safely_normalized_without_escalation() -> None:
    normalized, findings = SourceRuleGate({"R007"}).normalize_trip(trip_row(distance="5000"), 2)

    assert normalized is not None
    assert normalized.raw_distance_value == Decimal("5000")
    assert normalized.distance_km == Decimal("5")
    assert any(finding.rule_id == "normalize-distance-unit-v1" for finding in findings)
    assert not any(finding.requires_escalation for finding in findings)


def test_already_plausible_distance_is_not_changed_or_flagged() -> None:
    normalized, findings = SourceRuleGate({"R007"}).normalize_trip(trip_row(distance="6.5"), 2)

    assert normalized is not None
    assert normalized.distance_km == Decimal("6.5")
    assert findings == []


def test_implausible_distance_is_rejected_and_escalated() -> None:
    normalized, findings = SourceRuleGate({"R007"}).normalize_trip(trip_row(distance="200000"), 2)

    assert normalized is None
    assert any(
        finding.rule_id == "implausible-normalized-distance-v1" and finding.requires_escalation
        for finding in findings
    )


def test_payout_owner_mismatch_is_rejected_and_escalated() -> None:
    normalized, findings = SourceRuleGate({"R007", "R008"}, {"T100": "R007"}).normalize_payout_line(
        payout_row(rider_id="R008"), 2
    )

    assert normalized is None
    assert any(
        finding.rule_id == "payout-trip-owner-mismatch-v1" and finding.requires_escalation
        for finding in findings
    )
