# Phases 3–9 implementation overview

Status: implemented locally on 31 October 2026. External Docker/GitHub/account checks are listed
separately at the end.

## Phase 3 — deterministic payout engine

- Per-trip fare uses `25 + 6 * max(km - 2, 0)`, surge on the full fare, and per-trip `ROUND_HALF_UP`.
- Rider cancellation is −₹10; customer cancellation is zero.
- Daily incentive is ₹150 at 12 completed logical trips on an IST service date.
- Actual trip, incentive, and penalty lines are reconciled into `max(expected - actual, 0)`.
- Ownership, source completeness, and the received-date plus previous-six-IST-dates window are
  explicit evaluation fields. Alleged rider amounts never enter the calculation.
- Date/cause grouping works after source trip deduplication and normalization.

## Phase 4 — conversation and multi-complaint model adapter

- Versioned v2 model and runtime JSON schemas preserve every complaint independently.
- `complaints` rows never disappear when compatible complaints share one grouped `dispute`.
- `claim_revisions` retain each follow-up and contradiction; conversation memory contains compact
  open-dispute state rather than an unbounded transcript.
- Vendor `message_id` is reserved before model/provider work, so sequential duplicates reuse the
  stored reply and create no side effects. Database uniqueness protects concurrent delivery.
- Groq calls always include the v2 strict prompt and strict JSON schema, validate output, and retry
  once. A deterministic fail-safe parser handles outages and CI/offline operation.
- Model and PaySwift reads execute concurrently inside the webhook time budget.

## Phase 5 — non-LLM gates and trace

- Gate A checks schema version, safety flags, envelope identity, unique complaint indexes,
  grounding, prior-dispute linkage, and grouping consistency.
- Gate B checks runtime/human authority, identity, ownership, window, evidence completeness, exact
  positive shortfall, ₹200/daily guardrails, approval necessity, escalation necessity, provider
  reconciliation, and immutable idempotency fingerprints.
- Gate C scans the actual reply for other-rider IDs, unsupported trip IDs/rupee amounts, and false
  paid/approval/escalation claims.
- Every gate run and individual pass/fail/not-applicable rule result is stored with a checker
  version. Ordered trace events cover receipt, model call, gates/decisions, provider reconciliation,
  errors, ops decisions, and reply.

## Phase 6 — PaySwift durable flow

- Immutable payout intent is stored before submission with stable key/body fingerprint.
- Worker uses leases and `SKIP LOCKED` on PostgreSQL, bounded exponential retry, and attempt audit.
- Ledger reconciliation runs before submission and after 409/timeout/transport/5xx ambiguity.
- 400 is terminal; 429 and transient failures retry; exhausted failures create one necessary
  provider escalation. Same-key/different-body is blocked before any call.
- A lost-response test proves one provider payout is recovered from the ledger without a second
  submission.

## Phase 7 — HTTP and ops

- Required `/messages`, `/trace/{rider_id}`, and `/ops/pending` shapes are implemented.
- `/ops` provides a simple server-rendered pending table.
- Approve/reject is idempotent. Approval reruns Gate B and PaySwift reconciliation; an escalation
  cannot be converted directly into a guessed payout.

## Phase 8 — evals and adversarial tests

- `python evals/run.py <service-url>` sends only rider turns and observes the public API/PaySwift.
- Supports `--case N` for authoritative fresh-stack isolation, payout polling, ops inspection,
  reply mentions, trace completeness, latency, and aggregate pass/fail output.
- The local isolated-database suite passes all 24 supplied scenarios.
- Additional tests cover action injection, unnecessary escalation/approval, other-rider reply
  leakage, false paid wording, duplicate delivery, model schema retry, migrations, and provider
  lost-response reconciliation.

## Phase 9 — local hardening completed

- One-page root README, complete `.env.example`, ignored `.env`, CI migration/model parity, full
  tests, Compose health wait, and black-box duplicate smoke are present.
- No key is stored in tracked configuration and settings never serialize the Groq secret.

External/manual completion still required after supervisor review:

1. Run the Compose/black-box CI job on a Docker-capable host (Docker is unavailable locally).
2. Export the real raw Codex/ChatGPT session into `ai-logs/`; do not replace it with a summary.
3. Create/push the new private GitHub repository, set reviewed `main`, and add `Sarthak-pyard`.
