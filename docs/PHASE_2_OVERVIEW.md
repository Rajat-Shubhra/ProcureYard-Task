# Phase 2 overview — database and source ingestion

Status: implemented and locally verified on 31 October 2026.

## Outcome

Phase 2 adds an idempotent, versioned import before the API and worker start. It preserves all
6,576 source rows, builds typed calculation tables, records every safe normalization, and keeps
the model out of all source-quality decisions.

The supplied export finishes with `status=completed`, 296 non-blocking findings, and **zero
escalations**. The findings are exactly the expected safe cases: 10 exact duplicate trip rows, 64
non-canonical rider references, and 222 metre-like distance values.

## Startup flow

```text
Postgres healthy
  -> Alembic migrates to head
  -> one-shot source importer completes
  -> API and worker start
  -> /health becomes 200 after import + fresh worker heartbeat
```

`DATA_DIR` and `SOURCE_SNAPSHOT_VERSION` are configurable in `.env`. Reusing a snapshot version
with changed content is an escalation; an unchanged re-import skips existing raw and typed rows.

## Deterministic source rule gate

The source gate has an explicit `requires_escalation` flag. Severity alone never creates human
work. A warning means a single, versioned, reversible interpretation exists. An escalation means
the system cannot safely choose one interpretation or an ownership/integrity invariant failed.

| Case | Handling | Escalation |
|---|---|---:|
| `R7 -> R007`, `r19 -> R019`, with one roster match | Canonicalize and retain raw value | No |
| Distance `>100`, with `/1000` result in 0.1–100 km | Convert metres to km and retain raw value | No |
| Byte-for-byte equivalent trip rows with one trip ID | Keep every raw row; create one logical trip | No |
| Unknown/ambiguous rider | Reject typed record | Yes |
| Conflicting rows with one trip/line/roster ID | Reject typed record | Yes |
| Invalid ID, date, timestamp, status, amount, or numeric field | Reject typed record | Yes |
| Normalized distance outside 0.1–100 km | Reject typed trip | Yes |
| Payout trip is unknown or belongs to another rider | Reject typed payout line | Yes |
| Source row/content changes under an existing snapshot version | Retain prior evidence; flag conflict | Yes |

Current export proof:

| Measure | Result |
|---|---:|
| Roster source / typed rows | 40 / 40 |
| Trip source / logical rows | 3,314 / 3,304 |
| Payout source / typed rows | 3,222 / 3,222 |
| Raw rows retained | 6,576 |
| Safe findings | 296 |
| Required escalations | 0 |

## Schema delivered

Source and import tables:

- `import_batches`, `raw_source_rows`, `source_findings`
- `riders`, `normalized_trips`, `payout_lines`

Durable tables reserved for the next runtime phases:

- `conversations`, `messages`, `disputes`, `claim_revisions`
- `evidence`, `decisions`, `rule_check_results`, `trace_events`
- `ops_items`, `payout_intents`, `payout_attempts`

Important database protections include unique vendor `message_id`, versioned claim/decision/rule
keys, one logical ops-item key, unique payout idempotency key/provider ID, positive payout amount,
and the invariant that approvals have a known positive amount while escalations have `amount=null`.

## Main implementation files

- `app/ingestion/rules.py` — deterministic normalization and escalation boundary.
- `app/ingestion/service.py` — raw preservation, typed import, findings, and idempotency.
- `app/ingestion/cli.py` — one-shot import command used by Compose.
- `app/models.py` — Phase 2 persistence schema.
- `alembic/versions/20261031_0002_source_ingestion.py` — import/source tables.
- `alembic/versions/20261031_0003_runtime_state_tables.py` — durable runtime state tables.
- `tests/test_ingestion_rules.py` and `tests/test_ingestion_service.py` — safe versus escalating
  cases, exact repository counts, conflicting duplicates, and re-import behavior.

## Verification performed

- Ruff: passed.
- Pytest: 19 tests passed.
- Alembic: upgraded a fresh SQLite database through all three revisions; one head at
  `20261031_0003`.
- Real export: 6,576 raw rows, 3,304 logical trips, 3,222 payout lines, zero escalations.
- Idempotency: a second import adds no raw/typed records.
- Compose shape: an `ingest` one-shot service gates both API and worker startup.

Docker is not installed on this workstation, so the full Postgres/Compose startup remains a CI
verification. The GitHub workflow already builds the stack and waits for the import-aware health
endpoint.

## Supervisor review points

1. Confirm that source `>100 / 1000` and the 0.1–100 km plausible range remain approved.
2. Confirm that source-quality escalations may allow the service to start while affected records
   remain excluded; later money gates must block any dispute lacking complete safe evidence.
3. Confirm the durable table fields before Phase 3 starts writing fare evidence and decisions.

Phase 3 should now implement the deterministic payout/reconciliation engine against these typed
tables. It must not convert a `source_finding` into money without the later evidence and action
gates.
