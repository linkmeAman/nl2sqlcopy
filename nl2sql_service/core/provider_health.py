from __future__ import annotations

import time
from typing import Any

import httpx

from nl2sql_service.core.config import settings
from nl2sql_service.models import ProviderTestResult
from nl2sql_service.core.provider_registry import normalize_provider_name, provider_compat


async def _request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
) -> httpx.Response:
    response = await client.request(method, url, headers=headers, json=json_body)
    response.raise_for_status()
    return response


def _base_headers(provider: str, api_key: str, provider_config: dict[str, Any] | None = None) -> dict[str, str]:
    headers: dict[str, str] = {}
    compat = provider_compat(provider)
    if compat == "openai":
        headers["Authorization"] = f"Bearer {api_key}"
    elif compat == "anthropic":
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
    elif compat == "gemini":
        headers["x-goog-api-key"] = api_key
    org_id = str((provider_config or {}).get("org_id") or "").strip()
    if org_id and normalize_provider_name(provider) == "openai":
        headers["OpenAI-Organization"] = org_id
    return headers


async def list_provider_models(
    provider: dict[str, Any],
    api_key: str,
) -> ProviderTestResult:
    provider_name = normalize_provider_name(str(provider.get("provider_name") or provider.get("provider") or ""))
    compat = provider_compat(provider_name)
    base_url = str(provider.get("base_url") or "").rstrip("/")
    if provider_name != "ollama" and not api_key:
        return ProviderTestResult(
            status="auth_failed",
            error_message="An API key is required for this provider.",
        )
    if not base_url:
        return ProviderTestResult(
            status="unreachable",
            error_message="Provider base_url is not configured.",
        )

    headers = _base_headers(provider_name, api_key, provider)
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=settings.health_probe_timeout_clamp) as client:
            if compat == "ollama":
                response = await _request(client, "GET", f"{base_url}/api/tags")
                payload = response.json()
                models = [
                    str(item.get("name"))
                    for item in payload.get("models", [])
                    if str(item.get("name") or "").strip()
                ]
            elif compat == "openai":
                response = await _request(client, "GET", f"{base_url}/models", headers=headers)
                payload = response.json()
                models = [
                    str(item.get("id"))
                    for item in payload.get("data", [])
                    if str(item.get("id") or "").strip()
                ]
            elif compat == "gemini":
                response = await _request(client, "GET", f"{base_url}/models", headers=headers)
                payload = response.json()
                models = [
                    str(item.get("name"))
                    for item in payload.get("models", [])
                    if str(item.get("name") or "").strip()
                ]
            elif compat == "anthropic":
                response = await _request(
                    client,
                    "POST",
                    f"{base_url}/messages",
                    headers=headers,
                    json_body={
                        "model": settings.health_probe_model_anthropic,
                        "max_tokens": 1,
                        "messages": [{"role": "user", "content": "ping"}],
                    },
                )
                payload = response.json()
                model_name = str(payload.get("model") or "")
                models = [model_name] if model_name else []
            else:
                return ProviderTestResult(
                    status="unreachable",
                    error_message=f"Provider {provider_name} is not supported by the health probe.",
                )
    except httpx.TimeoutException:
        return ProviderTestResult(status="timeout", error_message="Provider probe timed out.")
    except httpx.HTTPStatusError as exc:
        status = "auth_failed" if exc.response.status_code in {401, 403} else "unreachable"
        return ProviderTestResult(
            status=status,
            error_message=f"Provider probe returned HTTP {exc.response.status_code}.",
        )
    except Exception as exc:  # noqa: BLE001
        return ProviderTestResult(status="unreachable", error_message=str(exc))

    latency_ms = max(0, int((time.monotonic() - started) * 1000))
    return ProviderTestResult(
        status="ok",
        latency_ms=latency_ms,
        available_models=models,
        error_message=None,
    )


async def test_provider_connection(
    provider: dict[str, Any],
    api_key: str,
    model_name: str,
) -> ProviderTestResult:
    result = await list_provider_models(provider, api_key)
    if result.status != "ok":
        return result
    available_models = list(result.available_models)
    if model_name and model_name not in available_models:
        available_models = [*available_models, model_name]
    return result.model_copy(update={"available_models": available_models})
