from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-backed application settings.

    Defaults mirror ``.env.example``. Secrets are represented as ``SecretStr`` so
    accidental model/log serialization does not reveal them.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    def __init__(self, **values: object) -> None:
        """Accept field names in code while reserving ACTIVE_* names for the environment.

        ``validation_alias`` protects deployments with an older ``.env`` from silently
        selecting a retired contract. Pydantic's global ``populate_by_name`` option
        would also re-enable those legacy environment names, so constructor values are
        translated explicitly instead.
        """

        constructor_aliases = {
            "prompt_version": "ACTIVE_PROMPT_VERSION",
            "model_schema_version": "ACTIVE_MODEL_SCHEMA_VERSION",
            "runtime_schema_version": "ACTIVE_RUNTIME_SCHEMA_VERSION",
            "worker_heartbeat_ttl_seconds": "ACTIVE_WORKER_HEARTBEAT_TTL_SECONDS",
        }
        for field_name, active_alias in constructor_aliases.items():
            if field_name in values and active_alias not in values:
                values[active_alias] = values.pop(field_name)
        super().__init__(**values)

    app_env: Literal["development", "test", "production"] = "development"
    app_host: str = "0.0.0.0"
    app_port: int = Field(default=8000, ge=1, le=65535)
    log_level: str = "INFO"
    app_timezone: str = "Asia/Kolkata"
    webhook_hard_deadline_seconds: float = Field(default=8.5, gt=0, le=9.5)
    data_dir: Path = Path("data")
    source_snapshot_version: str = "assignment-export-v1"

    database_url: str = "postgresql+psycopg://quickdrop:quickdrop_dev@postgres:5432/quickdrop"

    llm_provider: Literal["groq", "fake"] = "groq"
    groq_api_key: SecretStr | None = None
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_model: str = "openai/gpt-oss-20b"
    llm_temperature: float = Field(default=0, ge=0, le=2)
    llm_max_output_tokens: int = Field(default=2048, ge=1)
    llm_timeout_seconds: float = Field(default=4, gt=0)
    llm_max_retries: int = Field(default=1, ge=0, le=3)
    model_prompt_path: Path = Path("project-analysis/STRICT_RULES_SYSTEM_PROMPT.v2.md")
    model_schema_path: Path = Path("contracts/model_interpretation.v2.schema.json")
    runtime_schema_path: Path = Path("project-analysis/response_classification.v2.schema.json")
    model_fallback_enabled: bool = True
    message_max_length: int = Field(default=4000, ge=1, le=20000)

    policy_version: str = "payout-policy-v1"
    normalization_version: str = "source-normalization-v1"
    prompt_version: str = Field(default="strict-rules-v2", validation_alias="ACTIVE_PROMPT_VERSION")
    model_schema_version: str = Field(
        default="2.0.0", validation_alias="ACTIVE_MODEL_SCHEMA_VERSION"
    )
    runtime_schema_version: str = Field(
        default="2.0.0", validation_alias="ACTIVE_RUNTIME_SCHEMA_VERSION"
    )

    payswift_base_url: str = "http://payswift:8081"
    payswift_connect_timeout_seconds: float = Field(default=1, gt=0)
    payswift_read_timeout_seconds: float = Field(default=4, gt=0)
    payswift_max_retry_attempts: int = Field(default=6, ge=0)
    payswift_retry_base_seconds: float = Field(default=0.5, gt=0)
    payswift_retry_max_seconds: float = Field(default=30, gt=0)

    worker_poll_interval_seconds: float = Field(default=0.25, gt=0)
    worker_job_lease_seconds: int = Field(default=30, ge=1)
    worker_batch_size: int = Field(default=10, ge=1)
    worker_heartbeat_interval_seconds: float = Field(default=1, gt=0)
    worker_heartbeat_ttl_seconds: float = Field(
        default=20,
        gt=0,
        validation_alias="ACTIVE_WORKER_HEARTBEAT_TTL_SECONDS",
    )

    def validate_api_runtime(self) -> None:
        """Fail startup when the selected production adapter lacks credentials."""

        if self.llm_provider != "groq" or self.app_env == "test":
            return
        if self.groq_api_key is None or not self.groq_api_key.get_secret_value().strip():
            raise ValueError(
                "GROQ_API_KEY is required when LLM_PROVIDER=groq; add it to the ignored .env file"
            )

    def safe_summary(self) -> dict[str, object]:
        """Return non-secret configuration fields suitable for structured logs."""

        return {
            "app_env": self.app_env,
            "app_host": self.app_host,
            "app_port": self.app_port,
            "llm_provider": self.llm_provider,
            "groq_model": self.groq_model,
            "policy_version": self.policy_version,
            "normalization_version": self.normalization_version,
            "source_snapshot_version": self.source_snapshot_version,
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
