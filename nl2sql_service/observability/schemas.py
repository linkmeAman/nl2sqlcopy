from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class ExecutionTraceEvent:
    request_id: str
    trace_id: str
    correlation_id: str
    session_id: str
    workflow_id: str
    layer: str
    stage: str
    status: str
    message: str
    seq: int
    event: str
    span_id: str | None = None
    parent_span_id: str | None = None
    duration_ms: int | None = None
    provider: str | None = None
    model: str | None = None
    retry_count: int = 0
    reasoning_summary: str | None = None
    input_summary: dict[str, Any] = field(default_factory=dict)
    output_summary: dict[str, Any] = field(default_factory=dict)
    warning_codes: list[str] = field(default_factory=list)
    error_source: str | None = None
    errors: list[str] = field(default_factory=list)
    token_usage: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    started_at: str | None = None
    ended_at: str | None = None
    created_at: str = field(default_factory=utcnow_iso)
    schema_version: str = "nl2sql.observability.v1"
    service: str = "nl2sql-api"
    level: str = "INFO"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class FailureAnalysis:
    failure_type: str
    failed_step: str
    root_cause: str
    provider_error: str | None = None
    retry_history: list[dict[str, Any]] = field(default_factory=list)
    fallback_attempts: list[dict[str, Any]] = field(default_factory=list)
    latency_breakdown: dict[str, Any] = field(default_factory=dict)
    recommended_fix: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
