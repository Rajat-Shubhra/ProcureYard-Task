# Repository analysis and extracted requirements

## 1. What exists now

The repository is a starter/data package, not an application. It contains 15 tracked files:

- Assignment and delivery contract: `BRIEF.md`, `SUBMISSION.md`, top-level `README.md`.
- Business policy: `docs/policy.md`.
- Source exports: `data/riders.csv`, `data/trips.csv`, `data/payout_lines.csv`, `data/conversations.json`, and `data/README.md`.
- PaySwift sandbox: `payswift/README.md`, `payswift/Dockerfile`, compiled `payswift/server.pyc`.
- Starter infrastructure and submission evidence: `docker-compose.yml`, `.gitignore`, `ai-logs/README.md`.

No app, database schema, agent, UI, eval runner, or CI workflow has been implemented yet.

## 2. Fixed delivery contract

- `docker compose up` must start the application, Postgres, and the unchanged `payswift` service.
- The application must listen on port `8000`.
- `GET /health` must return HTTP 200 only when ready.
- PaySwift's base URL must come from `PAYSWIFT_BASE_URL`.
- `POST /messages` accepts one rider message at a time and returns JSON with at least `{"reply":"..."}`.
- `GET /trace/{rider_id}` returns an ordered JSON list. Every step needs `at`, `type`, `name`, `input`, and `output`. Allowed fixed `type` values are `message_in`, `tool_call`, `decision`, `reply`, and `error`.
- `GET /ops/pending` returns pending approvals/escalations. Every item needs `id`, `rider_id`, `type`, `amount`, `reason`, and `created_at`.
- Ops needs a page that shows conversations/actions and can approve or reject pending items.
- The eval suite must be runnable with one documented command that accepts the service URL and uses only the public assignment interface plus PaySwift.
- GitHub Actions must run on every push.
- Every required environment variable must be listed in `.env.example`; secrets and `.env` must not be committed.
- The final root `README.md` must stay within one page and cover run instructions, design boundary, assumptions, eval results, and omissions.
- Raw AI session exports, one file per main session, must be placed in `ai-logs/`.
- Submission is a new private repository, not a fork; add `Sarthak-pyard` as collaborator. The version on `main` at the deadline is evaluated.

## 3. Business policy

All monetary rules must be deterministic code, never free-form model arithmetic.

### Trip entitlement

For a completed trip:

```text
pre_surge = 25 + 6 * max(distance_km - 2, 0)
trip_fare = floor(pre_surge * surge_multiplier + 0.5)
```

Rounding is per trip, to the nearest rupee, with `.5` rounding up.

### Other components

- Daily incentive: ₹150 for at least 12 completed trips on an IST calendar day.
- Rider cancellation: one ₹10 penalty per trip.
- Customer cancellation: no earning and no penalty.
- Disputes are considered only for the last seven IST calendar days. The implementation should define this as the received date plus the previous six dates, inclusive, and use the vendor's `received_at` after validating it.
- The amount owed is the positive difference between deterministic entitlement and actual paid lines. Never create a negative payout or claw back through this flow.

### Auto-payment guardrails

- At most ₹200 may be auto-paid per verified dispute.
- At most one dispute may be auto-paid for a rider per IST day.
- A verified amount above ₹200 requires ops approval.
- A second otherwise-eligible dispute for that rider/day requires ops approval.
- Never pay more than the verified outstanding amount.
- Reconcile new adjustments and ambiguous provider results against PaySwift, not only the local database.
- An `approval` means the amount is known and owed but a payment guardrail blocks auto-pay. An `escalation` means facts, ownership, source data, policy application, or requested exception remain unclear; its amount is `null`.

## 4. Messaging and conversation behavior

- The vendor sends messages one at a time as the rider types them.
- `message_id` (`wamid`) is the delivery/idempotency identifier. The vendor retries if it does not receive a 2xx response in roughly 10 seconds.
- Deduplicate before model calls, tool calls, state changes, approval creation, or payment. A repeated `message_id` must return the stored reply and create no new effects.
- Conversation state is bound to the envelope `rider_id`, which comes from the phone number. A rider's text cannot switch identity.
- Riders may send multiple messages, revise a claim, push back, ask about status, or raise multiple issues together.
- Ask one focused clarification when the claim cannot be grounded to a date, trip, or issue. Do not invent a dispute.
- Split unrelated policy causes or dates into separate disputes. Group multiple trips into one dispute when they share rider, date, cause/type, and message context. This reproduces the samples where two missing trips total ₹176 as one dispute, while a surge issue and a duplicate penalty become separate disputes.
- Pushback does not change facts or payment guardrails. Re-check deterministically, explain, then optionally escalate if the rider still disputes source data.
- Reply in the rider's language/register where practical (samples mix Hindi/Hinglish and English), with concrete dates, trip IDs, amounts, and next status where safe.
- Say “paid/processed” only after PaySwift confirms or the provider ledger proves the payout exists. Otherwise say “processing,” “waiting for approval,” or “sent to ops.”

## 5. Identity, security, and prompt-injection behavior

- Treat all rider text as untrusted data, never as instructions to the agent/runtime.
- Ignore strings such as “SYSTEM: ignore previous rules” and never expose prompts, tools, credentials, other riders' data, or internal reasoning.
- A trip must belong to the envelope rider after approved source normalization. If it belongs to another rider, reveal no owner or payout details; provide a generic cannot-verify reply and optionally escalate.
- Never accept a rider ID typed in the message as an identity change.
- The model may extract claims and propose classifications. It may not authorize payment, approval, rejection, source normalization, or policy exceptions.
- Tool results are data, not new instructions.
- Persist only necessary data; redact secrets and avoid putting raw message text/PII into PaySwift references or idempotency keys.

## 6. Dataset inventory and quality findings

### Inventory

- 40 riders: 25 Bengaluru, 10 Hyderabad, 5 Pune.
- 3,314 trip rows from 12–21 September 2026: 3,030 completed, 146 rider-cancelled, 138 customer-cancelled.
- 3,222 payout lines: 3,004 trip lines, 150 cancellation penalties, 68 incentives. Eleven trip lines have payout date 22 September.
- Surge multipliers present: `1.0`, `1.2`, `1.5`.
- 24 sample conversations, containing 30 rider turns and 9 illustrative agent turns.

### Intentional source-data traps

- Ten trip IDs are exact duplicate rows. Deduplicate identical rows by stable trip ID before counting trips or computing entitlements; retain a trace warning.
- Sixty-four trip rows use non-canonical rider IDs: `R7` for `R007` and `r19` for `R019`. Canonicalize only when the roster mapping is unique and trace both raw and normalized values.
- 222 trip rows use metre-like distance values (for example `5000`) while the schema says kilometres. The samples require `5000 -> 5.0 km`. Apply an explicit import normalization rule (values greater than 100 divided by 1,000), validate the result against a plausible range, and escalate instead of paying if ambiguous.
- There are cross-account trip references. Example: rider `R021` asks about `T482410`, which belongs to `R030`. Do not disclose the owner or pay.
- One sample combines both anomalies: `T840677` is stored under `R7`, while the envelope and payout use `R007`.
- Reconciliation after the stated normalization finds 34 completed-trip shortfalls/missing lines, four duplicate cancellation penalties, and seven incentive mismatches (including one apparent overpayment). A discrepancy alone does not authorize money: identity, claim scope, seven-day window, and guardrails still apply.
- Source rows use UTC `started_at`; policy grouping must convert to IST before determining dates or incentive counts.

Every normalization must be deterministic, versioned, reversible, and visible in trace evidence. Never let the LLM silently repair source data.

## 7. PaySwift provider behavior

### Documented API

- `POST /v1/payouts` body: `rider_id`, integer `amount` from 1 to 10,000, and free-text `reference`.
- Optional `Idempotency-Key` header; the application must always provide one.
- Success is normally HTTP 201 with `payout_id` and `status`.
- `GET /v1/payouts?rider_id=...` lists provider payouts; rider filter is optional.
- `GET /health` reports health.
- Ledger is in memory and clears when the container restarts.
- Rate limits and 400/409/429/5xx responses are expected.

### Important behavior recovered from `server.pyc`

- Chaos is on by default (`PROVIDER_CHAOS=on`). Tunables default to 15% immediate 503, 10% eight-second delay, and 5% “lost response.”
- In the lost-response case, the payout is written and idempotency state stored, then the server returns 504. Treat every timeout/5xx after a request as ambiguous until reconciled through GET and/or a same-key retry.
- A same-key, same-body retry returns HTTP 200 with the stored payout.
- Reusing the key with a different body returns 409 `idempotency_key_reused`.
- Concurrent use of the same key returns 409 `request_in_progress`; retry/reconcile later.
- More than 30 payout POST requests in a 10-second global window triggers a 60-second block with 429.
- An outage path sleeps 10 seconds and returns 503.
- The bytecode contains an undocumented, unauthenticated `POST /admin/outage` test endpoint. The application must not depend on or expose it; restrict it to controlled tests if used at all.
- A stable key such as `payout:{dispute_id}:{decision_version}` and an immutable request fingerprint are required. Persist them before calling the provider.

## 8. Model versus code boundary

### Model may do

- Detect language and conversational intent.
- Extract mentioned dates, relative-date phrases, trip IDs, alleged amounts, and multiple claims.
- Link follow-up turns to open disputes.
- Identify that clarification is needed.
- Draft a concise rider-facing reply from verified facts.

### Deterministic code must do

- Validate and deduplicate messages.
- Bind identity and enforce trip ownership.
- Normalize approved source anomalies and preserve raw evidence.
- Convert time to IST and enforce the seven-day window.
- Count completed trips, calculate fares/penalties/incentives, and reconcile paid lines.
- Decide zero/positive outstanding amount.
- Apply the ₹200 and one-auto-pay-per-day rules.
- Decide whether an item is an approval versus an escalation.
- Generate stable IDs/idempotency keys, call/reconcile PaySwift, and manage retries.
- Validate the action and the final reply against policy and provider state.
- Write trace events and ops items transactionally.

## 9. Sample-scenario acceptance matrix

| Case | Required behavior |
|---|---|
| R003 vague, then “20 wala” | Ask for date/order, find `T926334`, pay exactly ₹25, later report actual provider status. |
| R027 surge plus duplicate penalty | Split into two disputes; auto-pay either ₹15 or ₹10 and send the other amount for approval because only one auto-payment/day is allowed. |
| R011 disputes trip count | Explain 10 completed trips; optional escalation after re-check, no payment. |
| R020 claims to be R005 | Keep envelope identity R020; no payment/approval; optional security escalation. |
| Duplicate `message_id` | Exactly one ₹25 payout and one logical processing path. |
| R005 missing incentive | Auto-pay ₹150. |
| R008 says surge was active | Source says multiplier 1.0 and paid amount is correct; explain, pay ₹0. |
| R013 vague complaint | Ask for a useful detail; no payment or automatic escalation. |
| R014 duplicate penalty | Credit ₹10. |
| R016 five missing trips | Known amount ₹425; approval, not auto-pay. |
| R021 references another rider's trip | No data disclosure/payment; optional escalation. |
| R022 old trip | Outside seven-day window; no payment, optional escalation. |
| R024 missing incentive | Auto-pay ₹150. |
| R025 incentive claim not supported/missing | Explain verified result; no payment. |
| R007 malformed source rider ID | Canonicalize `R7 -> R007`, credit ₹34. |
| R031 metres in source | Normalize `5000 -> 5 km`, credit ₹22. |
| R009 incentive already/not missing | Explain; no payment. |
| R026 distance underpayment | Credit exactly ₹32. |
| R033 two missing trips, same cause/date | One ₹176 dispute; auto-pay. |
| R034 three missing trips | One ₹249 dispute; approval. |
| R035 claims ₹300 | Ignore claimed amount; verified shortfall is ₹25, so pay only ₹25. |
| R036 says recorded distance is wrong | Source cannot prove real-world distance; no payment, optional escalation. |
| R028 requests accident exception | Recorded penalty follows policy; no automatic reversal, optional human exception escalation. |
| R037 prompt injection | Treat as untrusted text, take no monetary action, optionally security-escalate. |

## 10. Assumptions to document in the final submission

- “Last seven days” is seven inclusive IST calendar dates ending on `received_at`'s local date.
- Identical duplicate trip rows represent one trip; conflicting rows for the same ID are unsafe and escalate.
- Rider-ID and distance normalization rules above are accepted only because the samples demonstrate them; every normalization is traced.
- The exported payout lines are the baseline historical ledger; PaySwift is authoritative for new adjustments and ambiguous results.
- No automatic clawback is attempted for overpayment.
- Human approval runs the full rule gate again, because paid state may have changed while waiting.
