# Execution plan

## Phase status

- Phase 0 — complete and frozen for supervisor review on 31 October 2026.
- Phase 1 — implemented and locally linted/tested; container start/health gate awaits Docker-capable CI because Docker is not installed on this host.
- Phase 2 — implemented and locally verified; source import reports all expected anomaly counts and zero required escalations. Full Postgres/Compose startup awaits Docker-capable CI.
- Phase 3 — deterministic payout/reconciliation engine implemented and covered by policy/scenario tests.
- Phase 4 — multi-complaint v2 contracts, Groq/fallback adapter, durable conversations, and revisions implemented.
- Phase 5 — deterministic interpretation/action/reply gates, persisted rule results, and ordered trace implemented.
- Phase 6 — durable PaySwift intent/outbox/reconciliation worker implemented and ambiguity-tested.
- Phase 7 — required APIs, ops review mutations, and minimal ops UI implemented.
- Phase 8 — black-box eval CLI plus isolated all-scenario/adversarial tests implemented.
- Phase 9 — local hardening implemented; Docker CI, raw session export, private-repo push, and collaborator addition require external/manual completion after review.

Phase 0 outputs are in `contracts/`, `.env.example`, `docs/API_CONTRACT.md`, `docs/PHASE_0_DECISIONS.md`, and `docs/REQUIREMENTS_TRACEABILITY.md`. Phase 2 evidence and supervisor review points are in `docs/PHASE_2_OVERVIEW.md`.

## Recommended implementation

- Python 3.12, FastAPI, Pydantic, SQLAlchemy 2, Alembic, PostgreSQL, and `httpx`.
- A provider-agnostic structured-output LLM adapter; configure model/provider through environment variables.
- One application image with two Compose processes:
  - `api`: endpoints, server-rendered Jinja/HTMX ops UI, conversation orchestration.
  - `worker`: durable payout outbox/reconciliation loop.
- Existing unchanged `payswift` service plus Postgres.
- `pytest` for units/integration and a standalone black-box eval runner.

This stack keeps the UI simple, avoids a separate frontend build, and isolates slow/ambiguous payment work from the ten-second webhook deadline.

## Proposed project shape

```text
app/
  api/                 # messages, trace, ops, health routes
  agent/               # structured interpretation and reply drafting
  domain/              # policy calculations, decisions, state machine
  gates/               # deterministic pre-action and pre-reply checks
  providers/           # PaySwift and LLM adapters
  repositories/        # database access and source-evidence queries
  worker/              # outbox claiming, retry, reconciliation
  templates/           # ops UI and safe rider-reply templates
  models/              # SQLAlchemy and Pydantic models
alembic/
evals/
tests/
  unit/
  integration/
  contract/
.github/workflows/ci.yml
Dockerfile
docker-compose.yml
.env.example
```

## Phased plan and acceptance gates

### Phase 0 — freeze decisions and contracts

Tasks:

1. Adopt `STRICT_RULES_SYSTEM_PROMPT.md`, the response schema, the grouping rule, and the seven-day interpretation.
2. Define environment variables and defaults.
3. Record normalization rules and policy versions as constants/configuration.
4. Define API error behavior without changing required response shapes.

Gate:

- Every repository requirement is mapped to an implementation task or documented assumption.

### Phase 1 — scaffold runtime and infrastructure

Tasks:

1. Create FastAPI app, configuration, structured logging, Dockerfile, Compose app/Postgres/worker services.
2. Keep the PaySwift Compose service unchanged.
3. Add `/health` readiness and Alembic setup.
4. Add CI for lint/type checks, tests, and container build.

Gate:

- `docker compose up --build` brings up Postgres, API on 8000, worker, and PaySwift; health returns 200.

### Phase 2 — database and source ingestion

Status: complete locally. The fresh-database migration, real-source import, exact anomaly counts,
zero-escalation result, and idempotent re-import are covered by tests. Docker/Postgres startup is
delegated to the existing CI stack gate because Docker is unavailable on the development host.

Tables:

- riders, raw_trips, normalized_trips, payout_lines;
- conversations, messages, disputes, claim_revisions;
- evidence, decisions, rule_check_results, trace_events;
- ops_items, payout_intents, payout_attempts.

Tasks:

1. Import CSV data idempotently.
2. Preserve raw values and normalization version.
3. Canonicalize only unambiguous `R7`/`r19` patterns, normalize metre-like distance, convert timestamps to IST for derived fields, and collapse exact duplicate trips in calculation views.
4. Reject/escalate conflicting duplicate IDs.
5. Add constraints and indexes for message, rider/date, trip, ops, and payout lookups.

Gate:

- Re-import is idempotent; raw row counts match the repository; normalized views report 10 exact duplicate trip rows, 64 non-canonical rider rows, and 222 metre-like distance rows.

### Phase 3 — deterministic payout engine

Tasks:

1. Implement per-trip fare with half-up rounding.
2. Implement IST daily incentive and both cancellation outcomes.
3. Reconcile expected components against actual payout lines.
4. Enforce seven-day window, ownership, positive shortfall, grouping, ₹200 limit, and one auto-payment/day.
5. Produce versioned reason codes and evidence IDs.

Gate:

- Unit tests cover 2 km boundary, fractional `.5`, surge, UTC/IST midnight, seven-day edges, duplicate trips, malformed rider IDs, metres, overpayment, and mixed disputes.
- Deterministic outputs reproduce all numeric expectations in `data/conversations.json`.

### Phase 4 — conversation state and model adapter

Tasks:

1. Implement structured extraction against the JSON schema.
2. Add compact per-rider memory and claim revisions.
3. Add deterministic fast paths and safe fallback templates.
4. Implement split/group logic and one-question clarification.
5. Attach the strict prompt to every model call.

Gate:

- Multi-turn R003, R027, R011, and R020 scenarios preserve state and reach the expected end state.
- Invalid model JSON cannot reach a tool or monetary action.

### Phase 5 — deterministic gates and trace

Tasks:

1. Implement Gate A, Gate B, and Gate C from the workflow.
2. Store every rule's pass/fail/not-applicable result with version.
3. Implement ordered trace event writes for message, tools, decisions, errors, and replies.
4. Add privacy checks for cross-account references.

Gate:

- A forced unsafe model proposal is blocked.
- Every payment/approval/escalation can be reconstructed from trace and evidence.
- `GET /trace/{rider_id}` matches the required shape and order.

### Phase 6 — PaySwift and durable payout flow

Tasks:

1. Implement immutable payout intent, stable key, request fingerprint, and transactional outbox.
2. Handle 200/201, validation 400, both 409 variants, 429, 503, 504, timeouts, and connection failures.
3. Reconcile ambiguous results with PaySwift GET before retry.
4. Enforce same-key/same-body retry and bounded backoff.
5. Add worker recovery after process restart.

Gate:

- Duplicate webhook and concurrent delivery create exactly one provider payout.
- Simulated lost response creates one payout, is discovered by reconciliation, and ends confirmed.
- Rate limit/outage tests do not create retry storms or duplicate money.

### Phase 7 — required endpoints and ops UI

Tasks:

1. Implement `POST /messages`, `GET /trace/{rider_id}`, and `GET /ops/pending`.
2. Implement ops conversation/trace pages.
3. Add approve/reject endpoints with reviewer note and stale-decision revalidation.
4. Show known approvals separately from unknown/exception escalations.

Gate:

- R016 (₹425) and R034 (₹249) appear as approvals with evidence.
- Cross-account, record disagreement, distance dispute, accident exception, and injection appear only as appropriate escalations.
- Approve/reject is idempotent and reruns Gate B.

### Phase 8 — black-box evals and adversarial tests

Build `python evals/run.py <service-url>` to:

1. Start each sample on a fresh system or isolated database state.
2. Send only rider turns, preserving message IDs/timestamps.
3. Poll PaySwift and `/ops/pending` when actions are asynchronous.
4. Check payout total, approval amount, escalation requirement, reply mentions, duplicate safety, and trace completeness.
5. Print per-case pass/fail, latency, and an aggregate summary.

Additional tests:

- Same `message_id` concurrently and sequentially.
- Same text with different message IDs.
- Two valid disputes in one message.
- Cross-rider trip ID and typed identity switch.
- Prompt injection and secret/prompt request.
- Conflicting trip rows and implausible normalized distance.
- Boundary amounts ₹200/₹201.
- Previous auto-pay exists only in PaySwift, not local DB.
- Provider succeeds then times out; worker/process crashes and restarts.
- LLM timeout, malformed JSON, and hallucinated amount.

Gate:

- All 24 repository scenarios pass; repeated chaos runs never overpay or duplicate payout.

### Phase 9 — submission hardening

Tasks:

1. Run clean-clone Compose and eval commands.
2. Verify no secret or `.env` is committed.
3. Reduce root README to the required one-page content.
4. Record actual eval numbers and explicit omissions.
5. Export raw AI sessions into `ai-logs/`.
6. Push to a new private repo, add `Sarthak-pyard`, and verify `main`/Actions.

Gate:

- A reviewer can clone, run one Compose command, run one eval command, inspect ops UI/trace, and reproduce stated results.

## Suggested 24-hour order

| Work block | Focus | Approx. time |
|---|---|---:|
| 1 | Contracts, scaffold, Compose, DB migration | 2 h |
| 2 | Import/normalization and deterministic engine | 3.5 h |
| 3 | Message persistence, dedupe, conversation/model adapter | 3 h |
| 4 | Rule gates, trace, decision/ops items | 2.5 h |
| 5 | PaySwift outbox/reconciliation | 2.5 h |
| 6 | Endpoints and minimal ops UI | 2 h |
| 7 | Evals, concurrency/chaos/adversarial tests | 3 h |
| 8 | CI, README, AI logs, clean-clone verification | 1.5 h |
| Buffer | Debugging and final submission | 2 h |

## Priority if time is tight

Do not cut identity binding, deterministic arithmetic, idempotency, provider reconciliation, trace, rule gates, or evals. Cut visual polish, advanced analytics, and broad language support first. A plain ops table with correct approval/rejection behavior is better than a polished UI around unsafe money movement.

## Definition of done

- All required services and endpoints work from a clean clone.
- All 24 supplied scenarios and added safety/chaos cases pass.
- No duplicate or overpayment occurs under retries, timeouts, concurrency, or restart.
- No model output can bypass ownership, time, amount, auto-pay, approval, escalation, provider, or reply gates.
- Ops can understand and act on every pending item from evidence and trace.
- README, `.env.example`, CI, raw AI logs, collaborator, and private-repo submission requirements are complete.
