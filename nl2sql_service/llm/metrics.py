from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import asdict, dataclass
from threading import Lock

from nl2sql_service.llm.interfaces import LLMResponse

logger = logging.getLogger(__name__)


@dataclass
class UsageMetric:
    requests: int = 0
    failures: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    total_latency_ms: int = 0
    estimated_cost_usd: float = 0.0
    retries: int = 0


_metrics: dict[tuple[str, str, str], UsageMetric] = defaultdict(UsageMetric)
_lock = Lock()


def estimate_cost_usd(
    provider: str,
    model: str,
    prompt_tokens: int | None,
    completion_tokens: int | None,
) -> float | None:
    if prompt_tokens is None and completion_tokens is None:
        return None

    # Conservative public-list-price defaults per 1M tokens. Unknown models
    # intentionally return 0 cost instead of pretending to know provider billing.
    pricing: dict[tuple[str, str], tuple[float, float]] = {
        ("openai", "gpt-4.1-mini"): (0.40, 1.60),
        ("openai", "gpt-4.1-nano"): (0.10, 0.40),
        ("openai", "gpt-4o-mini"): (0.15, 0.60),
        ("anthropic", "claude-3-5-haiku-latest"): (0.80, 4.00),
        ("groq", "llama-3.3-70b-versatile"): (0.59, 0.79),
    }
    provider_key = provider.lower()
    model_key = model.lower()
    input_price, output_price = pricing.get((provider_key, model_key), (0.0, 0.0))
    return ((prompt_tokens or 0) * input_price + (completion_tokens or 0) * output_price) / 1_000_000


def record_llm_response(role: str, response: LLMResponse) -> None:
    provider = response.provider or "unknown"
    model = response.model_name or "unknown"
    key = (role, provider, model)
    with _lock:
        metric = _metrics[key]
        metric.requests += 1
        if response.error_type:
            metric.failures += 1
        metric.prompt_tokens += response.prompt_tokens or 0
        metric.completion_tokens += response.completion_tokens or 0
        metric.total_tokens += response.tokens_used or 0
        metric.total_latency_ms += response.latency_ms
        metric.estimated_cost_usd += response.estimated_cost_usd or 0.0
        metric.retries += response.retries

    logger.info(
        "llm_usage role=%s provider=%s model=%s latency_ms=%s tokens=%s retries=%s error_type=%s cost_usd=%.8f",
        role,
        provider,
        model,
        response.latency_ms,
        response.tokens_used,
        response.retries,
        response.error_type,
        response.estimated_cost_usd or 0.0,
    )


def snapshot() -> list[dict[str, object]]:
    with _lock:
        return [
            {
                "role": role,
                "provider": provider,
                "model": model,
                **asdict(metric),
                "avg_latency_ms": (
                    metric.total_latency_ms / metric.requests if metric.requests else 0
                ),
            }
            for (role, provider, model), metric in _metrics.items()
        ]


def reset() -> None:
    with _lock:
        _metrics.clear()
