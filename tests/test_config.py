import pytest
from pydantic import ValidationError

from app.config import Settings


def test_groq_key_is_required_for_non_test_api_runtime() -> None:
    settings = Settings(
        _env_file=None,
        app_env="development",
        llm_provider="groq",
        groq_api_key=None,
    )

    with pytest.raises(ValueError, match="GROQ_API_KEY"):
        settings.validate_api_runtime()


def test_fake_provider_allows_secret_free_tests() -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        llm_provider="fake",
        groq_api_key=None,
    )

    settings.validate_api_runtime()


def test_webhook_deadline_cannot_reach_vendor_retry_boundary() -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            app_env="test",
            llm_provider="fake",
            webhook_hard_deadline_seconds=10,
        )


def test_safe_summary_never_contains_api_key() -> None:
    settings = Settings(
        _env_file=None,
        app_env="development",
        llm_provider="groq",
        groq_api_key="not-a-real-secret",
    )

    assert "groq_api_key" not in settings.safe_summary()
    assert "not-a-real-secret" not in str(settings.safe_summary())


def test_legacy_contract_environment_names_cannot_downgrade_v2(monkeypatch) -> None:
    monkeypatch.setenv("PROMPT_VERSION", "strict-rules-v1")
    monkeypatch.setenv("MODEL_SCHEMA_VERSION", "1.0.0")
    monkeypatch.setenv("RUNTIME_SCHEMA_VERSION", "1.0.0")
    monkeypatch.setenv("WORKER_HEARTBEAT_TTL_SECONDS", "5")

    settings = Settings(_env_file=None, app_env="test", llm_provider="fake")

    assert settings.prompt_version == "strict-rules-v2"
    assert settings.model_schema_version == "2.0.0"
    assert settings.runtime_schema_version == "2.0.0"
    assert settings.worker_heartbeat_ttl_seconds == 20


def test_active_contract_environment_names_are_explicit_switches(monkeypatch) -> None:
    monkeypatch.setenv("ACTIVE_PROMPT_VERSION", "strict-rules-v2-review")
    monkeypatch.setenv("ACTIVE_MODEL_SCHEMA_VERSION", "2.0.1")
    monkeypatch.setenv("ACTIVE_RUNTIME_SCHEMA_VERSION", "2.0.1")
    monkeypatch.setenv("ACTIVE_WORKER_HEARTBEAT_TTL_SECONDS", "25")

    settings = Settings(_env_file=None, app_env="test", llm_provider="fake")

    assert settings.prompt_version == "strict-rules-v2-review"
    assert settings.model_schema_version == "2.0.1"
    assert settings.runtime_schema_version == "2.0.1"
    assert settings.worker_heartbeat_ttl_seconds == 25


def test_auto_provider_selects_groq_only_when_key_is_non_blank(tmp_path) -> None:
    from pathlib import Path

    from app.agent.fallback import HeuristicInterpretationProvider
    from app.agent.provider import GroqInterpretationProvider
    from app.conversation.service import ConversationService
    from app.db import Database

    root = Path(__file__).resolve().parents[1]
    database = Database(f"sqlite+pysqlite:///{tmp_path / 'cfg.db'}")
    try:
        offline = Settings(
            _env_file=None,
            app_env="development",
            llm_provider="auto",
            groq_api_key="   ",
        )
        offline.validate_api_runtime()
        assert offline.effective_llm_provider == "fake"
        assert isinstance(
            ConversationService(database, offline).provider,
            HeuristicInterpretationProvider,
        )

        online = Settings(
            _env_file=None,
            app_env="development",
            llm_provider="auto",
            groq_api_key="gsk_test_key_1234567890",
            model_prompt_path=root / "project-analysis/STRICT_RULES_SYSTEM_PROMPT.v2.md",
            model_schema_path=root / "contracts/model_interpretation.v2.schema.json",
        )
        online.validate_api_runtime()
        assert online.effective_llm_provider == "groq"
        assert "gsk_test_key_1234567890" not in str(online.safe_summary())
        assert isinstance(
            ConversationService(database, online).provider,
            GroqInterpretationProvider,
        )
    finally:
        database.dispose()
