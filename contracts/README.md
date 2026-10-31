# Frozen Phase 0 contracts

These files are the implementation authority for Phase 1 onward.

- `payout_policy.v1.json` — versioned business policy, assumptions, grouping, normalization, decision classes, and provider-safety constants.
- `model_interpretation.schema.json` — the only schema sent to Groq. It contains language understanding and claim extraction fields, not payment authority.
- `../project-analysis/response_classification.schema.json` — full runtime/audit envelope assembled by deterministic code after evidence and rule checks.
- `../project-analysis/STRICT_RULES_SYSTEM_PROMPT.md` — model behavior rules. Its structured-output rule points to the model interpretation schema.
- `../docs/API_CONTRACT.md` — HTTP success/error behavior.
- `../docs/REQUIREMENTS_TRACEABILITY.md` — source requirement to implementation phase and acceptance evidence.

## Contract boundary

The model returns `model_interpretation.schema.json`. Runtime code validates it, resolves evidence, applies `payout_policy.v1.json`, performs non-LLM gates, and then builds the complete runtime classification. The model cannot set rule-gate results, approval/payment authorization, provider confirmation, or final audit state.

Phase 4 introduces versioned multi-complaint contracts without changing the frozen v1 files:

- `model_interpretation.v2.schema.json` preserves each complaint as an independent extraction.
- `../project-analysis/response_classification.v2.schema.json` stores complaint-level links,
  decisions, actions, and gate runs.
- `../project-analysis/STRICT_RULES_SYSTEM_PROMPT.v2.md` is attached to every v2 model call.

Runtime defaults now select v2. The v1 contracts remain available for historical audit/replay.

The Docker image copies the model prompt and full runtime schema into `/app/contracts/strict_rules_system_prompt.md` and `/app/contracts/runtime_classification.schema.json`, keeping runtime paths independent of the analysis directory.

## Versioning rule

Never edit a released contract in a way that changes behavior without changing its version. Store the version used on every decision and trace. Add a migration/eval case whenever a policy or normalization version changes.
