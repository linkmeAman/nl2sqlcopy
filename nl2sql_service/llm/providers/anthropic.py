from __future__ import annotations

import time
from typing import Any

from nl2sql_service.core.config import settings as default_settings
from nl2sql_service.llm.interfaces import LLMResponse, ProviderConfig
from nl2sql_service.llm.providers.base import BaseHTTPProvider, classify_http_error


class AnthropicProvider(BaseHTTPProvider):
    def __init__(
        self,
        config: ProviderConfig | None = None,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        default_timeout: int | float = 60,
    ):
        if config is None:
            config = ProviderConfig(
                provider="anthropic",
                model=model or "",
                api_key=api_key,
                base_url=base_url,
                timeout=default_timeout,
            )
        super().__init__(config)

    @property
    def _base_url(self) -> str:
        return (self.config.base_url or default_settings.anthropic_default_base_url).rstrip("/")

    async def generate(
        self,
        prompt: str,
        max_tokens: int = 512,
        temperature: float = 0.0,
        enable_thinking: bool = False,
        timeout: int | float | None = None,
        response_format: str | None = None,
    ) -> LLMResponse:
        del response_format
        start = time.time()
        circuit = self._circuit_open_response(start)
        if circuit:
            return circuit
        system = "Return only the requested output."
        if enable_thinking:
            system += " Think privately if useful; do not expose private reasoning."
        body: dict[str, Any] = {
            "model": self.model_name,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }
        headers = {
            "x-api-key": self.config.api_key or "",
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            **self.config.extra_headers,
        }
        try:
            payload, retries = await self._post_json(
                f"{self._base_url}/messages",
                headers=headers,
                json_body=body,
                timeout=timeout or self.default_timeout,
            )
            content = payload.get("content")
            if not isinstance(content, list):
                raise ValueError("Anthropic response missing content")
            text = "".join(
                block.get("text", "") for block in content if isinstance(block, dict)
            ).strip()
            usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
            self._record_success()
            if not text:
                return self._error_response(
                    start=start,
                    error_type="empty",
                    error_message="Anthropic returned empty content",
                    retries=retries,
                )
            return self._response(
                start=start,
                text=text,
                prompt_tokens=_int_or_none(usage.get("input_tokens")),
                completion_tokens=_int_or_none(usage.get("output_tokens")),
                retries=retries,
                raw=payload,
            )
        except Exception as exc:  # noqa: BLE001
            self._record_failure()
            error_type, message = classify_http_error(exc)
            return self._error_response(
                start=start,
                error_type=error_type,
                error_message=f"Anthropic request failed: {message}",
                retries=max(0, self.config.max_retries - 1),
            )


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) else None
