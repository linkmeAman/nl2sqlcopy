from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Any

from nl2sql_service.config import Settings
from nl2sql_service.llm.interfaces import LLMProvider, LLMResponse, ProviderConfig
from nl2sql_service.llm.metrics import record_llm_response
from nl2sql_service.observability.context import emit_current_trace_event
from nl2sql_service.observability.metrics import observe_provider
from nl2sql_service.observability.sanitization import stable_hash
from nl2sql_service.provider_registry import (
    normalize_provider_name,
    provider_compat,
    provider_requires_key,
)
from nl2sql_service.llm.providers import (
    AnthropicProvider,
    GeminiProvider,
    OllamaProvider,
    OpenAICompatibleProvider,
    VoyageProvider,
)


class UnsupportedModelClient(LLMProvider):
    def __init__(self, provider: str, model: str):
        self._provider = provider
        self._model = model

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def provider_name(self) -> str:
        return self._provider

    @property
    def default_timeout(self) -> int | float:
        return 0

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
            thought=None,
            model_name=self._model,
            provider=self._provider,
            error_type="unsupported_provider",
            error_message=f"Unsupported LLM provider: {self._provider}",
        )


class FallbackLLMProvider(LLMProvider):
    def __init__(self, role: str, providers: list[LLMProvider]):
        self._role = role
        self._providers = providers

    @property
    def model_name(self) -> str:
        return self._providers[0].model_name

    @property
    def provider_name(self) -> str:
        return self._providers[0].provider_name

    @property
    def default_timeout(self) -> int | float:
        return self._providers[0].default_timeout

    async def generate(
        self,
        prompt: str,
        max_tokens: int = 512,
        temperature: float = 0.0,
        enable_thinking: bool = False,
        timeout: int | float | None = None,
        response_format: str | None = None,
    ) -> LLMResponse:
        last_response: LLMResponse | None = None
        for index, provider in enumerate(self._providers):
            await emit_current_trace_event(
                event="llm_request_started",
                stage="llm_provider",
                status="started",
                message="LLM provider request started.",
                provider=provider.provider_name,
                model=provider.model_name,
                retry_count=index,
                input_summary={
                    "role": self._role,
                    "prompt_hash": stable_hash(prompt),
                    "prompt_chars": len(prompt),
                    "max_tokens": max_tokens,
                    "response_format": response_format,
                },
                metadata={"fallback_attempt": index > 0},
            )
            response = await provider.generate(
                prompt=prompt,
                max_tokens=max_tokens,
                temperature=temperature,
                enable_thinking=enable_thinking,
                timeout=timeout,
                response_format=response_format,
            )
            record_llm_response(self._role, response)
            outcome = "ok" if response.text else (response.error_type or "empty")
            observe_provider(self._role, response.provider or provider.provider_name, response.model_name or provider.model_name, outcome)
            await emit_current_trace_event(
                event="llm_request_completed",
                stage="llm_provider",
                status="completed" if response.text else "failed",
                message="LLM provider request completed." if response.text else "LLM provider request failed.",
                duration_ms=response.latency_ms,
                provider=response.provider or provider.provider_name,
                model=response.model_name or provider.model_name,
                retry_count=response.retries + index,
                token_usage={
                    "total_tokens": response.tokens_used,
                    "prompt_tokens": response.prompt_tokens,
                    "completion_tokens": response.completion_tokens,
                },
                errors=[response.error_message] if response.error_message else [],
                metadata={
                    "role": self._role,
                    "fallback_attempt": index > 0,
                    "error_type": response.error_type,
                },
            )
            if response.text:
                if index > 0:
                    await emit_current_trace_event(
                        event="fallback_provider_used",
                        stage="llm_provider",
                        status="completed",
                        message="Fallback provider produced the response.",
                        provider=response.provider or provider.provider_name,
                        model=response.model_name or provider.model_name,
                        retry_count=index,
                        metadata={"role": self._role},
                    )
                return response
            last_response = response
        return last_response or LLMResponse(
            text="",
            model_name=self.model_name,
            provider=self.provider_name,
            error_type="upstream",
            error_message="No provider returned text",
        )

    async def embeddings(self, input_: list[str]) -> list[list[float]]:
        last_error: Exception | None = None
        for provider in self._providers:
            try:
                return await provider.embeddings(input_)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                continue
        if last_error:
            raise last_error
        return []

    async def health(self) -> dict[str, object]:
        checks = [await provider.health() for provider in self._providers]
        return {
            "status": "ok" if any(check.get("status") == "ok" for check in checks) else "unavailable",
            "providers": checks,
        }


class LLMFactory:
    @staticmethod
    def create(config: ProviderConfig) -> LLMProvider:
        provider = normalize_provider(config.provider)
        compat = provider_compat(provider)
        config = ProviderConfig(
            provider=provider,
            model=config.model,
            api_key=resolve_secret(config.api_key),
            base_url=config.base_url,
            timeout=config.timeout,
            max_retries=config.max_retries,
            retry_base_delay=config.retry_base_delay,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            role=config.role,
            extra_headers=config.extra_headers,
        )
        validation_error = validate_provider_config(config)
        if validation_error:
            return UnsupportedModelClient(provider=config.provider, model=config.model)

        if compat == "ollama":
            return OllamaProvider(config=config)
        if compat == "openai" and provider not in {"voyage", "voyageai"}:
            return OpenAICompatibleProvider(config=config)
        if provider == "anthropic":
            return AnthropicProvider(config=config)
        if provider == "gemini":
            return GeminiProvider(config=config)
        if provider in {"voyage", "voyageai"}:
            return VoyageProvider(config=config)
        return UnsupportedModelClient(provider=provider, model=config.model)

    @staticmethod
    def create_for_role(
        settings: Settings,
        *,
        role: str,
        model: str | None = None,
        default_timeout: int | float | None = None,
    ) -> LLMProvider:
        configs = list(resolve_role_configs(settings, role, model, default_timeout))
        providers = [LLMFactory.create(config) for config in configs]
        if len(providers) == 1:
            return FallbackLLMProvider(role=role, providers=providers)
        return FallbackLLMProvider(role=role, providers=providers)

    @staticmethod
    async def resolve_from_registry(
        role: str,
        pool: Any,
    ) -> LLMProvider | None:
        if pool is None:
            return None
        from nl2sql_service import provider_store
        from nl2sql_service.key_vault import decrypt_api_key

        model_config = await provider_store.get_default_model_config(pool, role)
        if not model_config:
            return None
        decrypted_key = None
        encrypted_key = model_config.get("api_key_enc")
        if encrypted_key:
            decrypted_key = decrypt_api_key(str(encrypted_key))
        provider_config = ProviderConfig(
            provider=str(model_config["provider_name"]),
            model=str(model_config["model_name"]),
            api_key=decrypted_key,
            base_url=model_config.get("base_url"),
            timeout=float((model_config.get("provider_extra_config") or {}).get("timeout") or 60),
            role=str(model_config.get("role") or role),
            extra_headers={
                "OpenAI-Organization": str(model_config["org_id"])
            } if model_config.get("org_id") and normalize_provider(str(model_config["provider_name"])) == "openai" else {},
        )
        return LLMFactory.create(provider_config)

    @staticmethod
    async def create_for_role_with_registry(
        settings: Settings,
        *,
        role: str,
        pool: Any,
        model: str | None = None,
        default_timeout: int | float | None = None,
    ) -> LLMProvider:
        provider = await LLMFactory.resolve_from_registry(role, pool)
        if provider is not None:
            return FallbackLLMProvider(role=role, providers=[provider])
        return LLMFactory.create_for_role(
            settings,
            role=role,
            model=model,
            default_timeout=default_timeout,
        )

    @staticmethod
    def create_embedding_provider(settings: Settings) -> LLMProvider:
        config = ProviderConfig(
            provider=normalize_provider(settings.embedding_provider),
            model=settings.embedding_model,
            api_key=settings.embedding_api_key,
            base_url=settings.embedding_base_url or (settings.llm_base_url if settings.embedding_provider.lower() == "ollama" else None),
            timeout=settings.embed_timeout,
            max_retries=settings.embed_max_retries,
            retry_base_delay=settings.embed_retry_base_delay,
            role="embedding",
        )
        return LLMFactory.create(config)


def get_model_client(
    settings: Settings,
    model: str | None,
    default_timeout: int,
    role: str = "default",
) -> LLMProvider:
    return LLMFactory.create_for_role(
        settings,
        role=role,
        model=model,
        default_timeout=default_timeout,
    )


def normalize_provider(provider: str | None) -> str:
    return normalize_provider_name(provider)


def resolve_secret(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if value.startswith("file:"):
        with open(value.removeprefix("file:"), encoding="utf-8") as handle:
            return handle.read().strip()
    if value.startswith("env:"):
        return os.getenv(value.removeprefix("env:"), "").strip() or None
    return value


def validate_provider_config(config: ProviderConfig) -> str | None:
    if not config.provider:
        return "provider is required"
    if not config.model:
        return "model is required"
    if provider_requires_key(config.provider) and not config.api_key:
        return f"{config.provider} requires an API key"
    return None


def resolve_role_configs(
    settings: Settings,
    role: str,
    model: str | None,
    default_timeout: int | float | None,
) -> Iterable[ProviderConfig]:
    provider = getattr(settings, f"{role}_model_provider", None) or settings.llm_provider
    if role == "answer" and not settings.answer_model_provider and not settings.answer_model:
        provider = settings.reasoning_model_provider or provider

    role_model = model or getattr(settings, f"{role}_model", None) or settings.llm_model
    if role == "query_rewrite":
        role_model = model or settings.effective_query_rewrite_model
    api_key = getattr(settings, f"{role}_model_api_key", None) or settings.llm_api_key
    role_base_url = getattr(settings, f"{role}_model_base_url", None)
    base_url = role_base_url if role_base_url else None
    if normalize_provider(provider) == normalize_provider(settings.llm_provider):
        base_url = base_url or settings.llm_base_url
    if role == "answer" and not settings.answer_model_api_key and not settings.answer_model:
        api_key = settings.reasoning_model_api_key or api_key
    if role == "answer" and not settings.answer_model_base_url and not settings.answer_model:
        base_url = settings.reasoning_model_base_url or base_url
    timeout = default_timeout or getattr(settings, f"{role}_timeout", None) or settings.llm_timeout
    max_tokens = getattr(settings, f"{role}_max_tokens", None) or settings.llm_max_tokens
    temperature = getattr(settings, f"{role}_temperature", None) or settings.llm_temperature

    yield ProviderConfig(
        provider=provider,
        model=role_model,
        api_key=api_key,
        base_url=base_url,
        timeout=timeout,
        max_retries=settings.llm_max_retries,
        retry_base_delay=settings.llm_retry_base_delay,
        temperature=temperature,
        max_tokens=max_tokens,
        role=role,
    )

    fallback_provider = (
        getattr(settings, f"{role}_fallback_provider", None)
        or settings.llm_fallback_provider
    )
    if not fallback_provider:
        return
    fallback_model = (
        getattr(settings, f"{role}_fallback_model", None)
        or settings.llm_fallback_model
        or role_model
    )
    fallback_api_key = (
        getattr(settings, f"{role}_fallback_api_key", None)
        or settings.llm_fallback_api_key
        or settings.llm_api_key
    )
    fallback_base_url = (
        getattr(settings, f"{role}_fallback_base_url", None)
        or settings.llm_fallback_base_url
        or settings.llm_base_url
    )
    yield ProviderConfig(
        provider=fallback_provider,
        model=fallback_model,
        api_key=fallback_api_key,
        base_url=fallback_base_url,
        timeout=timeout,
        max_retries=settings.llm_max_retries,
        retry_base_delay=settings.llm_retry_base_delay,
        temperature=temperature,
        max_tokens=max_tokens,
        role=role,
    )
