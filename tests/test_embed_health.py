from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_embedding_health_probe_reports_ok_for_configured_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nl2sql_service import embed
    from nl2sql_service.config import settings

    class FakeEmbeddingProvider:
        async def embeddings(self, input_: list[str]) -> list[list[float]]:
            return [[0.1] * settings.embedding_dimension for _ in input_]

    monkeypatch.setattr(settings, "embedding_provider", "openai")
    monkeypatch.setattr(settings, "embedding_model", "text-embedding-3-small")
    monkeypatch.setattr(embed.LLMFactory, "create_embedding_provider", lambda _settings: FakeEmbeddingProvider())

    result = await embed.health_probe()

    assert result["role"] == "embedding"
    assert result["status"] == "ok"
    assert result["healthy"] is True
    assert result["provider"] == "openai"
    assert result["model"] == "text-embedding-3-small"
    assert result["latency_ms"] is not None
    assert result["last_probe_latency_ms"] == result["latency_ms"]


@pytest.mark.asyncio
async def test_embedding_health_probe_returns_degraded_when_custom_provider_missing_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nl2sql_service import embed
    from nl2sql_service.config import settings

    monkeypatch.setattr(settings, "embedding_provider", "custom")
    monkeypatch.setattr(settings, "embedding_api_url", None)

    result = await embed.health_probe()

    assert result["role"] == "embedding"
    assert result["status"] == "degraded"
    assert result["healthy"] is False
    assert "EMBEDDING_API_URL is not configured" in str(result["error_message"])
    assert result["error_type"] == "configuration"
