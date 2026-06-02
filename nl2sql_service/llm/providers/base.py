from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from nl2sql_service.llm.interfaces import LLMProvider, LLMResponse, ProviderConfig
from nl2sql_service.llm.metrics import estimate_cost_usd

logger = logging.getLogger(__name__)

_CIRCUITS: dict[str, tuple[int, float]] = {}
_CIRCUIT_FAILURE_THRESHOLD = 3
_CIRCUIT_OPEN_SECONDS = 30.0


class BaseHTTPProvider(LLMProvider):
    def __init__(self, config: ProviderConfig):
        self.config = config

    @property
    def model_name(self) -> str:
        return self.config.model

    @property
    def provider_name(self) -> str:
        return self.config.provider

    @property
    def default_timeout(self) -> int | float:
        return self.config.timeout

    @property
    def _circuit_key(self) -> str:
        return f"{self.provider_name}:{self.model_name}"

    def _circuit_open_response(self, start: float) -> LLMResponse | None:
        failures, opened_until = _CIRCUITS.get(self._circuit_key, (0, 0.0))
        del failures
        now = time.time()
        if opened_until > now:
            return self._error_response(
                start=start,
                error_type="circuit_open",
                error_message=(
                    f"Circuit open for {self.provider_name}/{self.model_name}; "
                    f"retry after {int(opened_until - now)}s"
                ),
            )
        return None

    def _record_success(self) -> None:
        _CIRCUITS.pop(self._circuit_key, None)

    def _record_failure(self) -> None:
        failures, _ = _CIRCUITS.get(self._circuit_key, (0, 0.0))
        failures += 1
        opened_until = (
            time.time() + _CIRCUIT_OPEN_SECONDS
            if failures >= _CIRCUIT_FAILURE_THRESHOLD
            else 0.0
        )
        _CIRCUITS[self._circuit_key] = (failures, opened_until)

    def _error_response(
        self,
        *,
        start: float,
        error_type: str,
        error_message: str,
        retries: int = 0,
    ) -> LLMResponse:
        return LLMResponse(
            text="",
            thought=None,
            model_name=self.model_name,
            provider=self.provider_name,
            latency_ms=int((time.time() - start) * 1000),
            retries=retries,
            error_type=error_type,
            error_message=error_message,
        )

    def _response(
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
        total_tokens = (
            (prompt_tokens or 0) + (completion_tokens or 0)
            if prompt_tokens is not None or completion_tokens is not None
            else None
        )
        cost = estimate_cost_usd(
            self.provider_name,
            self.model_name,
            prompt_tokens,
            completion_tokens,
        )
        return LLMResponse(
            text=text,
            thought=thought,
            model_name=self.model_name,
            provider=self.provider_name,
            latency_ms=int((time.time() - start) * 1000),
            tokens_used=total_tokens,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            estimated_cost_usd=cost,
            retries=retries,
            raw=raw,
        )

    async def _post_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None,
        json_body: dict[str, Any],
        timeout: int | float,
    ) -> tuple[dict[str, Any], int]:
        retries = 0
        last_error: Exception | None = None
        for attempt in range(max(1, self.config.max_retries)):
            retries = attempt
            try:
                async with httpx.AsyncClient() as client:
                    request_kwargs: dict[str, Any] = {
                        "json": json_body,
                        "timeout": timeout,
                    }
                    if headers is not None:
                        request_kwargs["headers"] = headers
                    response = await client.post(url, **request_kwargs)
                status_code = getattr(response, "status_code", 200)
                if status_code in {408, 409, 425, 429} or status_code >= 500:
                    if attempt + 1 < max(1, self.config.max_retries):
                        await asyncio.sleep(self.config.retry_base_delay * (2**attempt))
                        continue
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("Provider response was not a JSON object")
                return payload, retries
            except (httpx.TimeoutException, httpx.RequestError, httpx.HTTPStatusError, ValueError) as exc:
                last_error = exc
                if attempt + 1 < max(1, self.config.max_retries):
                    await asyncio.sleep(self.config.retry_base_delay * (2**attempt))
                    continue
                raise
        if last_error:
            raise last_error
        raise RuntimeError("provider request failed")


def classify_http_error(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, httpx.TimeoutException):
        return "timeout", f"request timed out: {exc}"
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        body = exc.response.text[:300]
        if status == 429:
            return "rate_limited", f"HTTP 429 rate limit from provider: {body}"
        if status in {401, 403}:
            return "credentials", f"HTTP {status} authentication/authorization failure: {body}"
        return "upstream", f"HTTP {status} from provider: {body}"
    if isinstance(exc, ValueError):
        return "malformed", str(exc)
    return "upstream", str(exc)
