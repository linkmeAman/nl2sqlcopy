from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from nl2sql_service.config import settings as default_settings
from nl2sql_service.llm.interfaces import GenerateInput, LLMResponse, ProviderConfig, extract_think_block
from nl2sql_service.llm.providers.base import BaseHTTPProvider, classify_http_error


class OllamaProvider(BaseHTTPProvider):
    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        default_timeout: int | float = 60,
        config: ProviderConfig | None = None,
    ):
        if config is None:
            config = ProviderConfig(
                provider="ollama",
                model=model or "",
                base_url=base_url or default_settings.ollama_default_base_url,
                timeout=default_timeout,
            )
        super().__init__(config)

    @property
    def _base_url(self) -> str:
        return (self.config.base_url or default_settings.ollama_default_base_url).rstrip("/")

    async def generate(
        self,
        prompt: str,
        max_tokens: int = 512,
        temperature: float = 0.0,
        enable_thinking: bool = False,
        timeout: int | float | None = None,
        response_format: str | None = None,
    ) -> LLMResponse:
        start = time.time()
        circuit = self._circuit_open_response(start)
        if circuit:
            return circuit
        body: dict[str, object] = {
            "model": self.model_name,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }
        if enable_thinking:
            body["think"] = True
        if response_format:
            body["format"] = response_format

        try:
            payload, retries = await self._post_json(
                f"{self._base_url}/api/generate",
                headers=None,
                json_body=body,
                timeout=timeout or self.default_timeout,
            )
            raw = payload.get("response", "")
            thinking = payload.get("thinking", "")
            if not isinstance(raw, str):
                raise ValueError("Ollama response missing string 'response' field")
            thought, text = extract_think_block(raw)
            if isinstance(thinking, str) and thinking.strip():
                thought = thinking.strip()
            self._record_success()
            if not text and not thought:
                return self._error_response(
                    start=start,
                    error_type="empty",
                    error_message="Ollama returned empty response and no thinking field",
                    retries=retries,
                )
            return self._response(
                start=start,
                text=text,
                thought=thought if enable_thinking else None,
                prompt_tokens=_int_or_none(payload.get("prompt_eval_count")),
                completion_tokens=_int_or_none(payload.get("eval_count")),
                retries=retries,
                raw=payload,
            )
        except Exception as exc:  # noqa: BLE001
            self._record_failure()
            error_type, message = classify_http_error(exc)
            if error_type == "timeout":
                message = f"Ollama request timed out after {timeout or self.default_timeout}s"
            return self._error_response(
                start=start,
                error_type=error_type,
                error_message=message,
                retries=max(0, self.config.max_retries - 1),
            )

    async def stream(self, input_: GenerateInput) -> AsyncIterator[str]:
        body: dict[str, Any] = {
            "model": self.model_name,
            "prompt": input_.prompt,
            "stream": True,
            "options": {
                "temperature": input_.temperature,
                "num_predict": input_.max_tokens,
            },
        }
        async with httpx.AsyncClient() as client:
            async with client.stream(
                "POST",
                f"{self._base_url}/api/generate",
                json=body,
                timeout=input_.timeout or self.default_timeout,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except ValueError:
                        continue
                    chunk = payload.get("response")
                    if isinstance(chunk, str) and chunk:
                        yield chunk
                    if payload.get("done") is True:
                        break

    async def embeddings(self, input_: list[str]) -> list[list[float]]:
        payload, _ = await self._post_json(
            f"{self._base_url}/api/embed",
            headers=None,
            json_body={"model": self.model_name, "input": input_},
            timeout=self.default_timeout,
        )
        vectors = payload.get("embeddings")
        if not isinstance(vectors, list):
            raise ValueError("Ollama embedding response missing embeddings")
        return [[float(value) for value in vector] for vector in vectors]


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) else None
