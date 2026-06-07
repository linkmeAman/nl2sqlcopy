from __future__ import annotations

import time

from nl2sql_service.config import settings as default_settings
from nl2sql_service.llm.interfaces import LLMResponse, ProviderConfig
from nl2sql_service.llm.providers.base import BaseHTTPProvider, classify_http_error


class VoyageProvider(BaseHTTPProvider):
    def __init__(self, config: ProviderConfig):
        super().__init__(config)

    @property
    def _base_url(self) -> str:
        return (self.config.base_url or default_settings.voyage_default_base_url).rstrip("/")

    async def generate(
        self,
        prompt: str,
        max_tokens: int = 512,
        temperature: float = 0.0,
        enable_thinking: bool = False,
        timeout: int | float | None = None,
        response_format: str | None = None,
    ) -> LLMResponse:
        del prompt, max_tokens, temperature, enable_thinking, timeout, response_format
        return LLMResponse(
            text="",
            model_name=self.model_name,
            provider=self.provider_name,
            error_type="unsupported_provider",
            error_message="VoyageAI is supported for embeddings, not text generation",
        )

    async def embeddings(self, input_: list[str]) -> list[list[float]]:
        start = time.time()
        try:
            payload, _ = await self._post_json(
                f"{self._base_url}/embeddings",
                headers={
                    "Authorization": f"Bearer {self.config.api_key}",
                    "Content-Type": "application/json",
                    **self.config.extra_headers,
                },
                json_body={"model": self.model_name, "input": input_},
                timeout=self.default_timeout,
            )
            data = payload.get("data")
            if not isinstance(data, list):
                raise ValueError("Voyage embedding response missing data")
            return [
                [float(value) for value in item["embedding"]]
                for item in data
                if isinstance(item, dict) and isinstance(item.get("embedding"), list)
            ]
        except Exception as exc:  # noqa: BLE001
            error_type, message = classify_http_error(exc)
            raise RuntimeError(
                f"VoyageAI embedding failed ({error_type}) after "
                f"{int((time.time() - start) * 1000)}ms: {message}"
            ) from exc
