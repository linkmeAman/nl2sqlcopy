from __future__ import annotations

from pydantic import ValidationError
import pytest

from nl2sql_service.config import Settings
from nl2sql_service.models import ModelRoutingPatchRequest


def test_settings_require_explicit_ollama_base_url() -> None:
    with pytest.raises(ValidationError, match="requires an explicit base URL"):
        Settings(
            _env_file=None,
            database_url="postgresql://user:pass@localhost:5432/ragdb",
            embedding_api_url="http://embed.local/embed",
        )


def test_settings_allow_non_custom_embedding_without_embedding_api_url() -> None:
    config = Settings(
        _env_file=None,
        database_url="postgresql://user:pass@localhost:5432/ragdb",
        llm_base_url="http://localhost:11434",
        embedding_provider="openai",
        embedding_api_key="test-key",
    )

    report = config.provider_readiness_report()
    assert report["status"] == "ok"
    embedding_targets = [
        target for target in report["targets"] if target["target"] == "EMBEDDING_PROVIDER"
    ]
    assert embedding_targets[0]["provider"] == "openai"


def test_settings_validate_secret_references_for_cloud_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MISSING_OPENAI_KEY", raising=False)

    with pytest.raises(ValidationError, match="requires a resolved API key"):
        Settings(
            _env_file=None,
            database_url="postgresql://user:pass@localhost:5432/ragdb",
            llm_provider="openai",
            llm_model="gpt-4.1-mini",
            llm_api_key="env:MISSING_OPENAI_KEY",
            embedding_api_url="http://embed.local/embed",
            llm_base_url="http://localhost:11434",
        )


def test_settings_validate_fallback_provider_configuration() -> None:
    with pytest.raises(ValidationError, match="QUERY_REWRITE_FALLBACK_PROVIDER requires a resolved API key"):
        Settings(
            _env_file=None,
            database_url="postgresql://user:pass@localhost:5432/ragdb",
            embedding_api_url="http://embed.local/embed",
            llm_base_url="http://localhost:11434",
            query_rewrite_fallback_provider="openai",
            query_rewrite_fallback_model="gpt-4.1-mini",
        )


def test_settings_reject_invalid_startup_enforcement_mode() -> None:
    with pytest.raises(ValidationError, match="STARTUP_ENFORCEMENT_MODE must be one of: warn, strict"):
        Settings(
            _env_file=None,
            database_url="postgresql://user:pass@localhost:5432/ragdb",
            embedding_api_url="http://embed.local/embed",
            llm_base_url="http://localhost:11434",
            startup_enforcement_mode="block_all",
        )


def test_model_routing_patch_request_ignores_unknown_fields() -> None:
    payload = ModelRoutingPatchRequest(
        llm_model="gpt-4.1-mini",
        unknown_field="ignored",
    )

    assert payload.llm_model == "gpt-4.1-mini"
