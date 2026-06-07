from __future__ import annotations

import time
from typing import Any
from urllib.parse import quote

from nl2sql_service.config import settings as default_settings
from nl2sql_service.llm.interfaces import LLMResponse, ProviderConfig
from nl2sql_service.llm.providers.base import BaseHTTPProvider, classify_http_error


class GeminiProvider(BaseHTTPProvider):
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
                provider="gemini",
                model=model or "",
                api_key=api_key,
                base_url=base_url,
                timeout=default_timeout,
            )
        super().__init__(config)

    @property
    def _base_url(self) -> str:
        return (self.config.base_url or default_settings.gemini_default_base_url).rstrip("/")

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
        generation_config: dict[str, Any] = {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        }
        if not enable_thinking and self.model_name.startswith("gemini-2.5"):
            generation_config["thinkingConfig"] = {"thinkingBudget": 0}
        if response_format == "json":
            generation_config["responseMimeType"] = "application/json"
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": generation_config,
        }
        url = (
            f"{self._base_url}/models/{quote(self.model_name, safe='')}:generateContent"
            f"?key={quote(self.config.api_key or '', safe='')}"
        )
        try:
            payload, retries = await self._post_json(
                url,
                headers={"content-type": "application/json", **self.config.extra_headers},
                json_body=body,
                timeout=timeout or self.default_timeout,
            )
            candidates = payload.get("candidates")
            if not isinstance(candidates, list) or not candidates:
                block_reason = _prompt_feedback_reason(payload)
                return self._error_response(
                    start=start,
                    error_type="blocked" if block_reason else "malformed",
                    error_message=(
                        f"Gemini returned no candidates: {block_reason}"
                        if block_reason
                        else "Gemini response missing candidates"
                    ),
                    retries=retries,
                )

            candidate = candidates[0] if isinstance(candidates[0], dict) else {}
            content = candidate.get("content") if isinstance(candidate, dict) else None
            parts = content.get("parts") if isinstance(content, dict) else None
            finish_reason = candidate.get("finishReason") if isinstance(candidate, dict) else None
            if not isinstance(parts, list):
                detail = _candidate_detail(candidate, payload)
                return self._error_response(
                    start=start,
                    error_type="blocked" if finish_reason == "SAFETY" else "empty",
                    error_message=f"Gemini returned no text parts{detail}",
                    retries=retries,
                )

            text = "".join(
                part.get("text", "") for part in parts if isinstance(part, dict)
            ).strip()
            usage = (
                payload.get("usageMetadata")
                if isinstance(payload.get("usageMetadata"), dict)
                else {}
            )
            self._record_success()
            if not text:
                detail = _candidate_detail(candidate, payload)
                return self._error_response(
                    start=start,
                    error_type="blocked" if finish_reason == "SAFETY" else "empty",
                    error_message=f"Gemini returned empty content{detail}",
                    retries=retries,
                )
            return self._response(
                start=start,
                text=text,
                prompt_tokens=_int_or_none(usage.get("promptTokenCount")),
                completion_tokens=_int_or_none(usage.get("candidatesTokenCount")),
                retries=retries,
                raw=payload,
            )
        except Exception as exc:  # noqa: BLE001
            self._record_failure()
            error_type, message = classify_http_error(exc)
            return self._error_response(
                start=start,
                error_type=error_type,
                error_message=f"Gemini request failed: {message}",
                retries=max(0, self.config.max_retries - 1),
            )

    async def embeddings(self, input_: list[str]) -> list[list[float]]:
        url = (
            f"{self._base_url}/models/{quote(self.model_name, safe='')}:batchEmbedContents"
            f"?key={quote(self.config.api_key or '', safe='')}"
        )
        payload, _ = await self._post_json(
            url,
            headers={"content-type": "application/json", **self.config.extra_headers},
            json_body={
                "requests": [
                    {
                        "model": f"models/{self.model_name}",
                        "content": {"parts": [{"text": text}]},
                    }
                    for text in input_
                ]
            },
            timeout=self.default_timeout,
        )
        embeddings = payload.get("embeddings")
        if not isinstance(embeddings, list):
            raise ValueError("Gemini embedding response missing embeddings")
        vectors: list[list[float]] = []
        for item in embeddings:
            values = item.get("values") if isinstance(item, dict) else None
            if not isinstance(values, list):
                raise ValueError("Gemini embedding item missing values")
            vectors.append([float(value) for value in values])
        return vectors


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _prompt_feedback_reason(payload: dict[str, Any]) -> str | None:
    feedback = payload.get("promptFeedback")
    if not isinstance(feedback, dict):
        return None
    reason = feedback.get("blockReason")
    return str(reason) if reason else None


def _candidate_detail(candidate: dict[str, Any], payload: dict[str, Any]) -> str:
    details: list[str] = []
    finish_reason = candidate.get("finishReason")
    if finish_reason:
        details.append(f"finishReason={finish_reason}")
    block_reason = _prompt_feedback_reason(payload)
    if block_reason:
        details.append(f"blockReason={block_reason}")
    safety = candidate.get("safetyRatings")
    if isinstance(safety, list) and safety:
        blocked = [
            str(item.get("category"))
            for item in safety
            if isinstance(item, dict) and item.get("blocked") is True
        ]
        if blocked:
            details.append(f"blockedSafetyCategories={','.join(blocked)}")
    return f" ({'; '.join(details)})" if details else ""
