# API and error contract

Contract version: `api-contract-v1`

This freezes HTTP behavior for implementation and black-box evals. Required assignment shapes are preserved; additional fields are optional and must not change the meaning of required fields.

## General rules

- JSON request/response encoding is UTF-8.
- Success responses use `Content-Type: application/json`, except the ops HTML page.
- Timestamps are ISO 8601 with an explicit offset or `Z`.
- IDs are opaque strings.
- Never return stack traces, prompts, secrets, database details, raw provider errors, or another rider's data.
- Every error and successful message handling path has a trace/correlation ID.
- A 2xx `POST /messages` response always contains a non-empty string `reply`.

## `GET /health`

Ready response:

```json
{
  "status": "ok",
  "database": "ready",
  "data": "ready",
  "worker": "ready"
}
```

- HTTP 200 only after configuration is valid, migrations are current, source data is available, and the database/worker heartbeat is ready.
- PaySwift or Groq being temporarily unavailable does not make the API unready; those failures have durable/fallback paths.
- HTTP 503 uses the common error envelope when the service cannot safely accept work.

## `POST /messages`

Request:

```json
{
  "message_id": "wamid.KAZ5TCF7RARUVH",
  "rider_id": "R003",
  "text": "Bhai order T926334 ka surge nahi mila, 20 tarikh wala",
  "received_at": "2026-09-22T09:05:00+05:30"
}
```

Validation:

- All four fields are required strings.
- `message_id` and `rider_id` must be non-blank.
- `text` may be short but not blank; maximum accepted length will be fixed in Phase 1.
- `received_at` must be a timezone-aware ISO timestamp.
- Envelope `rider_id` is the trusted conversation identity; text cannot replace it.

Success:

```json
{
  "reply": "text sent to the rider",
  "message_id": "wamid.KAZ5TCF7RARUVH",
  "conversation_id": "conv_...",
  "state": "resolved_paid"
}
```

Only `reply` is required by the assignment evaluator. Additional fields are stable diagnostic metadata.

Idempotency:

- The first durable insert owns a `message_id`.
- A completed duplicate returns HTTP 200 with the exact stored reply and no new side effects.
- A concurrent duplicate waits briefly for the owning request; if the deadline is near, it returns the same logical processing status without creating a second action.

Failure behavior:

| Condition | Status | Behavior |
|---|---:|---|
| Invalid JSON | 400 | Common non-retryable error envelope |
| Missing/invalid field | 422 | Common non-retryable error envelope |
| Unknown rider | 200 | Safe rider-facing reply; optional escalation, no private lookup |
| LLM timeout/invalid output | 200 | Deterministic fallback clarification or processing reply |
| PaySwift timeout/429/5xx/ambiguous result | 200 | Honest processing reply; durable same-key reconciliation |
| Database unavailable before durable receipt | 503 | Retryable error; vendor may resend |
| Unexpected failure after durable receipt | 503 | Retryable; next delivery resumes the same message |

Never return 5xx merely because a payout provider call is slow after the message and payout intent are safely persisted.

## `GET /trace/{rider_id}`

- HTTP 200 with an ordered JSON array.
- Unknown/no-history rider returns `[]` without disclosing whether a different identity exists.
- Every step contains at least:
  - `at`: ISO timestamp;
  - `type`: `message_in`, `tool_call`, `decision`, `reply`, or `error`;
  - `name`;
  - `input`;
  - `output`.
- Stable order is database sequence first, timestamp second.
- Tool inputs/outputs are redacted for secrets and other-rider information.

## `GET /ops/pending`

- HTTP 200 with a JSON array; no pending work returns `[]`.
- Every item contains at least `id`, `rider_id`, `type`, `amount`, `reason`, and `created_at`.
- `type=approval` requires a positive known integer `amount`.
- `type=escalation` requires `amount=null`.
- Stable sort: oldest first, then ID.

## Ops UI and mutation endpoints

- `GET /ops`: server-rendered pending/conversation/trace page.
- `POST /ops/items/{id}/approve`: approve a pending approval only.
- `POST /ops/items/{id}/reject`: reject a pending approval or close an escalation.
- `POST /ops/items/{id}/retry-payout`: re-queue the retained payout intent behind a
  `provider-failure:*` escalation (same idempotency key, body and reference).

Mutation request:

```json
{
  "reviewer": "ops-user",
  "note": "optional reason"
}
```

Rules:

- Mutations are idempotent for the same terminal decision.
- A different second terminal decision returns 409 (`conflict`).
- Approval reruns the complete deterministic action gate against the evidence that produced the
  approval and reconciles against local payout intents and the PaySwift ledger (Gate B rule 9). A
  stale/already-paid item closes with HTTP 200 and `status: "closed_stale"` without another payout.
- Unknown item returns 404.
- Attempting to approve an escalation without first establishing a new verified amount returns 409.
- Authentication is outside the assignment's fixed interface and is deferred as a documented production hardening item; do not place a secret requirement on evaluator-facing GET endpoints.

## Common error envelope

```json
{
  "error": {
    "code": "invalid_request",
    "message": "Safe human-readable summary.",
    "retryable": false,
    "trace_id": "trace_..."
  }
}
```

Every error response also carries the `x-trace-id` header.

Frozen codes:

- `invalid_json`
- `validation_error`
- `not_found`
- `conflict`
- `invalid_request` (other client errors, e.g. 405)
- `service_not_ready`
- `database_unavailable`
- `internal_error` (always HTTP 503, `retryable: true`)

Provider-specific errors are mapped to internal trace reason codes and are not exposed raw to riders.
