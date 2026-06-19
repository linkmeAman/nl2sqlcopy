from __future__ import annotations

from contextlib import contextmanager, nullcontext
from typing import Any

try:
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.logging import LoggingInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
except Exception:  # noqa: BLE001
    trace = None
    OTLPSpanExporter = None
    FastAPIInstrumentor = None
    LoggingInstrumentor = None
    Resource = None
    TracerProvider = None
    BatchSpanProcessor = None


def setup_tracing(app: Any, settings: Any) -> None:
    if not getattr(settings, "otel_enabled", False):
        return
    if trace is None or TracerProvider is None or Resource is None:
        return
    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": settings.observability_service_name,
                "service.namespace": "nl2sql",
            }
        )
    )
    endpoint = getattr(settings, "otel_exporter_otlp_endpoint", None)
    if endpoint and OTLPSpanExporter and BatchSpanProcessor:
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    if LoggingInstrumentor is not None:
        LoggingInstrumentor().instrument(set_logging_format=False)
    if FastAPIInstrumentor is not None:
        FastAPIInstrumentor.instrument_app(app)


@contextmanager
def start_span(name: str, *, attributes: dict[str, Any] | None = None):
    if trace is None:
        yield None
        return
    tracer = trace.get_tracer("nl2sql.observability")
    with tracer.start_as_current_span(name) as span:
        for key, value in (attributes or {}).items():
            span.set_attribute(key, value)
        yield span


def get_span_ids() -> tuple[str | None, str | None]:
    if trace is None:
        return None, None
    span = trace.get_current_span()
    if span is None:
        return None, None
    span_context = span.get_span_context()
    if not span_context or not getattr(span_context, "is_valid", False):
        return None, None
    trace_id = f"{span_context.trace_id:032x}"
    span_id = f"{span_context.span_id:016x}"
    return trace_id, span_id
