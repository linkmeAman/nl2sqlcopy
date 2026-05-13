from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx

from nl2sql_service.config import Settings

logger = logging.getLogger(__name__)


@dataclass
class ModelResponse:
    text: str
    thought: str | None = None
    model_name: str = ""
    provider: str = ""
    latency_ms: int = 0
    tokens_used: int | None = None
    error_type: str | None = None
    error_message: str | None = None


class ModelClient(ABC):
    """
    Abstract interface for all LLM providers.
    Switching providers = change one env var.
    All callers use this interface - never raw HTTP.
    """

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        max_tokens: int = 512,
        temperature: float = 0.0,
        enable_thinking: bool = False,
        timeout: int | None = None,
        response_format: str | None = None,
    ) -> ModelResponse:
        """
        Generate text from a prompt.
        enable_thinking: for models that support chain-of-thought
          (qwen3 think=true, o1 reasoning, etc.)
        Returns ModelResponse with text and optional thought.
        Never raises - returns ModelResponse with empty text
        and sets provider-specific warning in caller.
        """

    @property
    @abstractmethod
    def model_name(self) -> str:
        ...

    @property
    @abstractmethod
    def provider_name(self) -> str:
        ...


def extract_think_block(raw: str) -> tuple[str, str]:
    think_start = raw.find("<think>")
    think_end = raw.find("</think>")
    if think_start != -1 and think_end != -1 and think_start < think_end:
        thought = raw[think_start + len("<think>") : think_end].strip()
        answer = raw[think_end + len("</think>") :].strip()
        return thought, answer

    return "", raw.strip()


class OllamaClient(ModelClient):
    """
    Ollama provider - current default.
    Supports: deepseek-coder:6.7b, qwen3:4b, any Ollama model.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        default_timeout: int = 60,
    ):
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._default_timeout = default_timeout

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def provider_name(self) -> str:
        return "ollama"

    async def generate(
        self,
        prompt: str,
        max_tokens: int = 512,
        temperature: float = 0.0,
        enable_thinking: bool = False,
        timeout: int | None = None,
        response_format: str | None = None,
    ) -> ModelResponse:
        start = time.time()
        try:
            body: dict[str, object] = {
                "model": self._model,
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

            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{self._base_url}/api/generate",
                    json=body,
                    timeout=timeout or self._default_timeout,
                )
                resp.raise_for_status()
                data = resp.json()
                if not isinstance(data, dict):
                    latency = int((time.time() - start) * 1000)
                    return ModelResponse(
                        text="",
                        thought=None,
                        model_name=self._model,
                        provider="ollama",
                        latency_ms=latency,
                        error_type="malformed",
                        error_message="Ollama response was not a JSON object",
                    )
                raw = data.get("response", "")
                thinking = data.get("thinking", "")
                if not isinstance(raw, str):
                    latency = int((time.time() - start) * 1000)
                    return ModelResponse(
                        text="",
                        thought=None,
                        model_name=self._model,
                        provider="ollama",
                        latency_ms=latency,
                        error_type="malformed",
                        error_message="Ollama response missing string 'response' field",
                    )

            thought, text = extract_think_block(raw)
            if isinstance(thinking, str) and thinking.strip():
                thought = thinking.strip()
            latency = int((time.time() - start) * 1000)
            if not text and not thought:
                return ModelResponse(
                    text="",
                    thought=None,
                    model_name=self._model,
                    provider="ollama",
                    latency_ms=latency,
                    error_type="empty",
                    error_message="Ollama returned empty response and no thinking field",
                )
            return ModelResponse(
                text=text,
                thought=thought if enable_thinking else None,
                model_name=self._model,
                provider="ollama",
                latency_ms=latency,
            )
        except httpx.TimeoutException:
            timeout_value = timeout or self._default_timeout
            return ModelResponse(
                text="",
                thought=None,
                model_name=self._model,
                provider="ollama",
                latency_ms=int((time.time() - start) * 1000),
                error_type="timeout",
                error_message=f"Ollama request timed out after {timeout_value}s",
            )
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            body = exc.response.text[:200]
            return ModelResponse(
                text="",
                thought=None,
                model_name=self._model,
                provider="ollama",
                latency_ms=int((time.time() - start) * 1000),
                error_type="upstream",
                error_message=f"Ollama returned HTTP {status_code}: {body}",
            )
        except httpx.RequestError as exc:
            return ModelResponse(
                text="",
                thought=None,
                model_name=self._model,
                provider="ollama",
                latency_ms=int((time.time() - start) * 1000),
                error_type="upstream",
                error_message=f"Ollama request failed: {exc}",
            )
        except ValueError:
            return ModelResponse(
                text="",
                thought=None,
                model_name=self._model,
                provider="ollama",
                latency_ms=int((time.time() - start) * 1000),
                error_type="malformed",
                error_message="Ollama response was not valid JSON",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("OllamaClient.generate failed: %s", exc)
            return ModelResponse(
                text="",
                thought=None,
                model_name=self._model,
                provider="ollama",
                latency_ms=int((time.time() - start) * 1000),
                error_type="upstream",
                error_message=f"Ollama client failed: {exc}",
            )


class UnsupportedModelClient(ModelClient):
    def __init__(self, provider: str, model: str):
        self._provider = provider
        self._model = model

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def provider_name(self) -> str:
        return self._provider

    async def generate(
        self,
        prompt: str,
        max_tokens: int = 512,
        temperature: float = 0.0,
        enable_thinking: bool = False,
        timeout: int | None = None,
        response_format: str | None = None,
    ) -> ModelResponse:
        del prompt, max_tokens, temperature, enable_thinking, timeout, response_format
        logger.warning("Unsupported LLM provider: %s", self._provider)
        return ModelResponse(
            text="",
            thought=None,
            model_name=self._model,
            provider=self._provider,
            error_type="unsupported_provider",
            error_message=f"Unsupported LLM provider: {self._provider}",
        )


def get_model_client(
    settings: Settings,
    model: str,
    default_timeout: int,
) -> ModelClient:
    provider = settings.llm_provider.lower().strip()
    if provider == "ollama":
        return OllamaClient(
            base_url=settings.llm_base_url,
            model=model,
            default_timeout=default_timeout,
        )
    return UnsupportedModelClient(provider=provider, model=model)
