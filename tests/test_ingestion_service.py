from pathlib import Path

from sqlalchemy import func, inspect, select

from app.db import Database
from app.ingestion.service import ImportService
from app.models import (
    Base,
    ImportBatch,
    NormalizedTrip,
    PayoutLine,
    RawSourceRow,
    Rider,
    SourceFinding,
)

ROOT = Path(__file__).resolve().parents[1]


def count(database: Database, model: type[Base]) -> int:
    with database.session() as session:
        return int(session.scalar(select(func.count()).select_from(model)) or 0)


def test_repository_sources_import_idempotently_with_only_safe_findings(tmp_path: Path) -> None:
    database = Database(f"sqlite+pysqlite:///{tmp_path / 'ingestion.db'}")
    Base.metadata.create_all(database.engine)
    expected_tables = {
        "claim_revisions",
        "conversations",
        "decisions",
        "disputes",
        "evidence",
        "import_batches",
        "messages",
        "normalized_trips",
        "ops_items",
        "payout_attempts",
        "payout_intents",
        "payout_lines",
        "raw_source_rows",
        "riders",
        "rule_check_results",
        "source_findings",
        "trace_events",
        "worker_heartbeats",
    }
    assert expected_tables <= set(inspect(database.engine).get_table_names())
    importer = ImportService(
        database,
        snapshot_version="assignment-export-v1",
        normalization_version="source-normalization-v1",
    )

    first = importer.import_directory(ROOT / "data")

    assert first["status"] == "completed"
    assert first["counts"]["source_rows"] == {
        "riders.csv": 40,
        "trips.csv": 3314,
        "payout_lines.csv": 3222,
    }
    assert first["counts"]["raw_rows"] == {
        "inserted": 6576,
        "skipped": 0,
        "conflicts": 0,
    }
    assert first["counts"]["trips"]["exact_duplicate_rows"] == 10
    assert first["counts"]["trips"]["noncanonical_rider_rows"] == 64
    assert first["counts"]["trips"]["metre_distance_rows"] == 222
    assert first["counts"]["findings"]["active_escalations"] == 0

    assert count(database, RawSourceRow) == 6576
    assert count(database, Rider) == 40
    assert count(database, NormalizedTrip) == 3304
    assert count(database, PayoutLine) == 3222

    with database.session() as session:
        alias_trip = session.get(NormalizedTrip, "T840677")
        metre_trip = session.get(NormalizedTrip, "T312538")
        assert alias_trip is not None and alias_trip.rider_id == "R007"
        assert "normalized_rider_id" in alias_trip.quality_flags
        assert metre_trip is not None and float(metre_trip.distance_km) == 5.0
        assert "normalized_distance_unit" in metre_trip.quality_flags
        assert (
            session.scalar(
                select(func.count())
                .select_from(SourceFinding)
                .where(SourceFinding.requires_escalation.is_(True))
            )
            == 0
        )

    second = importer.import_directory(ROOT / "data")

    assert second["status"] == "completed"
    assert second["counts"]["raw_rows"] == {
        "inserted": 0,
        "skipped": 6576,
        "conflicts": 0,
    }
    assert second["counts"]["riders"] == {"inserted": 0, "skipped": 40, "rejected": 0}
    assert second["counts"]["trips"]["inserted"] == 0
    assert second["counts"]["trips"]["skipped"] == 3304
    assert second["counts"]["payout_lines"]["inserted"] == 0
    assert second["counts"]["payout_lines"]["skipped"] == 3222
    assert second["counts"]["findings"]["active_escalations"] == 0

    assert count(database, ImportBatch) == 2
    assert count(database, RawSourceRow) == 6576
    assert count(database, Rider) == 40
    assert count(database, NormalizedTrip) == 3304
    assert count(database, PayoutLine) == 3222
    database.dispose()


def test_conflicting_duplicate_trip_is_the_kind_of_case_that_escalates(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "riders.csv").write_text(
        "rider_id,name,city,joined_on\nR007,Test Rider,Bengaluru,2024-01-01\n",
        encoding="utf-8",
    )
    (source_dir / "trips.csv").write_text(
        "trip_id,rider_id,started_at,status,distance_km,surge_multiplier\n"
        "T100,R007,2026-09-19T05:07:00Z,completed,5.0,1.0\n"
        "T100,R007,2026-09-19T05:07:00Z,completed,8.0,1.0\n",
        encoding="utf-8",
    )
    (source_dir / "payout_lines.csv").write_text(
        "line_id,payout_date,rider_id,line_type,trip_id,amount\n",
        encoding="utf-8",
    )
    database = Database(f"sqlite+pysqlite:///{tmp_path / 'conflict.db'}")
    Base.metadata.create_all(database.engine)

    result = ImportService(
        database,
        snapshot_version="conflict-test-v1",
        normalization_version="source-normalization-v1",
    ).import_directory(source_dir)

    assert result["status"] == "completed_with_escalations"
    assert result["counts"]["findings"]["active_escalations"] == 1
    assert count(database, NormalizedTrip) == 0
    with database.session() as session:
        finding = session.scalar(
            select(SourceFinding).where(SourceFinding.rule_id == "conflicting-duplicate-trip-v1")
        )
        assert finding is not None and finding.requires_escalation
    database.dispose()
