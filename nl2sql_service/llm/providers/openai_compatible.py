from __future__ import annotations

from collections.abc import AsyncIterator

from nl2sql_service.core.config import settings as default_settings
from nl2sql_service.llm.adapters.openai import OpenAIAdapter
from nl2sql_service.llm.interfaces import GenerateInput, LLMRequest, LLMResponse, ProviderConfig
from nl2sql_service.llm.providers.base import BaseHTTPProvider


class OpenAICompatibleProvider(BaseHTTPProvider):
    def __init__(
        self,
        config: ProviderConfig | None = None,
        *,
        provider: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        default_timeout: int | float = 60,
    ):
        if config is None:
            config = ProviderConfig(
                provider=provider or "openai",
                model=model or "",
                api_key=api_key,
                base_url=base_url,
                timeout=default_timeout,
            )
        super().__init__(config)
        self._llm_client = OpenAIAdapter(config)

    @property
    def _base_url(self) -> str:
        base_url = self.config.base_url or default_settings.openai_default_base_url
        if not base_url:
            raise ValueError(f"No base URL configured for {self.provider_name}")
        return base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            **self.config.extra_headers,
        }
        if self.provider_name == "openrouter":
            headers.setdefault("HTTP-Referer", "https://nl2sql.local")
            headers.setdefault("X-Title", "NL2SQL Service")
        return headers

    async def generate(
        self,
        prompt: str,
        max_tokens: int = 512,
        temperature: float = 0.0,
        enable_thinking: bool = False,
        timeout: int | float | None = None,
        response_format: str | None = None,
    ) -> LLMResponse:
        request = LLMRequest(
            prompt=prompt,
            provider=self.provider_name,
            model=self.model_name,
            max_tokens=max_tokens,
            temperature=temperature,
            enable_thinking=enable_thinking,
            timeout=timeout,
            response_format=response_format,
        )
        response = await self._llm_client.generate(request)
        return _to_legacy_response(response)

    async def stream(self, input_: GenerateInput) -> AsyncIterator[str]:
        request = LLMRequest(
            prompt=input_.prompt,
            provider=self.provider_name,
            model=self.model_name,
            max_tokens=input_.max_tokens,
            temperature=input_.temperature,
            enable_thinking=input_.enable_thinking,
            timeout=input_.timeout,
            response_format=input_.response_format,
            system_prompt=input_.system_prompt,
            metadata=input_.metadata,
        )
        async for chunk in self._llm_client.stream(request):
            if chunk.text:
                yield chunk.text

    async def embeddings(self, input_: list[str]) -> list[list[float]]:
        body = {"model": self.model_name, "input": input_}
        payload, _ = await self._post_json(
            f"{self._base_url}/embeddings",
            headers=self._headers(),
            json_body=body,
            timeout=self.default_timeout,
        )
        data = payload.get("data")
        if not isinstance(data, list):
            raise ValueError("embedding response missing data")
        vectors: list[list[float]] = []
        for item in data:
            if not isinstance(item, dict) or not isinstance(item.get("embedding"), list):
                raise ValueError("embedding item missing vector")
            vectors.append([float(value) for value in item["embedding"]])
        return vectors


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _to_legacy_response(response: LLMResponse) -> LLMResponse:
    return LLMResponse(
        text=response.text,
        thought=response.thought,
        model_name=response.model_name,
        provider=response.provider,
        latency_ms=response.latency_ms,
        tokens_used=response.tokens_used,
        prompt_tokens=response.prompt_tokens,
        completion_tokens=response.completion_tokens,
        estimated_cost_usd=response.estimated_cost_usd,
        retries=response.retries,
        error_type=response.error_type,
        error_message=response.error_message,
        raw=dict(response.raw) if response.raw is not None else None,
    )
