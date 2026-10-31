# Phase 0 decision record

Status: frozen for review  
Contract date: 31 October 2026  
Next phase: Phase 1 runtime/infrastructure scaffold

## Decisions

| ID | Frozen decision | Reason | Change impact |
|---|---|---|---|
| D-001 | Python 3.12, FastAPI, Pydantic, SQLAlchemy/Alembic, PostgreSQL, `httpx`, Jinja/HTMX | Fast implementation, typed contracts, simple ops UI | Changing stack affects Phases 1–9 |
| D-002 | Groq is the initial LLM provider; default model `openai/gpt-oss-20b` | Current Groq production model with strict structured-output support and low latency/cost | Model is environment-configurable |
| D-003 | The model emits only `contracts/model_interpretation.schema.json`; this strict-output call does not stream or invoke model tools | Prevents the model from fabricating payment, rule-gate, or provider fields and follows Groq structured-output constraints | Runtime executes evidence tools and builds the full audit envelope |
| D-004 | Seven-day window means received IST date plus previous six IST dates | Makes “last 7 days” deterministic and works with historical eval timestamps | Policy-version change if edited |
| D-005 | Vendor envelope `rider_id` is the identity authority | Prevents in-message impersonation/cross-account access | Mandatory security invariant |
| D-006 | Versioned source normalization handles `R7/r19`, metre-like distances, exact duplicate rows, and UTC→IST dates | Required by supplied examples/data | Threshold or mapping changes require new version/evals |
| D-007 | Claims group only when rider, IST service date, policy cause, and message context match | Reproduces R027 split and R033 combined expectations without limit gaming | Affects approval/auto-pay decisions |
| D-008 | LLM extracts language/claims; deterministic code owns math, identity, policy, actions, and final reply checks | Money movement must be reproducible and safe | Mandatory architecture boundary |
| D-009 | Auto-pay ≤₹200 once per rider per IST action day; known blocked amounts are approvals; unknowns are escalations | Direct finance instruction | Mandatory policy invariant |
| D-010 | Persist immutable payout intent before PaySwift; reconcile every ambiguous result with same-key/same-body retries | PaySwift can move money then return 504 | Mandatory reliability invariant |
| D-011 | LLM/PaySwift degradation returns a safe 2xx reply after durable receipt; database failure returns retryable 503 | Meets vendor ten-second retry behavior without losing work | Frozen in API contract |
| D-012 | Evaluator-facing GET endpoints remain unauthenticated; ops mutation auth is deferred as production hardening | Assignment defines no auth and external evals must work | Must be revisited before real deployment |

Groq model availability and strict-output support were checked against the official [Supported Models](https://console.groq.com/docs/models) and [Structured Outputs](https://console.groq.com/docs/structured-outputs) documentation on the contract date. The model remains an environment override because provider catalogs change.

## Environment contract

`.env.example` is tracked; `.env` is ignored and contains an empty `GROQ_API_KEY=` slot for the local key.

Required for a Groq-enabled runtime:

- `GROQ_API_KEY`: secret, never committed or logged.
- `GROQ_MODEL`: defaults to `openai/gpt-oss-20b`.
- `GROQ_BASE_URL`: defaults to Groq's OpenAI-compatible API.

Other values are frozen by category in `.env.example`:

- application/latency;
- PostgreSQL;
- model generation and timeout;
- contract versions;
- PaySwift timeouts/retry;
- worker polling/lease/batch.

Phase 1 must fail readiness with a safe configuration error when a required runtime value is missing. Tests that do not call the LLM may use a fake adapter without a real key.

## Manual-rule stages

“Manual rule check” is implemented in two ways:

1. Explicit non-LLM code gates:
   - Gate A validates interpretation, identity, safety, dates, and grouping.
   - Gate B recomputes evidence/money, checks ownership/window/limits/PaySwift/idempotency before actions.
   - Gate C validates privacy, amounts, facts, and payment-status wording before replies.
2. Human ops review:
   - approvals for known amounts blocked by finance limits;
   - escalations for unclear facts, data conflicts, exceptions, security, or unresolved provider state.

The LLM cannot mark any gate passed.

## Supervisor review points

These are the highest-value choices to edit before Phase 1:

1. Distance normalization: raw values `>100` are divided by 1,000; normalized values outside 0.1–100 km escalate.
2. Seven-day interpretation: current day plus previous six IST dates, anchored to vendor `received_at`.
3. Default Groq model: `openai/gpt-oss-20b`; override with `GROQ_MODEL`.
4. Grouping: same date and cause may group multiple trips into one dispute.
5. Ops authentication: deferred for take-home compatibility, required before production.
6. Provider read timeout: four seconds, then processing/outbox flow rather than blocking near the vendor deadline.

Edit the versioned contract before Phase 1 if any of these should change.
