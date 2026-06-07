from __future__ import annotations

from typing import Any


PROVIDER_DEFAULTS: dict[str, dict[str, Any]] = {
    "ollama": {
        "base_url": None,
        "requires_key": False,
        "compat": "ollama",
        "probe_model": None,
    },
    "openai": {
        "base_url": None,
        "requires_key": True,
        "compat": "openai",
        "probe_model": None,
    },
    "anthropic": {
        "base_url": None,
        "requires_key": True,
        "compat": "anthropic",
        "probe_model": None,
    },
    "gemini": {
        "base_url": None,
        "requires_key": True,
        "compat": "gemini",
        "probe_model": None,
    },
    "groq": {
        "base_url": None,
        "requires_key": True,
        "compat": "openai",
        "probe_model": None,
    },
    "openrouter": {
        "base_url": None,
        "requires_key": True,
        "compat": "openai",
        "probe_model": None,
    },
    "togetherai": {
        "base_url": None,
        "requires_key": True,
        "compat": "openai",
        "probe_model": None,
    },
    "voyageai": {
        "base_url": None,
        "requires_key": True,
        "compat": "openai",
        "probe_model": None,
    },
}

_PROVIDER_ALIASES = {
    "claude": "anthropic",
    "google": "gemini",
    "together": "togetherai",
    "voyage": "voyageai",
}


def normalize_provider_name(provider: str | None, *, default: str = "ollama") -> str:
    cleaned = (provider or default).strip().lower().replace("-", "_")
    return _PROVIDER_ALIASES.get(cleaned, cleaned)


def provider_defaults(provider: str | None) -> dict[str, Any]:
    return PROVIDER_DEFAULTS.get(normalize_provider_name(provider), {})


def provider_compat(provider: str | None) -> str:
    defaults = provider_defaults(provider)
    return str(defaults.get("compat") or normalize_provider_name(provider))


def provider_requires_key(provider: str | None) -> bool:
    defaults = provider_defaults(provider)
    return bool(defaults.get("requires_key"))
