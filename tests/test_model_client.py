from __future__ import annotations

import pytest

from nl2sql_service import sql_generator
from nl2sql_service.config import settings
from nl2sql_service.llm.interfaces import LLMResponse
from nl2sql_service.llm.providers.ollama import OllamaProvider
from nl2sql_service.models import WarningCode


@pytest.mark.asyncio
async def test_ollama_client_generate_success(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"response": "<think>plan</think>\nSELECT 1"}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, url: str, json: dict, timeout: int) -> FakeResponse:
            assert url == "http://ollama/api/generate"
            assert json["model"] == "deepseek-coder:6.7b"
            assert json["think"] is True
            assert json["format"] == "json"
            assert json["options"]["num_predict"] == 128
            assert timeout == 5
            return FakeResponse()

    from nl2sql_service.llm.providers import ollama

    monkeypatch.setattr(ollama.httpx, "AsyncClient", FakeClient)

    client = OllamaProvider("http://ollama", "deepseek-coder:6.7b", default_timeout=60)
    response = await client.generate(
        "prompt",
        max_tokens=128,
        enable_thinking=True,
        timeout=5,
        response_format="json",
    )

    assert response.text == "SELECT 1"
    assert response.thought == "plan"
    assert response.provider == "ollama"


@pytest.mark.asyncio
async def test_ollama_client_timeout_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, url: str, json: dict, timeout: int):
            del url, json, timeout
            from nl2sql_service.llm.providers import ollama

            raise ollama.httpx.TimeoutException("timeout")

    from nl2sql_service.llm.providers import ollama

    monkeypatch.setattr(ollama.httpx, "AsyncClient", FakeClient)

    client = OllamaProvider("http://ollama", "qwen3:4b", default_timeout=1)
    response = await client.generate("prompt")

    assert response.text == ""
    assert response.model_name == "qwen3:4b"
    assert response.provider == "ollama"
    assert response.error_type == "timeout"
    assert response.error_message == "Ollama request timed out after 1s"


@pytest.mark.asyncio
async def test_sql_wrapper_returns_warning_when_model_client_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class EmptyClient:
        provider_name = "fake"

        async def generate(self, **kwargs):
            del kwargs
            return LLMResponse(text="", provider="fake")

    monkeypatch.setattr(
        sql_generator,
        "get_model_client",
        lambda **kwargs: EmptyClient(),
    )

    raw, warnings = await sql_generator.call_ollama("prompt", settings)

    assert raw is None
    assert warnings[0].code == WarningCode.OLLAMA_UPSTREAM


@pytest.mark.asyncio
async def test_sql_wrapper_maps_timeout_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class EmptyClient:
        provider_name = "fake"

        async def generate(self, **kwargs):
            del kwargs
            return LLMResponse(
                text="",
                provider="fake",
                error_type="timeout",
                error_message="Ollama request timed out after 60s",
            )

    monkeypatch.setattr(
        sql_generator,
        "get_model_client",
        lambda **kwargs: EmptyClient(),
    )

    raw, warnings = await sql_generator.call_ollama("prompt", settings)

    assert raw is None
    assert warnings[0].code == WarningCode.OLLAMA_TIMEOUT
    assert warnings[0].message == "Ollama request timed out after 60s"
