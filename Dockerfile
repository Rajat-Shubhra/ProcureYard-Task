FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN useradd --create-home --uid 10001 appuser

COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir .

COPY alembic.ini ./
COPY alembic ./alembic
COPY contracts ./contracts
COPY project-analysis/STRICT_RULES_SYSTEM_PROMPT.md ./contracts/strict_rules_system_prompt.md
COPY project-analysis/response_classification.schema.json ./contracts/runtime_classification.schema.json
COPY project-analysis/STRICT_RULES_SYSTEM_PROMPT.v2.md ./project-analysis/STRICT_RULES_SYSTEM_PROMPT.v2.md
COPY project-analysis/response_classification.v2.schema.json ./project-analysis/response_classification.v2.schema.json
COPY project-analysis/STRICT_RULES_SYSTEM_PROMPT.v2.md ./contracts/strict_rules_system_prompt.v2.md
COPY project-analysis/response_classification.v2.schema.json ./contracts/runtime_classification.v2.schema.json
COPY data ./data

RUN chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

