from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from nl2sql_service.config import settings as default_settings
from nl2sql_service.llm.interfaces import LLMChunk, LLMRequest, LLMResponse, ProviderConfig
from nl2sql_service.llm.providers.base import BaseHTTPProvider, classify_http_error


class OpenAIAdapter(BaseHTTPProvider):
    """Provider-neutral LLMClient adapter for OpenAI-compatible APIs."""

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

    async def generate(self, request: LLMRequest) -> LLMResponse:  # type: ignore[override]
        start = time.time()
        circuit = self._circuit_open_response(start)
        if circuit:
            return _from_legacy_response(circuit)

        messages: list[dict[str, str]] = []
        if request.enable_thinking:
            messages.append(
                {
                    "role": "system",
                    "content": "Think privately if needed, then return only the requested output.",
                }
            )
        elif request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        messages.append({"role": "user", "content": request.prompt})

        body: dict[str, Any] = {
            "model": request.model or self.model_name,
            "messages": messages,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "stream": False,
        }
        if request.response_format == "json":
            body["response_format"] = {"type": "json_object"}

        try:
            payload, retries = await self._post_json(
                f"{self._base_url}/chat/completions",
                headers=self._headers(),
                json_body=body,
                timeout=request.timeout or self.default_timeout,
            )
            choices = payload.get("choices")
            if not isinstance(choices, list) or not choices:
                raise ValueError("OpenAI-compatible response missing choices")
            message = choices[0].get("message") if isinstance(choices[0], dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                text = "".join(
                    item.get("text", "") for item in content if isinstance(item, dict)
                ).strip()
            elif isinstance(content, str):
                text = content.strip()
            else:
                text = ""
            usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
            self._record_success()
            if not text:
                return self._error_response_new(
                    start=start,
                    error_type="empty",
                    error_message=f"{self.provider_name} returned empty content",
                    retries=retries,
                )
            return self._response_new(
                start=start,
                text=text,
                prompt_tokens=_int_or_none(usage.get("prompt_tokens")),
                completion_tokens=_int_or_none(usage.get("completion_tokens")),
                retries=retries,
                raw=payload,
            )
        except Exception as exc:  # noqa: BLE001
            self._record_failure()
            error_type, message = classify_http_error(exc)
            return self._error_response_new(
                start=start,
                error_type=error_type,
                error_message=f"{self.provider_name} request failed: {message}",
                retries=max(0, self.config.max_retries - 1),
            )

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMChunk]:  # type: ignore[override]
        body: dict[str, Any] = {
            "model": request.model or self.model_name,
            "messages": [{"role": "user", "content": request.prompt}],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "stream": True,
        }
        async with httpx.AsyncClient() as client:
            async with client.stream(
                "POST",
                f"{self._base_url}/chat/completions",
                headers=self._headers(),
                json=body,
                timeout=request.timeout or self.default_timeout,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line.removeprefix("data:").strip()
                    if data == "[DONE]":
                        break
                    try:
                        payload = json.loads(data)
                    except ValueError:
                        continue
                    choices = payload.get("choices")
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {})
                    content = delta.get("content") if isinstance(delta, dict) else None
                    if isinstance(content, str) and content:
                        yield LLMChunk(
                            text=content,
                            model_name=request.model or self.model_name,
                            provider=request.provider or self.provider_name,
                            raw=payload,
                        )

    def _error_response_new(
        self,
        *,
        start: float,
        error_type: str,
        error_message: str,
        retries: int = 0,
    ) -> LLMResponse:
        return _from_legacy_response(
            self._error_response(
                start=start,
                error_type=error_type,
                error_message=error_message,
                retries=retries,
            )
        )

    def _response_new(
        self,
        *,
        start: float,
        text: str,
        thought: str | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        retries: int = 0,
        raw: dict[str, Any] | None = None,
    ) -> LLMResponse:
        return _from_legacy_response(
            self._response(
                start=start,
                text=text,
                thought=thought,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                retries=retries,
                raw=raw,
            )
        )


def _from_legacy_response(response) -> LLMResponse:
    return LLMResponse(
        text=response.text,
        model_name=response.model_name,
        provider=response.provider,
        thought=response.thought,
        latency_ms=response.latency_ms,
        tokens_used=response.tokens_used,
        prompt_tokens=response.prompt_tokens,
        completion_tokens=response.completion_tokens,
        estimated_cost_usd=response.estimated_cost_usd,
        retries=response.retries,
        error_type=response.error_type,
        error_message=response.error_message,
        raw=response.raw,
    )


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) else None
