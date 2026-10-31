# Requirements traceability matrix

Status values:

- `Frozen` — represented by a Phase 0 contract/decision.
- `Implemented` — deterministic code and automated acceptance evidence exist.
- `Planned` — mapped to a later implementation phase and acceptance evidence.
- `Assumption` — repository text is ambiguous; the chosen interpretation is explicit and reviewable.

| Source requirement | Phase 0 contract/decision | Implementation phase | Acceptance evidence | Status |
|---|---|---:|---|---|
| Agent handles rider conversations end to end | Hybrid model/code boundary, D-008 | 3–7 | All supplied conversations reach expected end state | Implemented |
| Riders send messages one at a time and over multiple turns | Conversation state contract | 4 | R003/R011/R027 multi-turn evals | Implemented |
| Riders may change their story | Claim revisions; contradictions retained | 4–5 | Disagreement/revision test preserves prior evidence | Implemented |
| Riders may complain about two things at once | Versioned grouping/splitting rule | 3–5 | R027 split; R033 grouped | Frozen |
| Correct rider receives money | Identity, ownership, amount gates | 3, 5, 6 | Cross-account and exact-amount tests | Implemented |
| Incorrect claim gets an explanation, not silence | Reply outcome and Gate C | 4–5 | R008/R009/R025 reply assertions | Implemented |
| Unclear or shady cases go to ops with history/reason | Escalation decision class and trace contract | 5, 7 | `tests/test_money_regressions.py` (R021 cross-account, R011 pushback escalations with null amount) | Implemented |
| Ops must trust system before it moves money | Deterministic Gate B; evidence/version audit | 3, 5 | Unsafe model output cannot create payout | Frozen |
| Auto-pay up to ₹200 per dispute (cumulative per rider/date/cause) | Policy contract | 3, 5 | `tests/test_money_regressions.py::test_decision_boundaries`, split-across-days test | Implemented |
| Only one auto-payment per rider per day | IST action-day rule | 3, 5, 6 | R027 scenario; `tests/test_money_regressions.py::test_prior_auto_payment_known_only_to_payswift_forces_approval` | Implemented |
| Bigger or repeat disputes require ops approval | Approval decision class | 3, 7 | R016 ₹425, R034 ₹249, R027 repeat | Frozen |
| Never pay more than owed | Shortfall formula; claim amount non-authoritative; Gate B rule 9 coverage | 3, 5 | R035 scenario; `tests/test_money_regressions.py` (re-sent, follow-up, re-worded, duplicate-delivery and ops-approval double-pay regressions) | Implemented |
| Reconcile against PaySwift, not local DB alone | Provider authority and ambiguity rules; logical provider reference | 5–6 | `tests/test_worker_durability.py`, `tests/test_money_regressions.py::test_ops_approval_closes_stale_when_payswift_already_holds_the_payout` | Implemented |
| Vendor retries after about 10 seconds | 8.5-second hard deadline and idempotent ingress | 4, 6 | p95 latency and duplicate-delivery tests | Frozen |
| `message_id` is vendor idempotency identity | Unique message contract | 1, 4 | Duplicate/concurrent duplicate creates one effect | Frozen |
| Envelope `rider_id` comes from phone number | Identity authority D-005 | 3–5 | R020 identity-switch test | Frozen |
| `POST /messages` required request/response | API contract v1 | 7 | `tests/test_http_contract.py`; `evals/run.py` black-box | Implemented |
| `GET /trace/{rider_id}` ordered required shape | API contract v1 | 5, 7 | `tests/test_http_contract.py` (shape/order); redaction still pending | Partial |
| `GET /ops/pending` required shape | API contract v1 | 7 | `tests/test_http_contract.py` | Implemented |
| Ops page shows conversations/actions | Simple server-rendered UI decision | 7 | HTTP/UI route test and manual review | Implemented |
| Ops can approve/reject waiting work | Mutation contract and stale revalidation | 6–7 | `tests/test_http_contract.py`, `tests/test_ops_service.py`, stale-close regression | Implemented |
| `docker compose up` starts app, Postgres, PaySwift | Stack decision; unchanged PaySwift constraint | 1 | Clean-clone Compose smoke test | Planned |
| Application listens on port 8000 | `APP_PORT=8000` | 1 | Health call on port 8000 | Frozen |
| `GET /health` returns 200 when ready | API readiness contract | 1 | DB/migration/worker readiness tests | Frozen |
| Read PaySwift URL from `PAYSWIFT_BASE_URL` | Environment contract | 1, 6 | Config override integration test | Frozen |
| Keep PaySwift service unchanged | Explicit implementation constraint | 1 | Diff check for original service definition | Frozen |
| PaySwift amount is whole rupees, 1–10,000 | Provider input contract | 3, 5, 6 | Validation boundary tests | Frozen |
| PaySwift rate limits/failures occur | Durable outbox/retry design | 6 | `tests/test_worker_durability.py` (MockTransport response matrix) | Implemented |
| Provider ledger resets on sandbox restart | Local intent/audit retained; logical reference; eval `--reset-cmd` | 6, 8 | `tests/test_worker_durability.py` crash/restart cases; `evals/run.py --reset-cmd` | Implemented |
| Eval command accepts service URL | Eval CLI shape | 8 | `python evals/run.py http://localhost:8000` | Implemented |
| Evals use only public interface and PaySwift | Black-box eval boundary | 8 | No DB/internal import in eval package | Frozen |
| Run every conversation against fresh system | Eval isolation rule | 8 | `evals/run.py --reset-cmd` (CI `evals-full` job); shared-stack runs are labelled non-authoritative | Implemented |
| GitHub Actions on every push | CI plan | 1, 9 | Workflow run on push | Planned |
| List all environment variables in `.env.example` | Environment contract | 0 | Tracked file review/config parity test | Frozen |
| Do not commit `.env` or keys | Existing `.gitignore` plus local blank file | 0, 9 | Secret scan and `git check-ignore .env` | Frozen |
| Root README is one page max with required content | Submission plan | 9 | Manual/page-length review | Implemented |
| Raw AI session exports in `ai-logs/` | Submission plan | 9 | Export files present, not summaries/screenshots | Planned |
| Submit a new private repo, not a fork | Submission plan | 9 | Remote/repository setting review | Planned |
| Add `Sarthak-pyard` collaborator | Submission plan | 9 | Collaborator setting verified | Planned |
| Evaluation uses `main` at deadline | Submission plan | 9 | Final commit pushed to `main` | Planned |
| Fare formula, surge, per-trip half-up rounding | Policy contract | 3 | Numeric/boundary unit tests | Frozen |
| Daily incentive ₹150 for ≥12 completed trips | Policy contract | 3 | R005/R024 and 11/12 boundary tests | Frozen |
| Rider cancellation −₹10; customer cancellation zero | Policy contract | 3 | Cancellation status tests | Frozen |
| “Day” means IST | Policy/environment contract | 2–3 | UTC/IST midnight tests | Frozen |
| Only last seven days | Received date plus previous six IST dates, decided by trip evidence | 3, 5 | `tests/test_money_regressions.py` window tests (claimed date cannot move an expired trip) | Assumption |
| Raw/source anomalies cannot silently change money | Versioned normalization plus escalation | 2, 5 | R007/R031, conflicting-row, implausible-distance tests | Implemented |
| Prompt injection must not authorize actions | Strict prompt plus deterministic gates | 4–5 | R037 and adversarial variants | Frozen |
| Groq key must have a safe configuration slot | Blank ignored `.env` plus tracked example | 0 | Key absent from Git; config loader test in Phase 1 | Frozen |

## Current gate result

Every fixed repository requirement is mapped to a frozen contract, implemented evidence, an
external/manual Phase 9 item, or the explicitly documented seven-day assumption. The remaining
`Planned` entries require Docker/GitHub/account actions outside the local implementation pass.
