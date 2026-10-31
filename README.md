# QuickDrop Rider Payout Dispute Desk

FastAPI service for WhatsApp-style payout disputes. It preserves every source row, extracts
complaints with a strict-schema model (or a deterministic parser), computes policy outcomes in
code, blocks unsafe proposals with non-LLM gates, and moves money through a durable PaySwift outbox.

## Run

```bash
docker compose up --build   # clean clone: no .env needed; uses deterministic parser when GROQ_API_KEY is unset
# real model: export GROQ_API_KEY=gsk_... (or set GROQ_API_KEY in .env) and run docker compose up --build
```

API `http://localhost:8000`, ops UI `http://localhost:8000/ops` (pending table → item page with the
conversation, evidence, trace and approve/reject/retry buttons), PaySwift `http://localhost:8081`.
Startup: Postgres → migrations → idempotent import → API + worker; `GET /health` is 200 only after
imported data and a fresh worker heartbeat exist. All settings are in `.env.example` (`.env` optional).

## Design: what the model decides, what code decides

- **Model (Groq `LLM_PROVIDER=auto|groq`, strict JSON schema v2, no tools/streaming):** language,
  intent, one entry per complaint (type, date words, trip ids, alleged amount, grounding, link to a
  prior dispute), safety flags. A deterministic English/Hinglish parser replaces it offline/on failure.
- **Code evidence reads (no model involved):** rider roster, the rider's trips by IST date or by
  trip id, paid payout lines, entitlement calculation, the PaySwift ledger, open actions.
- **Code decides everything about money:** identity (envelope `rider_id`), ownership, IST dates,
  7-day window from the trip's own service date, fare/incentive/penalty arithmetic, shortfall
  `max(expected − actual, 0)`, deduction of anything already queued/paid/awaiting approval for the
  same evidence (local intents **and** the PaySwift ledger), the ₹200 cumulative limit and one
  auto-pay per rider per IST day, approval vs escalation, idempotency keys, retries, recursive trace
  redaction, and language-matched replies with verified math breakdowns checked by Gate C.

## Assumptions

- "Last 7 days" = the message's IST date plus the previous six IST dates; trip evidence decides the
  date, a rider-claimed date is only traced. Days are IST; `started_at` is UTC in the export.
- Distances > 100 are metres (÷1000, must land in 0.1–100 km); `R7`/`r19` map to `R007`/`R019`
  only with a unique roster match; byte-identical duplicate trip rows are one trip, conflicting
  rows are rejected and escalate. Every normalization is traced.
- The payout export is the historical baseline; PaySwift is authoritative for new money. No
  automatic clawback of overpayments. Ops authentication is deferred (evaluator GETs must stay open).

## Verify

```bash
pip install -e ".[dev]" && ruff check . && pytest --cov=app --cov-report=term-missing   # 208 tests
python evals/run.py http://localhost:8000 \
  --reset-cmd 'docker compose down --volumes && docker compose up --detach' --poll-seconds 40
python evals/run.py http://localhost:8000   # quick smoke on a running stack: NON-AUTHORITATIVE
```

`evals/run.py` is black-box (`POST /messages`, `GET /trace`, `GET /ops/pending`, `GET /health`,
PaySwift `GET /v1/payouts`). Per case it replays rider turns, polls until the expected
`(payout, approval)` end state holds, and checks escalation, `reply_mentions`, trace shape and
per-turn latency (`--max-turn-seconds`, default 9 s; p95 reported). **Results:** isolated mode
(fresh stack per case) 24/24 pass, p95 turn latency 63 ms on the in-process harness; shared-stack
mode 23/24 (case 04 re-asks about a trip case 00 already paid and is correctly reconciled instead of
paid again — hence NON-AUTHORITATIVE). CI runs lint, tests, migration parity, a Compose smoke case
and a second job that runs all 24 conversations against a recreated stack with PaySwift chaos on.

## Skipped / known limits

- Ops authentication is intentionally omitted so evaluator `GET /ops/pending` and `/ops` checks work
  without credentials; add SSO/RBAC before production deployment.
- No outbound WhatsApp push channel exists in the sandbox, so post-review ops decisions surface on
  the rider's next status turn; when `GROQ_API_KEY` is unset, the offline heuristic parser covers
  payout-domain English/Hinglish phrasings and standard date formats rather than open-domain chat.
