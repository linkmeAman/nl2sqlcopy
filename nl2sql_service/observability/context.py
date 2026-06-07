from __future__ import annotations

import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass, replace
from typing import Any


def _new_identifier() -> str:
    return uuid.uuid4().hex


@dataclass(slots=True)
class ObservabilityContext:
    request_id: str = "-"
    trace_id: str = ""
    correlation_id: str = ""
    session_id: str = ""
    workflow_id: str = ""
    endpoint: str = ""
    method: str = ""
    service: str = "nl2sql-api"


_context_var: ContextVar[ObservabilityContext] = ContextVar(
    "nl2sql_observability_context",
    default=ObservabilityContext(),
)
_trace_recorder_var: ContextVar[Any | None] = ContextVar(
    "nl2sql_trace_recorder",
    default=None,
)


def build_request_context(
    *,
    headers: dict[str, str] | None = None,
    endpoint: str = "",
    method: str = "",
    request_id: str | None = None,
) -> ObservabilityContext:
    header_map = {key.lower(): value for key, value in (headers or {}).items()}
    resolved_request_id = (
        request_id
        or header_map.get("x-request-id")
        or header_map.get("x-correlation-id")
        or _new_identifier()
    )
    trace_id = header_map.get("x-trace-id") or _new_identifier()
    correlation_id = header_map.get("x-correlation-id") or resolved_request_id
    session_id = header_map.get("x-session-id") or correlation_id
    workflow_id = header_map.get("x-workflow-id") or resolved_request_id
    return ObservabilityContext(
        request_id=resolved_request_id,
        trace_id=trace_id,
        correlation_id=correlation_id,
        session_id=session_id,
        workflow_id=workflow_id,
        endpoint=endpoint,
        method=method,
    )


def set_trace_context(context: ObservabilityContext) -> Token[ObservabilityContext]:
    return _context_var.set(context)


def get_observability_context() -> ObservabilityContext:
    return _context_var.get()


def bind_context(**updates: Any) -> ObservabilityContext:
    current = get_observability_context()
    updated = replace(current, **updates)
    _context_var.set(updated)
    return updated


def clear_context(token: Token[ObservabilityContext] | None = None) -> None:
    if token is not None:
        _context_var.reset(token)
        return
    _context_var.set(ObservabilityContext())


def set_request_scope(*, endpoint: str, method: str) -> ObservabilityContext:
    return bind_context(endpoint=endpoint, method=method)


def set_request_id(request_id: str) -> ObservabilityContext:
    workflow_id = get_observability_context().workflow_id or request_id
    return bind_context(request_id=request_id or "-", workflow_id=workflow_id)


def set_current_trace_recorder(recorder: Any | None) -> Token[Any | None]:
    return _trace_recorder_var.set(recorder)


def get_current_trace_recorder() -> Any | None:
    return _trace_recorder_var.get()


async def emit_current_trace_event(**kwargs: Any) -> None:
    recorder = get_current_trace_recorder()
    if recorder is None:
        return
    await recorder.emit(**kwargs)
