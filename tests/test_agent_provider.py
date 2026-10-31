import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

from app.agent.contracts import InterpretationContext, MessageEnvelope
from app.agent.provider import GroqInterpretationProvider
from app.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def test_groq_provider_retries_invalid_schema_and_attaches_strict_prompt() -> None:
    example = json.loads((ROOT / "contracts/model_interpretation.v2.example.json").read_text())
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        content = "{}" if len(requests) == 1 else json.dumps(example)
        return httpx.Response(
            200,
            headers={"x-request-id": "req_test"},
            json={"choices": [{"message": {"content": content}}]},
        )

    settings = Settings(
        _env_file=None,
        app_env="test",
        llm_provider="groq",
        groq_api_key="test-key",
        llm_max_retries=1,
        model_prompt_path=ROOT / "project-analysis/STRICT_RULES_SYSTEM_PROMPT.v2.md",
        model_schema_path=ROOT / "contracts/model_interpretation.v2.schema.json",
    )
    client = httpx.Client(
        base_url="https://api.groq.test",
        transport=httpx.MockTransport(handler),
    )
    provider = GroqInterpretationProvider(settings, client=client)

    result = provider.interpret(
        InterpretationContext(MessageEnvelope("msg-1", "R027", "two complaints", datetime.now(UTC)))
    )

    assert result.attempt_count == 2
    assert len(result.interpretation.complaints) == 2
    assert "MULTI-COMPLAINT EXTRACTION" in requests[0]["messages"][0]["content"]  # type: ignore[index]
    assert requests[0]["response_format"]["json_schema"]["strict"] is True  # type: ignore[index]
