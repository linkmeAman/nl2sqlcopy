from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from nl2sql_service.core.config import settings as default_settings


@dataclass(frozen=True)
class ProviderConfig:
    provider: str
    model: str
    api_key: str | None = None
    base_url: str | None = None
    timeout: int | float = 60
    max_retries: int = 2
    retry_base_delay: float = 0.5
    temperature: float = 0.0
    max_tokens: int = 512
    role: str = "default"
    extra_headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class GenerateInput:
    prompt: str
    max_tokens: int = 512
    temperature: float = 0.0
    enable_thinking: bool = False
    timeout: int | float | None = None
    response_format: str | None = None
    system_prompt: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LLMRequest:
    prompt: str
    provider: str
    model: str
    max_tokens: int = 512
    temperature: float = 0.0
    enable_thinking: bool = False
    timeout: int | float | None = None
    response_format: str | None = None
    system_prompt: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class LLMResponse:
    text: str
    thought: str | None = None
    model_name: str = ""
    provider: str = ""
    latency_ms: int = 0
    tokens_used: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    estimated_cost_usd: float | None = None
    retries: int = 0
    error_type: str | None = None
    error_message: str | None = None
    raw: dict[str, Any] | None = None

    @property
    def model(self) -> str:
        return self.model_name


@dataclass(frozen=True)
class LLMChunk:
    text: str
    model_name: str = ""
    provider: str = ""
    finish_reason: str | None = None
    raw: Mapping[str, Any] | None = None

    @property
    def model(self) -> str:
        return self.model_name


class LLMProvider(ABC):
    """Provider-independent LLM and embedding interface."""

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        max_tokens: int = 512,
        temperature: float = 0.0,
        enable_thinking: bool = False,
        timeout: int | float | None = None,
        response_format: str | None = None,
    ) -> LLMResponse:
        ...

    async def generate_input(self, input_: GenerateInput) -> LLMResponse:
        return await self.generate(
            prompt=input_.prompt,
            max_tokens=input_.max_tokens,
            temperature=input_.temperature,
            enable_thinking=input_.enable_thinking,
            timeout=input_.timeout,
            response_format=input_.response_format,
        )

    async def stream(
        self,
        input_: GenerateInput,
    ) -> AsyncIterator[str]:
        response = await self.generate_input(input_)
        if response.text:
            yield response.text

    async def embeddings(self, input_: list[str]) -> list[list[float]]:
        del input_
        raise NotImplementedError(f"{self.provider_name} does not support embeddings")

    async def health(self) -> dict[str, object]:
        response = await self.generate(
            "Return exactly: OK",
            max_tokens=default_settings.health_probe_max_tokens,
            temperature=0.0,
            timeout=min(
                float(self.default_timeout),
                default_settings.health_probe_timeout_clamp,
            ),
        )
        return {
            "provider": self.provider_name,
            "model": self.model_name,
            "status": "ok" if response.text else "unavailable",
            "latency_ms": response.latency_ms,
            "error_type": response.error_type,
            "error_message": response.error_message,
        }

    @property
    @abstractmethod
    def model_name(self) -> str:
        ...

    @property
    @abstractmethod
    def provider_name(self) -> str:
        ...

    @property
    @abstractmethod
    def default_timeout(self) -> int | float:
        ...


def extract_think_block(raw: str) -> tuple[str, str]:
    think_start = raw.find("<think>")
    think_end = raw.find("</think>")
    if think_start != -1 and think_end != -1 and think_start < think_end:
        thought = raw[think_start + len("<think>") : think_end].strip()
        answer = raw[think_end + len("</think>") :].strip()
        return thought, answer

    return "", raw.strip()
