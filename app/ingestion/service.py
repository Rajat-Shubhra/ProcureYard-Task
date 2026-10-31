from __future__ import annotations

import csv
import hashlib
import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import Database
from app.ingestion.rules import (
    CANONICAL_RIDER_RE,
    RIDER_ALIAS_RE,
    NormalizedPayoutLineInput,
    NormalizedRiderInput,
    NormalizedTripInput,
    RuleFinding,
    SourceRuleGate,
)
from app.models import (
    ImportBatch,
    NormalizedTrip,
    PayoutLine,
    RawSourceRow,
    Rider,
    SourceFinding,
)

SOURCE_FILES = ("riders.csv", "trips.csv", "payout_lines.csv")


@dataclass(frozen=True, slots=True)
class CsvSourceRow:
    source_file: str
    row_number: int
    payload: dict[str, str]

    @property
    def fingerprint(self) -> str:
        return _hash_json(self.payload)


class ImportService:
    def __init__(
        self,
        database: Database,
        *,
        snapshot_version: str,
        normalization_version: str,
    ) -> None:
        self.database = database
        self.snapshot_version = snapshot_version
        self.normalization_version = normalization_version

    def import_directory(self, data_dir: Path) -> dict[str, Any]:
        rows_by_file = {
            source_file: _read_csv(data_dir / source_file, source_file)
            for source_file in SOURCE_FILES
        }
        source_digest = _source_digest(data_dir)
        batch_id = f"import_{uuid4().hex}"
        self._start_batch(batch_id, source_digest)

        try:
            with self.database.session() as session, session.begin():
                findings: list[RuleFinding] = []
                batch = session.get(ImportBatch, batch_id)
                assert batch is not None

                self._check_snapshot_digest(session, source_digest, findings)
                raw_counts = self._store_raw_rows(
                    session,
                    batch_id,
                    rows_by_file,
                    findings,
                )
                rider_counts, known_riders = self._ingest_riders(
                    session,
                    rows_by_file["riders.csv"],
                    findings,
                )
                trip_counts, trip_owners = self._ingest_trips(
                    session,
                    rows_by_file["trips.csv"],
                    known_riders,
                    findings,
                )
                payout_counts = self._ingest_payout_lines(
                    session,
                    rows_by_file["payout_lines.csv"],
                    known_riders,
                    trip_owners,
                    findings,
                )
                finding_counts = self._store_findings(session, batch_id, findings)
                session.flush()

                active_escalations = int(
                    session.scalar(
                        select(func.count())
                        .select_from(SourceFinding)
                        .where(
                            SourceFinding.source_snapshot_version == self.snapshot_version,
                            SourceFinding.requires_escalation.is_(True),
                        )
                    )
                    or 0
                )
                total_findings = int(
                    session.scalar(
                        select(func.count())
                        .select_from(SourceFinding)
                        .where(SourceFinding.source_snapshot_version == self.snapshot_version)
                    )
                    or 0
                )
                counts: dict[str, Any] = {
                    "source_rows": {
                        source_file: len(rows) for source_file, rows in rows_by_file.items()
                    },
                    "raw_rows": raw_counts,
                    "riders": rider_counts,
                    "trips": trip_counts,
                    "payout_lines": payout_counts,
                    "findings": {
                        **finding_counts,
                        "active_total": total_findings,
                        "active_escalations": active_escalations,
                    },
                }
                batch.counts = counts
                batch.status = "completed_with_escalations" if active_escalations else "completed"
                batch.completed_at = datetime.now(UTC)
                return {
                    "batch_id": batch_id,
                    "source_snapshot_version": self.snapshot_version,
                    "source_digest": source_digest,
                    "normalization_version": self.normalization_version,
                    "status": batch.status,
                    "counts": counts,
                }
        except Exception:
            self._mark_batch_failed(batch_id)
            raise

    def _start_batch(self, batch_id: str, source_digest: str) -> None:
        with self.database.session() as session, session.begin():
            session.add(
                ImportBatch(
                    batch_id=batch_id,
                    source_snapshot_version=self.snapshot_version,
                    source_digest=source_digest,
                    normalization_version=self.normalization_version,
                    status="running",
                    counts={},
                )
            )

    def _mark_batch_failed(self, batch_id: str) -> None:
        with self.database.session() as session, session.begin():
            batch = session.get(ImportBatch, batch_id)
            if batch is not None:
                batch.status = "failed"
                batch.completed_at = datetime.now(UTC)

    def _check_snapshot_digest(
        self,
        session: Session,
        source_digest: str,
        findings: list[RuleFinding],
    ) -> None:
        previous_digests = set(
            session.scalars(
                select(ImportBatch.source_digest).where(
                    ImportBatch.source_snapshot_version == self.snapshot_version,
                    ImportBatch.status != "running",
                )
            )
        )
        if previous_digests and source_digest not in previous_digests:
            findings.append(
                RuleFinding(
                    rule_id="source-snapshot-version-reused-v1",
                    severity="error",
                    requires_escalation=True,
                    source_file="manifest",
                    source_row_number=None,
                    record_key=self.snapshot_version,
                    message="Source content changed without a new snapshot version.",
                    details={
                        "current_digest": source_digest,
                        "previous_digests": sorted(previous_digests),
                    },
                )
            )

    def _store_raw_rows(
        self,
        session: Session,
        batch_id: str,
        rows_by_file: dict[str, list[CsvSourceRow]],
        findings: list[RuleFinding],
    ) -> dict[str, int]:
        existing = {
            (row.source_file, row.source_row_number): row
            for row in session.scalars(
                select(RawSourceRow).where(
                    RawSourceRow.source_snapshot_version == self.snapshot_version
                )
            )
        }
        inserted = skipped = conflicts = 0
        for rows in rows_by_file.values():
            for row in rows:
                key = (row.source_file, row.row_number)
                prior = existing.get(key)
                if prior is not None:
                    if prior.row_fingerprint == row.fingerprint:
                        skipped += 1
                    else:
                        conflicts += 1
                        findings.append(
                            RuleFinding(
                                rule_id="source-row-changed-under-version-v1",
                                severity="error",
                                requires_escalation=True,
                                source_file=row.source_file,
                                source_row_number=row.row_number,
                                record_key=f"{row.source_file}:{row.row_number}",
                                message="Source row changed under an existing snapshot version.",
                                details={
                                    "previous_fingerprint": prior.row_fingerprint,
                                    "current_fingerprint": row.fingerprint,
                                },
                            )
                        )
                    continue
                session.add(
                    RawSourceRow(
                        first_seen_batch_id=batch_id,
                        source_snapshot_version=self.snapshot_version,
                        source_file=row.source_file,
                        source_row_number=row.row_number,
                        row_fingerprint=row.fingerprint,
                        payload=row.payload,
                    )
                )
                inserted += 1
        return {"inserted": inserted, "skipped": skipped, "conflicts": conflicts}

    def _ingest_riders(
        self,
        session: Session,
        rows: list[CsvSourceRow],
        findings: list[RuleFinding],
    ) -> tuple[dict[str, int], set[str]]:
        grouped: dict[str, list[CsvSourceRow]] = defaultdict(list)
        for row in rows:
            grouped[row.payload.get("rider_id", "").strip()].append(row)
        roster_ids = {rider_id for rider_id in grouped if CANONICAL_RIDER_RE.fullmatch(rider_id)}
        gate = SourceRuleGate(roster_ids)
        existing = {rider.rider_id: rider for rider in session.scalars(select(Rider))}
        known_riders: set[str] = set()
        inserted = skipped = rejected = 0

        for rider_id, group in grouped.items():
            if len(group) > 1 and not _all_payloads_equal(group):
                rejected += 1
                findings.append(
                    _service_error(
                        "conflicting-roster-row-v1",
                        "riders.csv",
                        group[0].row_number,
                        rider_id or "missing",
                        "Roster contains conflicting rows for one rider ID.",
                        {"row_numbers": [row.row_number for row in group]},
                    )
                )
                continue
            normalized, row_findings = gate.validate_rider(group[0].payload, group[0].row_number)
            findings.extend(row_findings)
            if normalized is None:
                rejected += 1
                continue
            prior = existing.get(normalized.rider_id)
            if prior is None:
                session.add(self._new_rider(normalized))
                inserted += 1
                known_riders.add(normalized.rider_id)
            elif _rider_matches(prior, normalized):
                skipped += 1
                known_riders.add(normalized.rider_id)
            else:
                rejected += 1
                findings.append(
                    _service_error(
                        "existing-rider-conflict-v1",
                        "riders.csv",
                        group[0].row_number,
                        normalized.rider_id,
                        "Imported roster data conflicts with the stored rider.",
                        {},
                    )
                )
        return {"inserted": inserted, "skipped": skipped, "rejected": rejected}, known_riders

    def _ingest_trips(
        self,
        session: Session,
        rows: list[CsvSourceRow],
        known_riders: set[str],
        findings: list[RuleFinding],
    ) -> tuple[dict[str, int], dict[str, str]]:
        grouped: dict[str, list[CsvSourceRow]] = defaultdict(list)
        for row in rows:
            grouped[row.payload.get("trip_id", "").strip()].append(row)
        existing = {trip.trip_id: trip for trip in session.scalars(select(NormalizedTrip))}
        trip_owners: dict[str, str] = {}
        gate = SourceRuleGate(known_riders)
        inserted = skipped = rejected = exact_duplicates = 0
        noncanonical_rows = sum(
            1
            for row in rows
            if _has_safe_noncanonical_rider(row.payload.get("rider_id", ""), known_riders)
        )
        metre_rows = sum(1 for row in rows if _is_metre_like(row.payload.get("distance_km", "")))

        for trip_id, group in grouped.items():
            if len(group) > 1 and not _all_payloads_equal(group):
                rejected += 1
                findings.append(
                    _service_error(
                        "conflicting-duplicate-trip-v1",
                        "trips.csv",
                        group[0].row_number,
                        trip_id or "missing",
                        "Trip ID has conflicting source rows and cannot be normalized safely.",
                        {"row_numbers": [row.row_number for row in group]},
                    )
                )
                continue

            duplicate_count = len(group) - 1
            if duplicate_count:
                exact_duplicates += duplicate_count
                findings.append(
                    RuleFinding(
                        rule_id="deduplicate-identical-trip-v1",
                        severity="info",
                        requires_escalation=False,
                        source_file="trips.csv",
                        source_row_number=group[0].row_number,
                        record_key=trip_id,
                        message="Exact duplicate trip rows were collapsed into one logical trip.",
                        details={
                            "duplicate_row_count": duplicate_count,
                            "row_numbers": [row.row_number for row in group],
                        },
                    )
                )

            normalized, row_findings = gate.normalize_trip(group[0].payload, group[0].row_number)
            findings.extend(row_findings)
            if normalized is None:
                rejected += 1
                continue
            prior = existing.get(normalized.trip_id)
            if prior is None:
                session.add(self._new_trip(normalized, duplicate_count))
                inserted += 1
                trip_owners[normalized.trip_id] = normalized.rider_id
            elif _trip_matches(prior, normalized, duplicate_count):
                skipped += 1
                trip_owners[normalized.trip_id] = normalized.rider_id
            else:
                rejected += 1
                findings.append(
                    _service_error(
                        "existing-trip-conflict-v1",
                        "trips.csv",
                        group[0].row_number,
                        normalized.trip_id,
                        "Normalized trip conflicts with the stored trip record.",
                        {},
                    )
                )
        return (
            {
                "inserted": inserted,
                "skipped": skipped,
                "rejected": rejected,
                "exact_duplicate_rows": exact_duplicates,
                "noncanonical_rider_rows": noncanonical_rows,
                "metre_distance_rows": metre_rows,
            },
            trip_owners,
        )

    def _ingest_payout_lines(
        self,
        session: Session,
        rows: list[CsvSourceRow],
        known_riders: set[str],
        trip_owners: dict[str, str],
        findings: list[RuleFinding],
    ) -> dict[str, int]:
        grouped: dict[str, list[CsvSourceRow]] = defaultdict(list)
        for row in rows:
            grouped[row.payload.get("line_id", "").strip()].append(row)
        existing = {line.line_id: line for line in session.scalars(select(PayoutLine))}
        gate = SourceRuleGate(known_riders, trip_owners)
        inserted = skipped = rejected = exact_duplicates = 0

        for line_id, group in grouped.items():
            if len(group) > 1 and not _all_payloads_equal(group):
                rejected += 1
                findings.append(
                    _service_error(
                        "conflicting-payout-line-v1",
                        "payout_lines.csv",
                        group[0].row_number,
                        line_id or "missing",
                        "Payout line ID has conflicting source rows.",
                        {"row_numbers": [row.row_number for row in group]},
                    )
                )
                continue
            if len(group) > 1:
                exact_duplicates += len(group) - 1
                findings.append(
                    RuleFinding(
                        rule_id="deduplicate-identical-payout-line-v1",
                        severity="info",
                        requires_escalation=False,
                        source_file="payout_lines.csv",
                        source_row_number=group[0].row_number,
                        record_key=line_id,
                        message="Exact duplicate payout line rows were collapsed safely.",
                        details={"duplicate_row_count": len(group) - 1},
                    )
                )

            normalized, row_findings = gate.normalize_payout_line(
                group[0].payload, group[0].row_number
            )
            findings.extend(row_findings)
            if normalized is None:
                rejected += 1
                continue
            prior = existing.get(normalized.line_id)
            if prior is None:
                session.add(self._new_payout_line(normalized))
                inserted += 1
            elif _payout_line_matches(prior, normalized):
                skipped += 1
            else:
                rejected += 1
                findings.append(
                    _service_error(
                        "existing-payout-line-conflict-v1",
                        "payout_lines.csv",
                        group[0].row_number,
                        normalized.line_id,
                        "Imported payout line conflicts with the stored line.",
                        {},
                    )
                )
        return {
            "inserted": inserted,
            "skipped": skipped,
            "rejected": rejected,
            "exact_duplicate_rows": exact_duplicates,
        }

    def _store_findings(
        self,
        session: Session,
        batch_id: str,
        findings: list[RuleFinding],
    ) -> dict[str, int]:
        existing = set(session.scalars(select(SourceFinding.fingerprint)))
        inserted = skipped = escalations_inserted = 0
        for finding in findings:
            fingerprint = _finding_fingerprint(self.snapshot_version, finding)
            if fingerprint in existing:
                skipped += 1
                continue
            session.add(
                SourceFinding(
                    finding_id=f"finding_{fingerprint[:32]}",
                    fingerprint=fingerprint,
                    first_seen_batch_id=batch_id,
                    source_snapshot_version=self.snapshot_version,
                    source_file=finding.source_file,
                    source_row_number=finding.source_row_number,
                    record_key=finding.record_key,
                    rule_id=finding.rule_id,
                    severity=finding.severity,
                    requires_escalation=finding.requires_escalation,
                    message=finding.message,
                    details=finding.details,
                )
            )
            existing.add(fingerprint)
            inserted += 1
            escalations_inserted += int(finding.requires_escalation)
        return {
            "inserted": inserted,
            "skipped": skipped,
            "escalations_inserted": escalations_inserted,
        }

    def _new_rider(self, normalized: NormalizedRiderInput) -> Rider:
        return Rider(
            rider_id=normalized.rider_id,
            name=normalized.name,
            city=normalized.city,
            joined_on=normalized.joined_on,
            source_snapshot_version=self.snapshot_version,
            raw_payload=normalized.raw_payload,
        )

    def _new_trip(
        self,
        normalized: NormalizedTripInput,
        duplicate_count: int,
    ) -> NormalizedTrip:
        return NormalizedTrip(
            trip_id=normalized.trip_id,
            rider_id=normalized.rider_id,
            started_at=normalized.started_at,
            service_date_ist=normalized.service_date_ist,
            status=normalized.status,
            raw_distance_value=normalized.raw_distance_value,
            distance_km=normalized.distance_km,
            surge_multiplier=normalized.surge_multiplier,
            normalization_version=self.normalization_version,
            source_snapshot_version=self.snapshot_version,
            quality_flags=list(normalized.quality_flags),
            exact_duplicate_row_count=duplicate_count,
            raw_payload=normalized.raw_payload,
        )

    def _new_payout_line(self, normalized: NormalizedPayoutLineInput) -> PayoutLine:
        return PayoutLine(
            line_id=normalized.line_id,
            payout_date=normalized.payout_date,
            rider_id=normalized.rider_id,
            line_type=normalized.line_type,
            trip_id=normalized.trip_id,
            amount=normalized.amount,
            source_snapshot_version=self.snapshot_version,
            raw_payload=normalized.raw_payload,
        )


def _read_csv(path: Path, source_file: str) -> list[CsvSourceRow]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"{source_file} has no header")
        return [
            CsvSourceRow(source_file, row_number, {key: value or "" for key, value in row.items()})
            for row_number, row in enumerate(reader, start=2)
        ]


def _source_digest(data_dir: Path) -> str:
    digest = hashlib.sha256()
    for source_file in SOURCE_FILES:
        digest.update(source_file.encode("utf-8"))
        digest.update(b"\0")
        digest.update((data_dir / source_file).read_bytes())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def _hash_json(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _finding_fingerprint(snapshot_version: str, finding: RuleFinding) -> str:
    return _hash_json(
        {
            "snapshot_version": snapshot_version,
            **asdict(finding),
        }
    )


def _all_payloads_equal(rows: list[CsvSourceRow]) -> bool:
    return all(row.payload == rows[0].payload for row in rows[1:])


def _has_safe_noncanonical_rider(raw_value: str, known_riders: set[str]) -> bool:
    raw = raw_value.strip()
    if raw in known_riders:
        return False
    match = RIDER_ALIAS_RE.fullmatch(raw)
    return bool(match and f"R{int(match.group(1)):03d}" in known_riders)


def _is_metre_like(raw_value: str) -> bool:
    try:
        value = Decimal(raw_value.strip())
    except (InvalidOperation, AttributeError):
        return False
    return value.is_finite() and value > 100


def _service_error(
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


def _rider_matches(stored: Rider, candidate: NormalizedRiderInput) -> bool:
    return (
        stored.name == candidate.name
        and stored.city == candidate.city
        and stored.joined_on == candidate.joined_on
        and stored.raw_payload == candidate.raw_payload
    )


def _trip_matches(
    stored: NormalizedTrip,
    candidate: NormalizedTripInput,
    duplicate_count: int,
) -> bool:
    return (
        stored.rider_id == candidate.rider_id
        and _same_instant(stored.started_at, candidate.started_at)
        and stored.service_date_ist == candidate.service_date_ist
        and stored.status == candidate.status
        and Decimal(stored.raw_distance_value) == candidate.raw_distance_value
        and Decimal(stored.distance_km) == candidate.distance_km
        and Decimal(stored.surge_multiplier) == candidate.surge_multiplier
        and stored.quality_flags == list(candidate.quality_flags)
        and stored.exact_duplicate_row_count == duplicate_count
        and stored.raw_payload == candidate.raw_payload
    )


def _same_instant(left: datetime, right: datetime) -> bool:
    """Compare timestamps consistently across SQLite and PostgreSQL drivers."""

    if left.tzinfo is None:
        left = left.replace(tzinfo=UTC)
    if right.tzinfo is None:
        right = right.replace(tzinfo=UTC)
    return left.astimezone(UTC) == right.astimezone(UTC)


def _payout_line_matches(
    stored: PayoutLine,
    candidate: NormalizedPayoutLineInput,
) -> bool:
    return (
        stored.payout_date == candidate.payout_date
        and stored.rider_id == candidate.rider_id
        and stored.line_type == candidate.line_type
        and stored.trip_id == candidate.trip_id
        and stored.amount == candidate.amount
        and stored.raw_payload == candidate.raw_payload
    )
