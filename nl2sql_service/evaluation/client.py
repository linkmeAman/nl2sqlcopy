from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from nl2sql_service.evaluation.models import EvaluationEndpoint, RunSpec


def _merge_headers(*parts: dict[str, str] | None) -> dict[str, str]:
    merged: dict[str, str] = {}
    for part in parts:
        if not part:
            continue
        merged.update({str(key): str(value) for key, value in part.items() if value is not None})
    return merged


def _decode_json_payload(payload: bytes | str) -> Any:
    if isinstance(payload, bytes):
        text = payload.decode("utf-8", "replace")
    else:
        text = payload
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        return text


@dataclass(slots=True)
class ClientResponse:
    status_code: int
    body: Any
    latency_ms: int
    raw_text: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)


class Nl2SqlEvaluationClient:
    def __init__(
        self,
        *,
        service_url: str,
        timeout_seconds: float,
        bearer_token: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        headers = {
            "Accept": "application/json",
        }
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"
        headers = _merge_headers(headers, extra_headers)
        timeout = httpx.Timeout(timeout_seconds, connect=timeout_seconds)
        self._client = httpx.AsyncClient(
            base_url=service_url.rstrip("/"),
            timeout=timeout,
            headers=headers,
        )
        self._base_headers = headers

    async def aclose(self) -> None:
        await self._client.aclose()

    def _request_headers(self, spec: RunSpec) -> dict[str, str]:
        return _merge_headers(
            self._base_headers,
            {
                "X-Request-ID": spec.request_id,
                "X-Trace-ID": spec.trace_id,
                "X-Correlation-ID": spec.request_id,
                "X-Session-ID": spec.request_id,
                "X-Workflow-ID": spec.request_id,
            },
        )

    async def health_report(self) -> dict[str, Any]:
        reports: dict[str, Any] = {}
        for path in ("/health", "/health/config", "/health/runtime"):
            response = await self._client.get(path)
            reports[path] = _decode_json_payload(response.content)
        return reports

    async def ensure_ready(self) -> None:
        reports = await self.health_report()
        errors: list[str] = []
        for path, body in reports.items():
            if isinstance(body, dict):
                if body.get("status") != "ok":
                    errors.append(f"{path} status={body.get('status')!r}")
            else:
                errors.append(f"{path} returned non-JSON response")
        if errors:
            raise RuntimeError("Service readiness check failed: " + "; ".join(errors))

    async def sync_benchmark_case(self, payload: dict[str, Any]) -> None:
        response = await self._client.post("/benchmark/cases", json=payload)
        response.raise_for_status()

    async def ask(self, spec: RunSpec) -> ClientResponse:
        payload = {
            "query": spec.case.query,
            "top_k": spec.top_k,
            "request_id": spec.request_id,
        }
        headers = self._request_headers(spec)
        started = time.monotonic()
        if spec.endpoint == EvaluationEndpoint.ASK:
            response = await self._client.post("/ask", json=payload, headers=headers)
            latency_ms = int((time.monotonic() - started) * 1000)
            body = _decode_json_payload(response.content)
            return ClientResponse(
                status_code=response.status_code,
                body=body,
                latency_ms=latency_ms,
                raw_text=response.text,
            )

        async with self._client.stream("POST", "/ask/stream", json=payload, headers=headers) as response:
            content = await response.aread()
        latency_ms = int((time.monotonic() - started) * 1000)
        text = content.decode("utf-8", "replace")
        events: list[dict[str, Any]] = []
        final_response: dict[str, Any] | None = None
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            event = _decode_json_payload(stripped)
            if isinstance(event, dict):
                events.append(event)
                if event.get("event") == "final" and isinstance(event.get("response"), dict):
                    final_response = event["response"]
            else:
                events.append({"event": "raw", "text": stripped})
        body: Any = final_response if final_response is not None else _decode_json_payload(text)
        return ClientResponse(
            status_code=response.status_code,
            body=body,
            latency_ms=latency_ms,
            raw_text=text,
            events=events,
        )

    async def get_trace(self, request_id: str, *, limit: int = 1000) -> dict[str, Any]:
        response = await self._client.get(
            f"/telemetry/trace/{request_id}",
            params={"limit": limit},
        )
        payload = _decode_json_payload(response.content)
        if isinstance(payload, dict):
            return payload
        return {"request_id": request_id, "results": [], "total": 0, "raw": payload}

    async def get_trace_with_retry(
        self,
        request_id: str,
        *,
        limit: int = 1000,
        retries: int = 6,
        delay_seconds: float = 0.25,
    ) -> dict[str, Any]:
        last_payload: dict[str, Any] = {"request_id": request_id, "results": [], "total": 0}
        for attempt in range(max(1, retries)):
            payload = await self.get_trace(request_id, limit=limit)
            last_payload = payload
            if payload.get("results"):
                return payload
            if attempt < retries - 1:
                await asyncio.sleep(delay_seconds * (attempt + 1))
        return last_payload

    async def get_failures(
        self,
        *,
        limit: int = 500,
        endpoint: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if endpoint:
            params["endpoint"] = endpoint
        response = await self._client.get("/failures", params=params)
        payload = _decode_json_payload(response.content)
        return payload if isinstance(payload, list) else []

    async def get_failure_for_request(
        self,
        request_id: str,
        *,
        endpoint: str | None = None,
        limit: int = 500,
        retries: int = 4,
        delay_seconds: float = 0.25,
    ) -> dict[str, Any] | None:
        for attempt in range(max(1, retries)):
            failures = await self.get_failures(limit=limit, endpoint=endpoint)
            for row in failures:
                if str(row.get("request_id")) == request_id:
                    return row
            if attempt < retries - 1:
                await asyncio.sleep(delay_seconds * (attempt + 1))
        return None
