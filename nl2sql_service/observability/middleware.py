from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

from fastapi import Request, Response

from nl2sql_service.observability.context import (
    build_request_context,
    clear_context,
    get_observability_context,
    set_request_scope,
    set_trace_context,
)
from nl2sql_service.observability.metrics import observe_request
from nl2sql_service.observability.tracing import start_span


def install_request_middleware(app: Any) -> None:
    @app.middleware("http")
    async def request_observability_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        started = time.monotonic()
        context_token = set_trace_context(
            build_request_context(
                headers=dict(request.headers),
                endpoint=request.url.path,
                method=request.method,
            )
        )
        set_request_scope(endpoint=request.url.path, method=request.method)
        with start_span(
            f"{request.method} {request.url.path}",
            attributes={
                "http.method": request.method,
                "http.route": request.url.path,
            },
        ):
            try:
                response = await call_next(request)
            except Exception:
                observe_request(request.url.path, "exception", int((time.monotonic() - started) * 1000))
                clear_context(context_token)
                raise
            context = get_observability_context()
            request.state.request_id = context.request_id
            request.state.trace_id = context.trace_id
            request.state.workflow_id = context.workflow_id
            response.headers["x-request-id"] = context.request_id
            response.headers["x-trace-id"] = context.trace_id
            response.headers["x-correlation-id"] = context.correlation_id
            response.headers["x-session-id"] = context.session_id
            response.headers["x-workflow-id"] = context.workflow_id
            observe_request(
                request.url.path,
                str(response.status_code),
                int((time.monotonic() - started) * 1000),
            )
            clear_context(context_token)
            return response
