from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


class FailureType(str, Enum):
    RETRIEVAL_FAILURE = "RETRIEVAL_FAILURE"
    CHUNKING_FAILURE = "CHUNKING_FAILURE"
    RERANKING_FAILURE = "RERANKING_FAILURE"
    SCHEMA_RETRIEVAL_FAILURE = "SCHEMA_RETRIEVAL_FAILURE"
    PLANNING_FAILURE = "PLANNING_FAILURE"
    SQL_GENERATION_FAILURE = "SQL_GENERATION_FAILURE"
    SQL_VALIDATION_FAILURE = "SQL_VALIDATION_FAILURE"
    EXECUTION_FAILURE = "EXECUTION_FAILURE"
    ANSWER_GENERATION_FAILURE = "ANSWER_GENERATION_FAILURE"
    CACHE_FAILURE = "CACHE_FAILURE"
    PROVIDER_FAILURE = "PROVIDER_FAILURE"


class EvaluationEndpoint(str, Enum):
    ASK = "ask"
    ASK_STREAM = "ask-stream"


class BenchmarkExpectedCriteria(BaseModel):
    status: Literal["ok", "clarification_needed", "rejected"] = "ok"
    answer_contains: list[str] = Field(default_factory=list)
    answer_not_contains: list[str] = Field(default_factory=list)
    react_actions_contains: list[str] = Field(default_factory=list)
    react_actions_not_contains: list[str] = Field(default_factory=list)
    warning_codes_contains: list[str] = Field(default_factory=list)
    warning_codes_not_contains: list[str] = Field(default_factory=list)


class BenchmarkSqlCharacteristics(BaseModel):
    must_include: list[str] = Field(default_factory=list)
    must_exclude: list[str] = Field(default_factory=list)
    must_use_tables: list[str] = Field(default_factory=list)
    forbid_select_star: bool = False
    requires_join: bool | None = None
    min_join_count: int | None = None
    max_join_count: int | None = None
    requires_group_by: bool | None = None
    requires_order_by: bool | None = None
    requires_limit: bool | None = None
    limit_value: int | None = None
    regex: list[str] = Field(default_factory=list)


class FailureHints(BaseModel):
    likely_failure_types: list[FailureType] = Field(default_factory=list)
    signals: list[str] = Field(default_factory=list)
    investigation: str | None = None


class BenchmarkCase(BaseModel):
    id: str
    query: str
    expected_criteria: BenchmarkExpectedCriteria = Field(default_factory=BenchmarkExpectedCriteria)
    expected_tables: list[str] = Field(default_factory=list)
    expected_keywords: list[str] = Field(default_factory=list)
    expected_sql_characteristics: BenchmarkSqlCharacteristics = Field(
        default_factory=BenchmarkSqlCharacteristics
    )
    failure_classification_hints: FailureHints = Field(default_factory=FailureHints)
    endpoint: EvaluationEndpoint | None = None
    top_k: int | None = None
    top_k_values: list[int] = Field(default_factory=list)
    repeat: int = 1
    notes: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class BenchmarkSuite(BaseModel):
    suite_id: str
    level: int
    title: str
    description: str | None = None
    cases: list[BenchmarkCase]
    metadata: dict[str, Any] = Field(default_factory=dict)


@dataclass(slots=True)
class RunSpec:
    suite_id: str
    suite_level: int
    suite_title: str
    case: BenchmarkCase
    endpoint: EvaluationEndpoint
    top_k: int
    repeat_index: int
    variant_index: int
    request_id: str
    trace_id: str


@dataclass(slots=True)
class RunAssessment:
    passed: bool
    reasons: list[str] = field(default_factory=list)
    failure_type: FailureType | None = None
    root_cause: str | None = None
    recommended_investigation: str | None = None
    trace_summary: dict[str, Any] = field(default_factory=dict)
    retrieved_context: list[Any] = field(default_factory=list)
    retrieved_tables: list[str] = field(default_factory=list)
    react_actions: list[dict[str, Any]] = field(default_factory=list)
    provider: str | None = None
    model: str | None = None
    cache_hit: bool = False
    cache_source: str | None = None
    failure_signals: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RunResult:
    spec: RunSpec
    timestamp: str
    latency_ms: int
    http_status_code: int | None = None
    response_body: dict[str, Any] | None = None
    response_status: str | None = None
    actual_answer: str | None = None
    generated_sql: str | None = None
    warnings: list[dict[str, Any]] = field(default_factory=list)
    row_count: int | None = None
    columns: list[str] = field(default_factory=list)
    tables_used: list[str] = field(default_factory=list)
    matched_groups: list[str] = field(default_factory=list)
    attempt_count: int | None = None
    cache_hit: bool = False
    cache_source: str | None = None
    trace_id: str | None = None
    provider: str | None = None
    model: str | None = None
    react_actions: list[dict[str, Any]] = field(default_factory=list)
    retrieved_context: list[Any] = field(default_factory=list)
    retrieved_tables: list[str] = field(default_factory=list)
    stream_events: list[dict[str, Any]] = field(default_factory=list)
    trace_events: list[dict[str, Any]] = field(default_factory=list)
    trace_summary: dict[str, Any] = field(default_factory=dict)
    failure_log_entry: dict[str, Any] | None = None
    assessment: RunAssessment | None = None
    request_error: str | None = None

    def outcome_key(self) -> str:
        return f"{self.spec.suite_id}:{self.spec.case.id}:{self.spec.variant_index}:{self.spec.repeat_index}"


@dataclass(slots=True)
class EvaluationConfig:
    service_url: str
    benchmarks_dir: Path
    output_dir: Path
    endpoint: EvaluationEndpoint
    top_k: int
    parallel: int = 1
    timeout_seconds: float = 120.0
    trace_retry_limit: int = 6
    trace_retry_delay_seconds: float = 0.25
    require_ready: bool = False
    sync_db: bool = False
    repeat: int = 1
    bearer_token: str | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)
    fail_limit: int = 500


@dataclass(slots=True)
class EvaluationSummary:
    run_at: str
    service_url: str
    endpoint: str
    total_tests: int
    passed: int
    failed: int
    pass_rate: float
    avg_latency_ms: float
    p95_latency_ms: float
    cache_hit_rate: float
    retrieval_failure_rate: float
    sql_failure_rate: float
    provider_failure_rate: float
    failure_breakdown: dict[str, int] = field(default_factory=dict)
    difficulty_breakdown: dict[str, dict[str, int]] = field(default_factory=dict)
    suite_breakdown: dict[str, dict[str, int]] = field(default_factory=dict)
    output_files: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_at": self.run_at,
            "service_url": self.service_url,
            "endpoint": self.endpoint,
            "total_tests": self.total_tests,
            "passed": self.passed,
            "failed": self.failed,
            "pass_rate": self.pass_rate,
            "avg_latency_ms": self.avg_latency_ms,
            "p95_latency_ms": self.p95_latency_ms,
            "cache_hit_rate": self.cache_hit_rate,
            "retrieval_failure_rate": self.retrieval_failure_rate,
            "sql_failure_rate": self.sql_failure_rate,
            "provider_failure_rate": self.provider_failure_rate,
            "failure_breakdown": self.failure_breakdown,
            "difficulty_breakdown": self.difficulty_breakdown,
            "suite_breakdown": self.suite_breakdown,
            "output_files": self.output_files,
        }
