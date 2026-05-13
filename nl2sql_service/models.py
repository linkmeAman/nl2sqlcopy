from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field


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


class ReActAction(str, Enum):
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


class ReactTrace(BaseModel):
    steps: list[ReActStep]
    total_iterations: int
    final_action: ReActAction


class GenerateSqlSuccess(BaseModel):
    status: Literal["ok"] = "ok"
    sql: str
    warnings: list[SqlWarning] = []
    tables_used: list[str]
    matched_groups: list[str]
    attempt_count: int
    cache_hit: bool = False
    react_trace: ReactTrace | None = None


class GenerateSqlRejected(BaseModel):
    status: Literal["rejected"] = "rejected"
    sql: None = None
    warnings: list[SqlWarning]
    attempt_count: int
    cache_hit: bool = False
    react_trace: ReactTrace | None = None


class GenerateSqlClarification(BaseModel):
    status: Literal["clarification_needed"] = "clarification_needed"
    question: str
    suggestions: list[str]
    original_query: str
    failure_reason: str
    cache_hit: bool = False
    react_trace: ReactTrace | None = None


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
    answer: str
    sql: str
    warnings: list[SqlWarning] = []
    row_count: int
    columns: list[str]
    tables_used: list[str]
    matched_groups: list[str]
    attempt_count: int
    react_trace: ReactTrace | None = None


class AskRejected(BaseModel):
    status: Literal["rejected"] = "rejected"
    answer: None = None
    sql: str | None = None
    warnings: list[SqlWarning]
    attempt_count: int
    react_trace: ReactTrace | None = None


AskResponse = Annotated[
    AskSuccess | AskRejected | GenerateSqlClarification,
    Field(discriminator="status"),
]


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
    source: str


class IngestGroupsResponse(IngestResponse):
    enrichment_summary: EnrichmentSummary | None = None
    failed_groups: list[GroupIngestFailure] = Field(default_factory=list)
    failure_count: int = 0


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
