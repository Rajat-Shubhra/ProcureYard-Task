# QuickDrop Rider Payout Dispute Desk

Production-shaped FastAPI service for WhatsApp-style payout disputes. It preserves every source
row, extracts multiple complaints independently, computes policy outcomes deterministically,
blocks unsafe model proposals with non-LLM gates, and moves money through a durable PaySwift
outbox.

## Run

```bash
cp .env.example .env
# add GROQ_API_KEY, or set LLM_PROVIDER=fake for deterministic/offline behavior
docker compose up --build
```

The API is on `http://localhost:8000`, ops UI on `http://localhost:8000/ops`, and PaySwift on
`http://localhost:8081`. Startup order is Postgres → migrations → idempotent source import → API
and worker. `GET /health` becomes 200 only after imported data and a fresh worker heartbeat exist.
All configuration is listed in `.env.example`; `.env` is ignored.

Required interfaces:

- `POST /messages` — durable/idempotent rider message handling; returns at least `{"reply":"..."}`.
- `GET /trace/{rider_id}` — ordered message/tool/decision/error/reply audit.
- `GET /ops/pending` — pending approvals and necessary escalations.
- `POST /ops/items/{id}/approve|reject` — idempotent human review with full gate revalidation.

## Safety design

- Groq receives only the v2 extraction schema and strict prompt. Each complaint has its own date,
  trips, alleged amount, grounding, prior-dispute relation, and grouping proposal. It cannot emit a
  payment or approval.
- Runtime recomputes grouping, IST dates, ownership, seven-day eligibility, per-trip half-up fare,
  incentive, cancellation result, and actual shortfall from normalized evidence.
- Gate A validates extraction; Gate B validates every action and exact amount; Gate C scans the
  literal reply for unsupported riders, trips, amounts, or paid/approved/escalated claims.
- Auto-pay is ≤₹200 once per rider/IST action day. Known blocked amounts become approvals; unclear
  facts become escalations only when human investigation is actually necessary.
- Payout intents, immutable fingerprints, idempotency keys, attempts, leases, retries, and ledger
  reconciliation are persisted before/around provider calls. Ambiguous responses never prove
  failure and never change key/body on retry.

## Verify

```bash
pip install -e ".[dev]"
ruff check .
pytest --cov=app --cov-report=term-missing
python evals/run.py http://localhost:8000
# authoritative isolated case: restart a clean stack, then add --case N
```

The automated suite covers all 24 supplied scenarios on isolated databases, duplicate messages,
multiple complaints, revisions, source anomalies, action/reply gate attacks, migration parity, and
lost PaySwift responses recovered through ledger reconciliation. Docker Compose and one black-box
case run in GitHub Actions.

Known submission-only tasks: export the raw Codex/ChatGPT session into `ai-logs/`, push the final
reviewed commit to a new private repository on `main`, and add `Sarthak-pyard`. These external
account actions are intentionally not fabricated by the application.
