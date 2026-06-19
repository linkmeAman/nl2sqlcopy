from __future__ import annotations

from datetime import datetime
from enum import Enum
from uuid import UUID
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Ingest request models
# ---------------------------------------------------------------------------


class SchemaTable(BaseModel):
    """Mirrors the shape produced by nl2sql_build_corpus.py."""

    database: str
    object_name: str
    object_type: str = "table"
    full_object_name: str
    text: str
    chunk_index: int = 1
    total_chunks: int = 1
    column_count: int | None = None
    source_kind: str = "schema_export"


class IngestTextRequest(BaseModel):
    type: Literal["text"]
    source: str
    text: str


class IngestSchemaRequest(BaseModel):
    type: Literal["schema"]
    source: str
    tables: list[SchemaTable]


IngestRequest = Annotated[
    Union[IngestTextRequest, IngestSchemaRequest],
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# Query request / response models
# ---------------------------------------------------------------------------


class QueryRequest(BaseModel):
    query: str
    top_k: int | None = None


class QueryResult(BaseModel):
    content: str
    similarity: float
    metadata: dict[str, Any]


class QueryResponse(BaseModel):
    results: list[QueryResult]


# ---------------------------------------------------------------------------
# Runtime model routing
# ---------------------------------------------------------------------------


class ModelRoutingPatchRequest(BaseModel):
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_api_key: str | None = None
    llm_base_url: str | None = None
    llm_fallback_provider: str | None = None
    llm_fallback_model: str | None = None
    llm_fallback_api_key: str | None = None
    llm_fallback_base_url: str | None = None

    sql_model_provider: str | None = None
    sql_model: str | None = None
    sql_model_api_key: str | None = None
    sql_model_base_url: str | None = None
    sql_fallback_provider: str | None = None
    sql_fallback_model: str | None = None
    sql_fallback_api_key: str | None = None
    sql_fallback_base_url: str | None = None

    reasoning_model_provider: str | None = None
    reasoning_model: str | None = None
    reasoning_model_api_key: str | None = None
    reasoning_model_base_url: str | None = None
    reasoning_fallback_provider: str | None = None
    reasoning_fallback_model: str | None = None
    reasoning_fallback_api_key: str | None = None
    reasoning_fallback_base_url: str | None = None

    query_rewrite_model_provider: str | None = None
    query_rewrite_model: str | None = None
    query_rewrite_model_api_key: str | None = None
    query_rewrite_model_base_url: str | None = None
    query_rewrite_fallback_provider: str | None = None
    query_rewrite_fallback_model: str | None = None
    query_rewrite_fallback_api_key: str | None = None
    query_rewrite_fallback_base_url: str | None = None

    answer_model_provider: str | None = None
    answer_model: str | None = None
    answer_model_api_key: str | None = None
    answer_model_base_url: str | None = None
    answer_fallback_provider: str | None = None
    answer_fallback_model: str | None = None
    answer_fallback_api_key: str | None = None
    answer_fallback_base_url: str | None = None

    embedding_provider: str | None = None
    embedding_model: str | None = None
    embedding_api_key: str | None = None
    embedding_base_url: str | None = None
    embedding_api_url: str | None = None

    startup_enforcement_mode: str | None = None

    model_config = ConfigDict(extra="ignore")


class AskModelPatchRequest(BaseModel):
    provider: str | None = None
    model: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    fallback_provider: str | None = None
    fallback_model: str | None = None
    fallback_api_key: str | None = None
    fallback_base_url: str | None = None

    model_config = ConfigDict(extra="ignore")


class ModelRoutingSnapshot(BaseModel):
    llm: dict[str, Any]
    sql: dict[str, Any]
    reasoning: dict[str, Any]
    query_rewrite: dict[str, Any]
    answer: dict[str, Any]
    embedding: dict[str, Any]
    startup_enforcement_mode: str
    provider_readiness: dict[str, Any]


class AskModelSnapshot(BaseModel):
    provider: str | None
    model: str | None
    base_url: str | None
    api_key_configured: bool
    fallback_provider: str | None
    fallback_model: str | None
    fallback_base_url: str | None
    fallback_api_key_configured: bool


# ---------------------------------------------------------------------------
# Provider management
# ---------------------------------------------------------------------------


class CreateProviderRequest(BaseModel):
    provider_name: str
    display_name: str
    base_url: str | None = None
    org_id: str | None = None
    is_local: bool = False
    extra_config: dict[str, Any] = Field(default_factory=dict)


class UpdateProviderRequest(BaseModel):
    display_name: str | None = None
    base_url: str | None = None
    org_id: str | None = None
    is_active: bool | None = None
    is_local: bool | None = None
    extra_config: dict[str, Any] | None = None


class ProviderConfig(BaseModel):
    id: UUID
    provider_name: str
    display_name: str
    base_url: str | None = None
    org_id: str | None = None
    is_active: bool
    is_local: bool
    extra_config: dict[str, Any] = Field(default_factory=dict)
    key_count: int
    model_count: int
    created_at: datetime | None = None
    updated_at: datetime | None = None


class AddApiKeyRequest(BaseModel):
    key_label: str
    api_key: str


class ApiKeyRecord(BaseModel):
    id: UUID
    provider_id: UUID
    key_label: str
    key_prefix: str
    is_active: bool
    created_at: datetime


class RegisterModelRequest(BaseModel):
    provider_id: UUID
    api_key_id: UUID | None = None
    model_name: str
    display_name: str | None = None
    role: str = "general"
    context_window: int | None = None
    supports_tools: bool = False
    supports_stream: bool = True
    is_default: bool = False
    extra_config: dict[str, Any] = Field(default_factory=dict)


class UpdateModelRequest(BaseModel):
    display_name: str | None = None
    api_key_id: UUID | None = None
    context_window: int | None = None
    supports_tools: bool | None = None
    supports_stream: bool | None = None
    is_default: bool | None = None
    is_active: bool | None = None
    extra_config: dict[str, Any] | None = None


class ModelRecord(BaseModel):
    id: UUID
    provider_id: UUID
    provider_name: str
    model_name: str
    display_name: str | None = None
    role: str
    is_default: bool
    is_active: bool
    supports_tools: bool
    supports_stream: bool
    context_window: int | None = None
    api_key_id: UUID | None = None
    extra_config: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ActiveModelPatchRequest(BaseModel):
    model_id: UUID


class ProviderTestResult(BaseModel):
    status: Literal["ok", "unreachable", "auth_failed", "timeout"]
    latency_ms: int | None = None
    available_models: list[str] = Field(default_factory=list)
    error_message: str | None = None


# ---------------------------------------------------------------------------
# Schema-group models
# ---------------------------------------------------------------------------


class IngestGroupsRequest(BaseModel):
    group_names: list[str] | None = None


class GroupEmbeddingStatusItem(BaseModel):
    group_name: str
    entity_id: str
    file_hash: str
    stored_version: str | None
    is_current: bool
    last_embedded_at: str | None


class GroupEmbeddingStatusResponse(BaseModel):
    groups: list[GroupEmbeddingStatusItem]
    current_count: int
    stale_count: int
    never_embedded_count: int


class EnrichmentSummary(BaseModel):
    groups_with_columns: int
    groups_without_columns: int
    groups_with_aliases: int
    groups_with_examples: int


class GroupIngestFailure(BaseModel):
    group_name: str
    reason: str


class IngestKnowledgeRequest(BaseModel):
    include_column_catalog: bool = True
    include_sql_examples: bool = True
    include_relations: bool = True
    include_graph: bool = True
    include_view_registry: bool = True
    include_onboarding_rules: bool = True
    column_limit: int | None = None
    sql_example_limit: int | None = 200
    relation_limit: int | None = None
    graph_limit: int | None = None
    view_registry_limit: int | None = None


class GroupQueryResponse(BaseModel):
    matched_groups: list[str]
    """Ordered list of group source names returned by the vector search."""

    tables_in_scope: list[str]
    """Deduplicated, ordered union of tables + related_tables from all matched groups."""

    context: str
    """Formatted context block ready to paste into an LLM prompt."""

    results: list[QueryResult]
    """Raw per-chunk results for transparency/debugging."""


# ---------------------------------------------------------------------------
# SQL generation models
# ---------------------------------------------------------------------------


class GenerateSqlRequest(BaseModel):
    query: str
    top_k: int | None = None
    request_id: str | None = None


class WarningCode(str, Enum):
    REQUEST_TIMEOUT = "REQUEST_TIMEOUT"
    OLLAMA_TIMEOUT = "OLLAMA_TIMEOUT"
    OLLAMA_UPSTREAM = "OLLAMA_UPSTREAM"
    OLLAMA_MALFORMED = "OLLAMA_MALFORMED"
    SQL_EMPTY = "SQL_EMPTY"
    SQL_MULTI_STATEMENT = "SQL_MULTI_STATEMENT"
    SQL_DESTRUCTIVE = "SQL_DESTRUCTIVE"
    SQL_NOT_SELECT = "SQL_NOT_SELECT"
    TABLE_OUT_OF_SCOPE = "TABLE_OUT_OF_SCOPE"
    COLUMN_OUT_OF_SCOPE = "COLUMN_OUT_OF_SCOPE"
    MYSQL_EXPLAIN_ERROR = "MYSQL_EXPLAIN_ERROR"
    MYSQL_EXPLAIN_UNAVAILABLE = "MYSQL_EXPLAIN_UNAVAILABLE"
    MYSQL_QUERY_ERROR = "MYSQL_QUERY_ERROR"
    ANSWER_TIMEOUT = "ANSWER_TIMEOUT"
    ANSWER_UPSTREAM = "ANSWER_UPSTREAM"
    ANSWER_MALFORMED = "ANSWER_MALFORMED"
    ANSWER_HALLUCINATION = "ANSWER_HALLUCINATION"
    REVIEW_FAILED = "REVIEW_FAILED"
    MAX_RETRIES_EXCEEDED = "MAX_RETRIES_EXCEEDED"


class SqlWarning(BaseModel):
    code: WarningCode
    message: str


class CacheSource(str, Enum):
    NONE = "none"
    MEMORY_EXACT = "memory_exact"
    MEMORY_SEMANTIC = "memory_semantic"
    DB_EXACT = "db_exact"
    DB_SEMANTIC = "db_semantic"


class ReActAction(str, Enum):
    RETRIEVE_PAST_CORRECTIONS = "RETRIEVE_PAST_CORRECTIONS"
    RETRIEVE_SCHEMA_FOR_TABLES = "RETRIEVE_SCHEMA_FOR_TABLES"
    RETRIEVE_JOIN_PATHS = "RETRIEVE_JOIN_PATHS"
    RETRIEVE_SAMPLE_QUERIES = "RETRIEVE_SAMPLE_QUERIES"
    REQUEST_CLARIFICATION = "REQUEST_CLARIFICATION"
    RETRIEVE_MORE_CONTEXT = "RETRIEVE_MORE_CONTEXT"
    FETCH_SCHEMA = "FETCH_SCHEMA"
    GENERATE_SQL = "GENERATE_SQL"
    VALIDATE_AND_RETURN = "VALIDATE_AND_RETURN"
    ASK_CLARIFICATION = "ASK_CLARIFICATION"
    GIVE_UP = "GIVE_UP"


class ReActStep(BaseModel):
    iteration: int
    thought: str
    action: ReActAction
    action_input: str
    observation: str
    duration_ms: int | None = None


class ReactTrace(BaseModel):
    steps: list[ReActStep]
    total_iterations: int
    final_action: ReActAction


class HumanReviewPrompt(BaseModel):
    question: str
    accept_label: str = "Looks correct"
    reject_label: str = "Needs correction"
    needs_review: bool = False
    reason: str | None = None
    teach_payload: dict[str, Any]


class GenerateSqlSuccess(BaseModel):
    status: Literal["ok"] = "ok"
    request_id: str | None = None
    trace_id: str | None = None
    workflow_id: str | None = None
    sql: str
    warnings: list[SqlWarning] = []
    tables_used: list[str]
    matched_groups: list[str]
    attempt_count: int
    cache_hit: bool = False
    cache_source: CacheSource = CacheSource.NONE
    react_trace: ReactTrace | None = None
    stage_latencies_ms: dict[str, int] | None = None
    review_prompt: HumanReviewPrompt | None = None


class GenerateSqlRejected(BaseModel):
    status: Literal["rejected"] = "rejected"
    request_id: str | None = None
    trace_id: str | None = None
    workflow_id: str | None = None
    sql: None = None
    warnings: list[SqlWarning]
    attempt_count: int
    cache_hit: bool = False
    cache_source: CacheSource = CacheSource.NONE
    react_trace: ReactTrace | None = None
    stage_latencies_ms: dict[str, int] | None = None


class GenerateSqlClarification(BaseModel):
    status: Literal["clarification_needed"] = "clarification_needed"
    request_id: str | None = None
    trace_id: str | None = None
    workflow_id: str | None = None
    question: str
    suggestions: list[str]
    original_query: str
    failure_reason: str
    cache_hit: bool = False
    cache_source: CacheSource = CacheSource.NONE
    react_trace: ReactTrace | None = None
    stage_latencies_ms: dict[str, int] | None = None


GenerateSqlResponse = Annotated[
    GenerateSqlSuccess | GenerateSqlRejected | GenerateSqlClarification,
    Field(discriminator="status"),
]


class AskRequest(BaseModel):
    query: str
    top_k: int | None = None
    request_id: str | None = None


class AskSuccess(BaseModel):
    status: Literal["ok"] = "ok"
    request_id: str | None = None
    trace_id: str | None = None
    workflow_id: str | None = None
    answer: str
    sql: str
    warnings: list[SqlWarning] = []
    row_count: int
    columns: list[str]
    tables_used: list[str]
    matched_groups: list[str]
    attempt_count: int
    cache_hit: bool = False
    cache_source: CacheSource = CacheSource.NONE
    react_trace: ReactTrace | None = None
    stage_latencies_ms: dict[str, int] | None = None
    review_prompt: HumanReviewPrompt | None = None


class AskRejected(BaseModel):
    status: Literal["rejected"] = "rejected"
    request_id: str | None = None
    trace_id: str | None = None
    workflow_id: str | None = None
    answer: None = None
    sql: str | None = None
    warnings: list[SqlWarning]
    attempt_count: int
    cache_hit: bool = False
    cache_source: CacheSource = CacheSource.NONE
    react_trace: ReactTrace | None = None
    stage_latencies_ms: dict[str, int] | None = None


AskResponse = Annotated[
    AskSuccess | AskRejected | GenerateSqlClarification,
    Field(discriminator="status"),
]


class TraceEvent(BaseModel):
    request_id: str
    trace_id: str | None = None
    correlation_id: str | None = None
    session_id: str | None = None
    workflow_id: str | None = None
    seq: int
    event: str | None = None
    layer: str
    stage: str
    status: str
    message: str
    span_id: str | None = None
    parent_span_id: str | None = None
    duration_ms: int | None = None
    provider: str | None = None
    model: str | None = None
    retry_count: int = 0
    reasoning_summary: str | None = None
    input_summary: dict[str, Any] = Field(default_factory=dict)
    output_summary: dict[str, Any] = Field(default_factory=dict)
    warning_codes: list[str] = Field(default_factory=list)
    error_source: str | None = None
    token_usage: dict[str, Any] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    started_at: str | None = None
    ended_at: str | None = None
    schema_version: str | None = None
    created_at: str | None = None


class PatternFeedbackRequest(BaseModel):
    pattern_id: int
    helpful: bool


# ---------------------------------------------------------------------------
# Interactive learning models
# ---------------------------------------------------------------------------


class InstructionType(str, Enum):
    TABLE_RELATIONSHIP = "table_relationship"
    BUSINESS_RULE = "business_rule"
    QUERY_METHODOLOGY = "query_methodology"
    TERM_MAPPING = "term_mapping"
    FILTER_RULE = "filter_rule"
    CORRECTION = "correction"


class TeachRequest(BaseModel):
    instruction_type: InstructionType
    content: str
    tables_affected: list[str] = Field(default_factory=list)
    source_query: str | None = None


class LearningStatus(str, Enum):
    SAVED_NEW = "saved_new"
    UPDATED_EXISTING = "updated_existing"
    CONFLICT_DETECTED = "conflict_detected"
    PENDING_CONFIRMATION = "pending_confirmation"
    SIMILAR_FOUND = "similar_found"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class SimilarInstruction(BaseModel):
    id: int
    instruction_type: str
    content: str
    confidence_score: float
    is_verified: bool
    use_count: int


class TeachResponse(BaseModel):
    learning_status: LearningStatus
    message: str
    instruction_id: int | None = None
    similar_instructions: list[SimilarInstruction] = Field(default_factory=list)
    requires_confirmation: bool = False
    confirmation_token: str | None = None


class ConfirmRequest(BaseModel):
    confirmation_token: str
    action: Literal["confirm", "reject", "replace"]


# ---------------------------------------------------------------------------
# Ingest response
# ---------------------------------------------------------------------------


class IngestResponse(BaseModel):
    inserted: int
    updated: int = 0
    skipped: int = 0
    source: str


class IngestGroupsResponse(IngestResponse):
    enrichment_summary: EnrichmentSummary | None = None
    failed_groups: list[GroupIngestFailure] = Field(default_factory=list)
    failure_count: int = 0


class EmbeddedIngestResponse(IngestResponse):
    embedded: int = 0


# ---------------------------------------------------------------------------
# Evaluation / telemetry models
# ---------------------------------------------------------------------------


class BenchmarkCaseCreateRequest(BaseModel):
    query: str
    gold_sql: str | None = None
    expected_status: Literal["ok", "clarification_needed", "rejected"] = "ok"
    slices: list[str] = Field(default_factory=list)
    error_label: str | None = None
    source: str = "manual"
    metadata: dict[str, Any] = Field(default_factory=dict)


class BenchmarkCaseCreateResponse(BaseModel):
    id: int
    query: str
    expected_status: str
