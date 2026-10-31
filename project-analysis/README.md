# Rider Payout Dispute Desk: design package

This directory turns the cloned assignment into implementation-ready guidance.

Phase 0 is now frozen in the repository-level `contracts/`, `.env.example`, and `docs/` artifacts; this directory remains the detailed analysis/design source.

- `REPOSITORY_ANALYSIS.md` — complete requirement, data, provider, and scenario extraction.
- `STRICT_RULES_SYSTEM_PROMPT.md` — standalone rules to attach to every model request.
- `response_classification.schema.json` — machine-readable JSON Schema for the complete runtime/audit decision envelope; the smaller Groq-facing schema is in `../contracts/model_interpretation.schema.json`.
- `response_classification.example.json` — representative valid classification for a surge shortfall.
- `CONVERSATION_WORKFLOW.md` — low-latency, reliable conversation and action workflow, including non-LLM rule gates.
- `EXECUTION_PLAN.md` — phased implementation plan and acceptance gates.

The source repository was inspected read-only at `C:\dev\qd-assignment`. All 15 tracked files match the writable workspace byte-for-byte. All project text, all CSV/JSON rows, and the compiled PaySwift bytecode were inspected. `.git` object files and sample hooks are clone metadata, not application requirements.
