from __future__ import annotations

from typing import Any

try:
    from prometheus_client import Counter, Histogram, generate_latest
except Exception:  # noqa: BLE001
    Counter = Histogram = None
    generate_latest = None

REQUEST_COUNT = (
    Counter(
        "nl2sql_requests_total",
        "NL2SQL requests by endpoint and status.",
        ("endpoint", "status"),
    )
    if Counter
    else None
)
REQUEST_LATENCY = (
    Histogram(
        "nl2sql_request_latency_ms",
        "NL2SQL request latency in milliseconds.",
        ("endpoint",),
        buckets=(25, 50, 100, 250, 500, 1000, 2500, 5000, 15000, 30000, 60000),
    )
    if Histogram
    else None
)
STAGE_LATENCY = (
    Histogram(
        "nl2sql_stage_latency_ms",
        "Stage latency in milliseconds.",
        ("stage", "status"),
        buckets=(5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 15000, 30000),
    )
    if Histogram
    else None
)
PROVIDER_CALLS = (
    Counter(
        "nl2sql_provider_calls_total",
        "Provider calls by role, provider, model, and outcome.",
        ("role", "provider", "model", "outcome"),
    )
    if Counter
    else None
)
RETRIEVAL_COUNT = (
    Counter(
        "nl2sql_retrieval_events_total",
        "Retrieval executions by outcome.",
        ("outcome",),
    )
    if Counter
    else None
)


def observe_request(endpoint: str, status: str, latency_ms: int) -> None:
    if REQUEST_COUNT:
        REQUEST_COUNT.labels(endpoint=endpoint, status=status).inc()
    if REQUEST_LATENCY:
        REQUEST_LATENCY.labels(endpoint=endpoint).observe(max(latency_ms, 0))


def observe_stage(stage: str, status: str, duration_ms: int | None) -> None:
    if STAGE_LATENCY and duration_ms is not None:
        STAGE_LATENCY.labels(stage=stage, status=status).observe(max(duration_ms, 0))


def observe_provider(role: str, provider: str, model: str, outcome: str) -> None:
    if PROVIDER_CALLS:
        PROVIDER_CALLS.labels(
            role=role or "default",
            provider=provider or "unknown",
            model=model or "unknown",
            outcome=outcome,
        ).inc()


def observe_retrieval(outcome: str) -> None:
    if RETRIEVAL_COUNT:
        RETRIEVAL_COUNT.labels(outcome=outcome).inc()


def render_metrics() -> bytes:
    if generate_latest is None:
        return b"# Prometheus client not installed\n"
    return generate_latest()


def snapshot_metrics_available() -> dict[str, Any]:
    return {"prometheus_enabled": generate_latest is not None}
