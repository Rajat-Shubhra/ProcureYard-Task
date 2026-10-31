# Phase 1 implementation overview

Status: implementation complete; local static/unit verification passed  
Container acceptance: delegated to GitHub Actions because Docker is not installed on this host

## Delivered foundation

- Python 3.12 package using FastAPI, Pydantic Settings, SQLAlchemy 2.0, Alembic, psycopg, and HTTPX.
- Typed environment configuration with secret-safe `GROQ_API_KEY` handling and runtime validation.
- Structured JSON logging and per-request trace IDs.
- Common safe HTTP error envelope.
- PostgreSQL engine/session layer.
- Versioned Alembic migration for the worker readiness heartbeat.
- Worker process that upserts its heartbeat and shuts down cleanly on SIGTERM/SIGINT.
- `GET /health` that returns 200 only when both PostgreSQL and the worker heartbeat are ready.
- Docker image running as a non-root user.
- Compose services for Postgres, one-shot migrations, API, worker, and the unchanged PaySwift service.
- GitHub Actions on push and pull request for dependency install, lint, tests, migration-head check, Compose validation, full image build/start, and HTTP readiness.

## Runtime sequence

1. Postgres starts and passes `pg_isready`.
2. The one-shot `migrate` service applies Alembic migrations.
3. API and worker start only after migration success.
4. Worker writes `payout-worker` heartbeat every second.
5. `/health` checks `SELECT 1` and requires a heartbeat no older than five seconds.
6. Compose and CI consider the API healthy only after both checks pass.

## Key files

- `pyproject.toml` — dependency compatibility bands and test/lint configuration.
- `app/config.py` — environment contract and secret-safe settings.
- `app/application.py` — application factory/lifecycle.
- `app/api/health.py` and `app/health.py` — readiness endpoint and database/worker probe.
- `app/errors.py` and `app/middleware.py` — stable errors, trace header, request logging.
- `app/db.py`, `app/models.py`, `app/worker.py` — database ownership and worker heartbeat.
- `alembic/versions/20261031_0001_phase1_foundation.py` — first migration.
- `Dockerfile`, `docker-compose.yml`, `.dockerignore` — container runtime.
- `.github/workflows/ci.yml` — push/PR quality and full-stack readiness gate.
- `tests/` — configuration, contract, health, and error-envelope tests.

## Groq key

The actual repo's ignored `.env` contains an empty `GROQ_API_KEY=` slot. Paste the key after the equals sign. The tracked `.env.example` remains blank and safe to commit.

API startup fails clearly when `LLM_PROVIDER=groq` and the key is missing. CI switches to `LLM_PROVIDER=fake`, so secrets are never required in build logs or unit tests.

## Verification performed

- `ruff check .` — passed.
- `pytest --cov=app --cov-report=term-missing` — 10 passed; 40% foundation coverage.
- `alembic heads` — one head: `20261031_0001`.
- Python compilation — passed.
- Compose and GitHub workflow YAML parsing — passed.
- Model and runtime JSON Schema example validation — passed in the Phase 0 contract tests.

Docker is not installed on this Windows host, so `docker compose up --build` was not executed locally. The CI workflow runs that exact build/start/readiness lifecycle and prints Compose logs on failure.

## Supervisor review points

1. `/health` intentionally depends on a fresh worker heartbeat, not merely the API process.
2. API startup intentionally fails if Groq is selected without a key.
3. Dependency bands are deliberately capped to mature compatible families instead of floating to breaking releases.
4. Migrations run in a one-shot Compose service before API/worker startup.
5. The API and worker restart unless stopped; the migration service does not restart.
6. PaySwift's original service definition remains unchanged.

## Deliberately not included yet

Phase 1 does not implement source ingestion, conversation/message endpoints, policy calculations, PaySwift payouts, ops screens, or evals. Those remain assigned to Phases 2–8.
