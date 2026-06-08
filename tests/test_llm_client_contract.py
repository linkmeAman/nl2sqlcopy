from __future__ import annotations

import pytest

from nl2sql_service.llm.adapters.openai import OpenAIAdapter
from nl2sql_service.llm.client import LLMClient
from nl2sql_service.llm.interfaces import LLMChunk, LLMRequest, LLMResponse, ProviderConfig
from nl2sql_service.llm.providers.openai_compatible import OpenAICompatibleProvider


class FakeLLMClient:
    async def generate(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            text=f"echo: {request.prompt}",
            model_name=request.model,
            provider=request.provider,
            raw={"ok": True},
        )


def test_llm_request_response_and_chunk_shapes() -> None:
    request = LLMRequest(
        prompt="Return SQL",
        provider="openai",
        model="gpt-test",
        max_tokens=128,
        temperature=0.1,
        response_format="json",
    )
    response = LLMResponse(text="SELECT 1", model_name="gpt-test", provider="openai")
    chunk = LLMChunk(text="SEL", model_name="gpt-test", provider="openai")

    assert request.prompt == "Return SQL"
    assert request.model == "gpt-test"
    assert response.text == "SELECT 1"
    assert chunk.text == "SEL"


@pytest.mark.asyncio
async def test_llm_client_protocol_accepts_structural_client() -> None:
    client: LLMClient = FakeLLMClient()

    response = await client.generate(
        LLMRequest(prompt="hello", provider="fake", model="fake-model")
    )

    assert response.text == "echo: hello"
    assert response.model == "fake-model"
    assert response.provider == "fake"


@pytest.mark.asyncio
async def test_openai_adapter_maps_chat_completion_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = OpenAIAdapter(
        ProviderConfig(
            provider="openai",
            model="gpt-test",
            api_key="test-key",
            max_retries=1,
        )
    )

    async def fake_post_json(*args, **kwargs):
        del args, kwargs
        return {
            "choices": [{"message": {"content": "SELECT 1"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        }, 0

    monkeypatch.setattr(adapter, "_post_json", fake_post_json)

    response = await adapter.generate(
        LLMRequest(prompt="sql", provider="openai", model="gpt-test")
    )

    assert response.text == "SELECT 1"
    assert response.model_name == "gpt-test"
    assert response.provider == "openai"
    assert response.prompt_tokens == 3
    assert response.completion_tokens == 2


@pytest.mark.asyncio
async def test_openai_compatible_provider_preserves_legacy_response_shape() -> None:
    provider = OpenAICompatibleProvider(
        config=ProviderConfig(
            provider="openai",
            model="gpt-test",
            api_key="test-key",
        )
    )

    class FakeClient:
        async def generate(self, request: LLMRequest) -> LLMResponse:
            assert request.prompt == "sql"
            assert request.model == "gpt-test"
            assert request.max_tokens == 7
            return LLMResponse(
                text="SELECT 1",
                model_name=request.model,
                provider=request.provider,
                prompt_tokens=1,
                completion_tokens=2,
                raw={"ok": True},
            )

    provider._llm_client = FakeClient()

    response = await provider.generate("sql", max_tokens=7)

    assert response.text == "SELECT 1"
    assert response.model_name == "gpt-test"
    assert response.provider == "openai"
    assert response.prompt_tokens == 1
    assert response.completion_tokens == 2
    assert response.raw == {"ok": True}
