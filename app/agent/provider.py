from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

import httpx
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from app.agent.contracts import (
    InterpretationContext,
    InterpretationResult,
    ModelInterpretation,
    validate_interpretation_semantics,
)
from app.config import Settings


class InterpretationProvider(Protocol):
    def interpret(self, context: InterpretationContext) -> InterpretationResult: ...


class InterpretationProviderError(RuntimeError):
    pass


class GroqInterpretationProvider:
    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        if settings.groq_api_key is None:
            raise ValueError("GROQ_API_KEY is required for the Groq provider")
        self.settings = settings
        self.prompt = settings.model_prompt_path.read_text(encoding="utf-8")
        self.schema = _load_json(settings.model_schema_path)
        Draft202012Validator.check_schema(self.schema)
        self.validator = Draft202012Validator(self.schema, format_checker=FormatChecker())
        self.client = client or httpx.Client(
            base_url=settings.groq_base_url.rstrip("/"),
            timeout=httpx.Timeout(settings.llm_timeout_seconds),
            headers={
                "Authorization": f"Bearer {settings.groq_api_key.get_secret_value()}",
                "Content-Type": "application/json",
            },
        )

    def interpret(self, context: InterpretationContext) -> InterpretationResult:
        validation_feedback: str | None = None
        last_error: Exception | None = None
        maximum_attempts = self.settings.llm_max_retries + 1
        for attempt in range(1, maximum_attempts + 1):
            try:
                response = self.client.post(
                    "/chat/completions",
                    json=self._request_body(context, validation_feedback),
                )
                response.raise_for_status()
                payload = _extract_json(response.json())
                self.validator.validate(payload)
                validate_interpretation_semantics(payload)
                return InterpretationResult(
                    interpretation=ModelInterpretation.from_payload(payload),
                    provider="groq",
                    model=self.settings.groq_model,
                    prompt_version=self.settings.prompt_version,
                    attempt_count=attempt,
                    provider_request_id=response.headers.get("x-request-id"),
                )
            except (
                httpx.HTTPError,
                KeyError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ) as exc:
                last_error = exc
                validation_feedback = "The prior response was unavailable or invalid JSON."
            except ValidationError as exc:
                last_error = exc
                validation_feedback = f"The prior JSON failed schema validation: {exc.message}"
        raise InterpretationProviderError(
            f"Groq interpretation failed after {maximum_attempts} attempt(s)"
        ) from last_error

    def _request_body(
        self,
        context: InterpretationContext,
        validation_feedback: str | None,
    ) -> dict[str, Any]:
        user_payload = context.as_prompt_payload()
        if validation_feedback:
            user_payload["validation_feedback"] = validation_feedback
        return {
            "model": self.settings.groq_model,
            "temperature": self.settings.llm_temperature,
            "max_tokens": self.settings.llm_max_output_tokens,
            "messages": [
                {"role": "system", "content": self.prompt},
                {
                    "role": "user",
                    "content": json.dumps(user_payload, ensure_ascii=False),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "quickdrop_multi_complaint_interpretation_v2",
                    "strict": True,
                    "schema": self.schema,
                },
            },
        }


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _extract_json(response_payload: dict[str, Any]) -> dict[str, Any]:
    content = response_payload["choices"][0]["message"]["content"]
    if isinstance(content, dict):
        return content
    if not isinstance(content, str):
        raise TypeError("Model response content is not JSON text")
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise TypeError("Model response root must be an object")
    return parsed
