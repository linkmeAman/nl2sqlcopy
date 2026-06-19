from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html import escape
from typing import Any


HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
METHOD_ORDER = {"GET": 0, "POST": 1, "PUT": 2, "PATCH": 3, "DELETE": 4}

MODULE_LABELS = {
    "ops": "Operations",
    "ingestion": "Ingestion",
    "retrieval": "Retrieval",
    "learning": "Learning",
    "generation": "Generation",
}
MODULE_ORDER = {name: index for index, name in enumerate(MODULE_LABELS)}


def route_key(method: str, path: str) -> str:
    return f"{method.upper()} {path}"


@dataclass(frozen=True)
class RouteDoc:
    method: str
    path: str
    module: str
    slug: str
    title: str
    summary: str
    description: str
    request_example: Any | None = None
    response_example: Any | None = None
    error_cases: tuple[str, ...] = ()
    related: tuple[str, ...] = ()
    auth: str = "No authentication is required by this service route."
    notes: tuple[str, ...] = ()
    curl_path: str | None = None

    @property
    def key(self) -> str:
        return route_key(self.method, self.path)


@dataclass(frozen=True)
class HelpEndpoint:
    method: str
    path: str
    module: str
    slug: str
    title: str
    summary: str
    description: str
    request_example: Any | None
    response_example: Any | None
    error_cases: tuple[str, ...]
    related: tuple[str, ...]
    auth: str
    notes: tuple[str, ...]
    curl_path: str
    operation: dict[str, Any] = field(default_factory=dict)
    fallback: bool = False

    @property
    def key(self) -> str:
        return route_key(self.method, self.path)


@dataclass(frozen=True)
class HelpIndex:
    endpoints: tuple[HelpEndpoint, ...]
    by_module: dict[str, tuple[HelpEndpoint, ...]]
    by_detail: dict[tuple[str, str], HelpEndpoint]
    by_key: dict[str, HelpEndpoint]


ROUTE_DOCS: tuple[RouteDoc, ...] = (
    RouteDoc(
        method="GET",
        path="/health",
        module="ops",
        slug="health",
        title="Health Check",
        summary="Returns service liveness plus dependency readiness summaries.",
        description="Use this endpoint to confirm that the API process is running and to see compact readiness states for PostgreSQL, provider configuration, MySQL execution, schema assets, and teach-confirmation alerts.",
        response_example={"status": "ok", "db": "connected", "provider_config": {"status": "ok", "issue_count": 0}},
        error_cases=("Normally returns HTTP 200 even when the DB is degraded.",),
        related=("/health/config", "/health/runtime", "/telemetry/summary", "/telemetry/recent"),
    ),
    RouteDoc(
        method="GET",
        path="/health/llm",
        module="ops",
        slug="health-llm",
        title="LLM Health",
        summary="Probe a single model role or embedding provider.",
        description="Use this endpoint to probe the active provider/model assignment for sql, reasoning, query rewrite, answer, default, or embedding. Embedding probes use the embedding transport directly and return a structured degraded or unhealthy payload when the provider is unavailable.",
        response_example={
            "role": "embedding",
            "provider": "custom",
            "model": "bge-large-en-v1.5",
            "status": "degraded",
            "healthy": False,
            "error_message": "EMBEDDING_API_URL is not configured.",
        },
        error_cases=("HTTP 422 only for unsupported role values.",),
        related=("/config/model-routing", "/health/vector", "/health/config"),
    ),
    RouteDoc(
        method="GET",
        path="/config/model-routing",
        module="ops",
        slug="config-model-routing",
        title="Model Routing",
        summary="Inspect live task-to-model routing.",
        description="Use this page to inspect the active provider/model assignments for SQL generation, reasoning, query rewrite, answer generation, embeddings, and startup enforcement mode.",
        response_example={
            "startup_enforcement_mode": "warn",
            "sql": {"provider": "ollama", "model": "deepseek-coder:6.7b"},
        },
        error_cases=("GET should always succeed while the service is running.",),
        related=("/health/config", "/health/runtime"),
    ),
    RouteDoc(
        method="GET",
        path="/config/ask-model",
        module="ops",
        slug="config-ask-model",
        title="Ask Model",
        summary="Inspect the model used by /ask answer generation.",
        description="Use this page to inspect the active provider/model assignment for the natural-language answer step used by /ask and /ask/stream.",
        response_example={
            "provider": "ollama",
            "model": "deepseek-coder:6.7b",
            "api_key_configured": False,
        },
        error_cases=("GET should always succeed while the service is running.",),
        related=("/config/model-routing", "/health/llm?role=answer"),
    ),
    RouteDoc(
        method="PATCH",
        path="/config/ask-model",
        module="ops",
        slug="patch-config-ask-model",
        title="Ask Model Patch",
        summary="Patch the model used by /ask answer generation.",
        description="Use this route when you only want to change the provider/model used for the final answer step in /ask and /ask/stream. Changes apply immediately to the running process and do not persist across restart.",
        request_example={"provider": "ollama", "model": "deepseek-coder:6.7b"},
        response_example={
            "provider": "ollama",
            "model": "deepseek-coder:6.7b",
            "api_key_configured": False,
        },
        error_cases=("HTTP 422 when the resulting ask-model configuration is invalid.",),
        related=("/config/model-routing", "/config/ask-model"),
    ),
    RouteDoc(
        method="GET",
        path="/health/runtime",
        module="ops",
        slug="health-runtime",
        title="Runtime Dependency Readiness",
        summary="Returns detailed readiness for MySQL execution and schema assets.",
        description="Use this endpoint during deployment checks to confirm that the MySQL target is configured and reachable and that required schema/docs assets are present on disk.",
        response_example={"status": "ok", "mysql_target": {"status": "ok"}, "schema_assets": {"status": "ok"}},
        error_cases=("Normally returns HTTP 200 with embedded error details when runtime dependencies are missing.",),
        related=("/health", "/health/config", "/health/vector"),
    ),
    RouteDoc(
        method="GET",
        path="/telemetry/recent",
        module="ops",
        slug="telemetry-recent",
        title="Recent Telemetry",
        summary="Lists recent request telemetry events.",
        description="Use this endpoint during debugging to inspect recently persisted request outcomes, warning codes, latency, and endpoint names.",
        response_example={"results": [{"endpoint": "/ask", "status": "ok", "latency_ms": 1250}]},
        error_cases=("HTTP 503 when the PostgreSQL pool is unavailable.", "HTTP 422 for invalid query parameter types."),
        related=("/telemetry/summary", "/benchmark/cases"),
        curl_path="/telemetry/recent?limit=20&endpoint=/ask",
    ),
    RouteDoc(
        method="GET",
        path="/telemetry/summary",
        module="ops",
        slug="telemetry-summary",
        title="Telemetry Summary",
        summary="Returns aggregate telemetry KPIs for monitoring and release gates.",
        description="Use this endpoint to review request volume, ok/clarification/rejected rates, latency percentiles, and grouped error sources over a time window.",
        response_example={
            "endpoint": "/ask",
            "since_minutes": 60,
            "total_requests": 12,
            "ok_count": 10,
            "clarification_count": 1,
            "rejected_count": 1,
        },
        error_cases=("HTTP 503 when the PostgreSQL pool is unavailable.", "HTTP 422 for invalid query parameter types."),
        related=("/telemetry/recent", "/benchmark/cases"),
        curl_path="/telemetry/summary?endpoint=/ask&since_minutes=60",
    ),
    RouteDoc(
        method="GET",
        path="/metrics/teach",
        module="ops",
        slug="teach-metrics",
        title="Teach Metrics",
        summary="Returns operational counts for pending teach confirmations.",
        description="Use this endpoint to monitor active pending teach confirmations, expired confirmation buildup, and the next pending expiry timestamp.",
        response_example={
            "pending_active_count": 2,
            "pending_expired_count": 0,
            "oldest_pending_created_at": "2026-06-01T10:00:00Z",
            "next_pending_expiry_at": "2026-06-01T10:30:00Z",
        },
        error_cases=("HTTP 503 when the PostgreSQL pool is unavailable.",),
        related=("/teach/pending", "/teach/pending/cleanup", "/teach/confirm"),
    ),
    RouteDoc(
        method="GET",
        path="/logs/days",
        module="ops",
        slug="logs-days",
        title="List Log Days",
        summary="Lists the repo-local active and rotated daily log files.",
        description="Use this endpoint to see which day-wise JSON log files are available under the configured observability log directory.",
        response_example={
            "log_dir": "/var/www/py-workspace/nl2sql/logs",
            "results": [
                {"day": "current", "file": "nl2sql.log", "is_active": True},
                {"day": "2026-06-02", "file": "nl2sql.log.2026-06-02", "is_active": False},
            ],
        },
        error_cases=("GET should always succeed while the service is running.",),
        related=("/logs/recent", "/logs/stream"),
    ),
    RouteDoc(
        method="GET",
        path="/logs/recent",
        module="ops",
        slug="logs-recent",
        title="Recent Log Lines",
        summary="Returns the most recent lines from the selected repo-local log file.",
        description="Use this endpoint when you need to inspect the current log or a rotated day file without shell access on the host.",
        response_example={
            "day": "current",
            "file": "nl2sql.log",
            "lines": ['{"level":"INFO","message":"Service started"}'],
            "total_lines_returned": 1,
        },
        error_cases=("HTTP 404 when the requested log file does not exist.", "HTTP 422 for invalid day or lines parameters."),
        related=("/logs/days", "/logs/stream"),
        curl_path="/logs/recent?day=current&lines=200",
    ),
    RouteDoc(
        method="GET",
        path="/logs/stream",
        module="ops",
        slug="logs-stream",
        title="Stream Log Feed",
        summary="Streams repo-local log lines as NDJSON for live debugging.",
        description="Use this endpoint to tail the active JSON log file over HTTP. Backlog lines are sent first, then new lines are emitted as they are written.",
        response_example={"event": "log_line", "day": "current", "file": "nl2sql.log", "line": '{"level":"INFO","message":"..."}'},
        error_cases=("HTTP 404 when the requested log file does not exist.", "HTTP 422 for invalid day, backlog, or poll interval parameters."),
        related=("/logs/days", "/logs/recent", "/telemetry/recent"),
        curl_path="/logs/stream?day=current&backlog=50&follow=true",
    ),
    RouteDoc(
        method="POST",
        path="/benchmark/cases",
        module="ops",
        slug="benchmark-create",
        title="Create Benchmark Case",
        summary="Adds a benchmark case for replay and regression checks.",
        description="Use this endpoint to persist expected NL2SQL behavior that can later be replayed by the benchmark script.",
        request_example={
            "query": "show unpaid invoices by counselor",
            "gold_sql": "SELECT id FROM invoice WHERE status='unpaid'",
            "expected_status": "ok",
            "slices": ["joins"],
            "source": "manual",
            "metadata": {},
        },
        response_example={"id": 1, "query": "show unpaid invoices by counselor", "expected_status": "ok"},
        error_cases=("HTTP 503 when the PostgreSQL pool is unavailable.", "HTTP 422 for invalid request body shape."),
        related=("/benchmark/cases", "/telemetry/summary", "/generate-sql"),
    ),
    RouteDoc(
        method="GET",
        path="/benchmark/cases",
        module="ops",
        slug="benchmark-list",
        title="List Benchmark Cases",
        summary="Lists stored benchmark cases ordered by newest first.",
        description="Use this endpoint to inspect replay cases that are active for evaluation and release checks.",
        response_example={"results": [{"id": 1, "query": "show unpaid invoices", "expected_status": "ok"}]},
        error_cases=("HTTP 503 when the PostgreSQL pool is unavailable.", "HTTP 422 for invalid query parameter types."),
        related=("/benchmark/cases", "/telemetry/summary"),
        curl_path="/benchmark/cases?limit=50&active_only=true",
    ),
    RouteDoc(
        method="POST",
        path="/ingest",
        module="ingestion",
        slug="ingest",
        title="Ingest Text Or Schema",
        summary="Embeds free text or schema table records into vector storage.",
        description="Use this endpoint for direct ingestion of documentation text or explicit schema table objects into the pgvector-backed embeddings table.",
        request_example={"type": "text", "source": "docs:faq", "text": "Long business documentation text..."},
        response_example={"inserted": 3, "updated": 0, "source": "docs:faq"},
        error_cases=(
            "HTTP 503 when the PostgreSQL pool is unavailable.",
            "HTTP 502 when the embedding upstream times out or fails.",
            "HTTP 422 for invalid request body shape or embedding dimension mismatch.",
        ),
        related=("/query", "/ingest/groups", "/ingest/knowledge"),
    ),
    RouteDoc(
        method="POST",
        path="/query",
        module="retrieval",
        slug="query",
        title="Vector Query",
        summary="Runs cosine similarity retrieval across embedded chunks.",
        description="Use this endpoint to retrieve relevant text, schema, learned pattern, or instruction chunks without generating SQL.",
        request_example={"query": "find unpaid invoices", "top_k": 5},
        response_example={"results": [{"content": "invoice status context", "similarity": 0.87, "metadata": {"type": "schema_group"}}]},
        error_cases=(
            "HTTP 503 when the PostgreSQL pool is unavailable.",
            "HTTP 502 when the embedding upstream times out or fails.",
            "HTTP 422 for invalid request body shape.",
        ),
        related=("/query/groups", "/ingest", "/generate-sql"),
    ),
    RouteDoc(
        method="POST",
        path="/ingest/groups",
        module="ingestion",
        slug="ingest-groups",
        title="Ingest Schema Groups",
        summary="Embeds schema-group chunks from rag_schema entity files.",
        description="Use this endpoint after schema metadata changes to store group-level context with table lists, aliases, examples, and live column enrichment when available.",
        request_example={"group_names": ["inquiry_lifecycle"]},
        response_example={
            "inserted": 1,
            "updated": 0,
            "source": "inquiry_lifecycle",
            "enrichment_summary": {"groups_with_columns": 1, "groups_without_columns": 0},
            "failed_groups": [],
            "failure_count": 0,
        },
        error_cases=(
            "HTTP 200 with partial=true semantics in the response when some groups exceed the token ceiling.",
            "HTTP 503 when the PostgreSQL pool is unavailable.",
            "HTTP 502 when the embedding upstream times out or fails.",
        ),
        related=("/ingest/groups/status", "/query/groups", "/generate-sql"),
    ),
    RouteDoc(
        method="GET",
        path="/ingest/groups/status",
        module="ingestion",
        slug="ingest-groups-status",
        title="Schema Group Status",
        summary="Compares current schema-group file hashes with embedded versions.",
        description="Use this endpoint to decide whether schema groups need re-ingestion after rag_schema files are regenerated.",
        response_example={"groups": [], "current_count": 0, "stale_count": 0, "never_embedded_count": 0},
        error_cases=("HTTP 503 when the PostgreSQL pool is unavailable.",),
        related=("/ingest/groups", "/query/groups"),
    ),
    RouteDoc(
        method="POST",
        path="/ingest/knowledge",
        module="ingestion",
        slug="ingest-knowledge",
        title="Ingest Enriched Knowledge",
        summary="Embeds generated column catalogs, SQL examples, relation links, graph nodes, view registry, and rules.",
        description="Use this endpoint to refresh the richer NL2SQL knowledge corpus from generated docs and rag_schema metadata sources.",
        request_example={
            "include_column_catalog": True,
            "include_sql_examples": True,
            "include_relations": True,
            "include_graph": True,
            "include_view_registry": True,
            "include_onboarding_rules": True,
            "sql_example_limit": 200,
        },
        response_example={"inserted": 12, "updated": 2, "source": "knowledge"},
        error_cases=(
            "HTTP 503 when the PostgreSQL pool is unavailable.",
            "HTTP 502 when the embedding upstream times out or fails.",
            "HTTP 422 for invalid request body shape.",
        ),
        related=("/query", "/query/groups", "/ingest/groups"),
    ),
    RouteDoc(
        method="POST",
        path="/ingest/patterns",
        module="ingestion",
        slug="ingest-patterns",
        title="Ingest Learned Patterns",
        summary="Manually embeds active learned SQL patterns.",
        description="Use this manual or cron endpoint after successful ask traffic accumulates. Live prompting can still read patterns directly without this route.",
        request_example={},
        response_example={"embedded": 2, "source": "learned_patterns"},
        error_cases=("HTTP 503 when the PostgreSQL pool is unavailable.", "HTTP 502 when the embedding upstream times out or fails."),
        related=("/patterns/feedback", "/ask", "/query/groups"),
    ),
    RouteDoc(
        method="POST",
        path="/ingest/instructions",
        module="ingestion",
        slug="ingest-instructions",
        title="Ingest User Instructions",
        summary="Manually embeds active user instructions that meet the confidence threshold.",
        description="Use this optional manual or cron endpoint to mirror active instructions into vector storage. SQL generation also reads live instructions directly.",
        request_example={},
        response_example={"embedded": 2, "source": "user_instructions"},
        error_cases=("HTTP 503 when the PostgreSQL pool is unavailable.", "HTTP 502 when the embedding upstream times out or fails."),
        related=("/teach", "/instructions", "/generate-sql"),
    ),
    RouteDoc(
        method="POST",
        path="/query/groups",
        module="retrieval",
        slug="query-groups",
        title="Schema Group Query",
        summary="Retrieves schema-group context ready for LLM prompting.",
        description="Use this endpoint to see matched groups, tables in scope, composed prompt context, and raw retrieval results before SQL generation.",
        request_example={"query": "show unpaid invoices by counselor", "top_k": 3},
        response_example={"matched_groups": ["billing"], "tables_in_scope": ["invoice"], "context": "Group: billing", "results": []},
        error_cases=(
            "HTTP 503 when the PostgreSQL pool is unavailable.",
            "HTTP 502 when the embedding upstream times out or fails.",
            "HTTP 422 for invalid request body shape.",
        ),
        related=("/query", "/generate-sql", "/ask"),
    ),
    RouteDoc(
        method="POST",
        path="/patterns/feedback",
        module="learning",
        slug="patterns-feedback",
        title="Pattern Feedback",
        summary="Boosts or deactivates a learned pattern based on human feedback.",
        description="Use this endpoint to mark a learned SQL pattern as helpful for future retrieval or unhelpful so it stops being used.",
        request_example={"pattern_id": 1, "helpful": True},
        response_example={"pattern_id": 1, "action": "boosted"},
        error_cases=("HTTP 404 when the pattern id is not found.", "HTTP 503 when the PostgreSQL pool is unavailable.", "HTTP 422 for invalid request body shape."),
        related=("/ingest/patterns", "/ask"),
    ),
    RouteDoc(
        method="POST",
        path="/teach",
        module="learning",
        slug="teach",
        title="Teach Instruction",
        summary="Saves user-provided instructions that guide SQL generation.",
        description="Use this endpoint to teach business rules, term mappings, table relationships, query methodology, filter rules, or corrections.",
        request_example={
            "instruction_type": "term_mapping",
            "content": "counselor means employee table",
            "tables_affected": ["employee"],
            "source_query": "show counselor sales",
        },
        response_example={
            "learning_status": "saved_new",
            "message": "This instruction is new. I've saved it.",
            "instruction_id": 42,
            "similar_instructions": [],
            "requires_confirmation": False,
            "confirmation_token": None,
        },
        error_cases=("HTTP 200 with learning_status=rejected for controlled learning failures.", "HTTP 503 when the PostgreSQL pool is unavailable.", "HTTP 422 for invalid request body shape."),
        related=("/teach/confirm", "/instructions", "/ingest/instructions"),
    ),
    RouteDoc(
        method="POST",
        path="/teach/confirm",
        module="learning",
        slug="teach-confirm",
        title="Confirm Instruction Conflict",
        summary="Resolves a pending instruction conflict created by /teach.",
        description="Use this endpoint within 30 minutes of a conflict response to confirm, reject, or replace the pending instruction.",
        request_example={"confirmation_token": "9f0c2a8b1234abcd", "action": "replace"},
        response_example={"learning_status": "confirmed", "message": "Instruction confirmed.", "instruction_id": 43, "similar_instructions": [], "requires_confirmation": False, "confirmation_token": None},
        error_cases=("HTTP 200 with learning_status=rejected when the token/action cannot be processed.", "HTTP 503 when the PostgreSQL pool is unavailable.", "HTTP 422 for invalid request body shape."),
        related=("/teach", "/instructions"),
    ),
    RouteDoc(
        method="GET",
        path="/teach/pending",
        module="learning",
        slug="teach-pending-list",
        title="List Pending Teach Confirmations",
        summary="Lists pending teach confirmations for admin review.",
        description="Use this endpoint to inspect unresolved teach confirmation tokens, their pending instruction payloads, expiry timestamps, and aggregate stats.",
        response_example={
            "results": [
                {
                    "token": "9f0c2a8b1234abcd",
                    "instruction_type": "table_relationship",
                    "content": "employee.employee_id = contact.id",
                    "tables_affected": ["employee", "contact"],
                    "source_query": None,
                    "conflicting_id": 12,
                    "created_at": "2026-06-01T10:00:00Z",
                    "expires_at": "2026-06-01T10:30:00Z",
                    "is_expired": False,
                }
            ],
            "stats": {"pending_active_count": 1, "pending_expired_count": 0},
        },
        error_cases=("HTTP 503 when the PostgreSQL pool is unavailable.", "HTTP 422 for invalid query parameter types."),
        related=("/metrics/teach", "/teach/pending/cleanup", "/teach/confirm"),
        curl_path="/teach/pending?limit=20&include_expired=false",
    ),
    RouteDoc(
        method="POST",
        path="/teach/pending/cleanup",
        module="learning",
        slug="teach-pending-cleanup",
        title="Cleanup Pending Teach Confirmations",
        summary="Deletes expired pending teach confirmations immediately.",
        description="Use this endpoint for explicit housekeeping or scheduled cleanup of expired pending teach confirmation tokens.",
        response_example={"deleted": 3, "stats": {"pending_active_count": 2, "pending_expired_count": 0}},
        error_cases=("HTTP 503 when the PostgreSQL pool is unavailable.",),
        related=("/teach/pending", "/metrics/teach", "/teach/confirm"),
    ),
    RouteDoc(
        method="GET",
        path="/instructions",
        module="learning",
        slug="instructions-list",
        title="List Instructions",
        summary="Lists saved user instructions for review.",
        description="Use this endpoint to inspect active or historical instructions, confidence, verification state, counters, and timestamps.",
        response_example=[{"id": 1, "instruction_type": "term_mapping", "content": "counselor means employee table", "is_active": True}],
        error_cases=("HTTP 503 when the PostgreSQL pool is unavailable.", "HTTP 422 for invalid query parameter types."),
        related=("/teach", "/instructions/{instruction_id}", "/ingest/instructions"),
        curl_path="/instructions?active_only=true",
    ),
    RouteDoc(
        method="DELETE",
        path="/instructions/{instruction_id}",
        module="learning",
        slug="instructions-delete",
        title="Delete Instruction",
        summary="Soft-deletes a user instruction and marks its embedded copy inactive.",
        description="Use this endpoint to deactivate a stored instruction without hard-deleting its audit history.",
        response_example={"deactivated": True, "instruction_id": 1},
        error_cases=("HTTP 503 when the PostgreSQL pool is unavailable.", "HTTP 422 when instruction_id is not an integer."),
        related=("/instructions", "/teach", "/ingest/instructions"),
        curl_path="/instructions/1",
    ),
    RouteDoc(
        method="POST",
        path="/generate-sql",
        module="generation",
        slug="generate-sql",
        title="Generate SQL",
        summary="Generates guarded MySQL SELECT SQL without executing it.",
        description="Use this endpoint when a caller wants validated SQL, matched groups, warnings, and ReAct trace details while keeping execution under caller control.",
        request_example={"query": "show me the 5 most recent inquiries", "top_k": 3, "request_id": "demo-001"},
        response_example={"status": "ok", "sql": "SELECT id FROM inquiry ORDER BY created_at DESC LIMIT 5", "warnings": [], "tables_used": ["inquiry"], "matched_groups": ["inquiry_lifecycle"], "attempt_count": 1},
        error_cases=(
            "HTTP 200 with status=clarification_needed when the model needs more user input.",
            "HTTP 200 with status=rejected for model transport or malformed-output failures.",
            "HTTP 503 when the PostgreSQL pool is unavailable.",
            "HTTP 422 for invalid request body shape.",
        ),
        related=("/query/groups", "/ask", "/ask/stream"),
    ),
    RouteDoc(
        method="POST",
        path="/ask",
        module="generation",
        slug="ask",
        title="Ask Question",
        summary="Runs NL question to SQL generation, bounded MySQL execution, and natural-language answer generation.",
        description="Use this endpoint when app or backend callers want one blocking JSON response containing the final answer and SQL metadata. Deterministic recent-list queries bypass the answer LLM and return a direct fallback answer.",
        request_example={"query": "show me the 5 most recent inquiries", "top_k": 3, "request_id": "demo-001"},
        response_example={"status": "ok", "answer": "Here are the 5 most recent inquiries.", "sql": "SELECT id FROM inquiry ORDER BY created_at DESC LIMIT 5", "warnings": [], "row_count": 5, "columns": ["id"]},
        error_cases=(
            "HTTP 200 with status=clarification_needed when SQL generation needs clarification.",
            "HTTP 200 with status=rejected for generation transport failures or MySQL execution failures.",
            "HTTP 503 when the PostgreSQL pool is unavailable.",
            "HTTP 422 for invalid request body shape.",
        ),
        related=("/generate-sql", "/ask/stream", "/query/groups"),
    ),
    RouteDoc(
        method="POST",
        path="/ask/stream",
        module="generation",
        slug="ask-stream",
        title="Ask Streaming",
        summary="Runs the ask workflow as newline-delimited JSON progress events.",
        description="Use this endpoint for terminal and debugging workflows where progress events are useful before the final answer response. Deterministic recent-list queries skip the answer LLM and finish faster than non-deterministic asks.",
        request_example={"query": "show me the 5 most recent inquiries", "top_k": 3, "request_id": "demo-001"},
        response_example={"event": "final", "response": {"status": "ok", "answer": "Here are the matching rows."}},
        error_cases=(
            "Final NDJSON event can contain status=clarification_needed or status=rejected.",
            "HTTP 503 when the PostgreSQL pool is unavailable.",
            "HTTP 422 for invalid request body shape.",
        ),
        related=("/ask", "/generate-sql", "/query/groups"),
    ),
)

ROUTE_DOC_BY_KEY = {doc.key: doc for doc in ROUTE_DOCS}


def build_help_index(openapi_schema: dict[str, Any]) -> HelpIndex:
    endpoints: list[HelpEndpoint] = []
    for path, method, operation in iter_openapi_operations(openapi_schema):
        key = route_key(method, path)
        doc = ROUTE_DOC_BY_KEY.get(key)
        if doc is None:
            doc = _fallback_doc(path=path, method=method, operation=operation)
            fallback = True
        else:
            fallback = False

        endpoints.append(
            HelpEndpoint(
                method=method,
                path=path,
                module=doc.module,
                slug=doc.slug,
                title=doc.title,
                summary=doc.summary,
                description=doc.description,
                request_example=doc.request_example,
                response_example=doc.response_example,
                error_cases=doc.error_cases,
                related=doc.related,
                auth=doc.auth,
                notes=doc.notes,
                curl_path=doc.curl_path or _sample_path(path),
                operation=operation,
                fallback=fallback,
            )
        )

    endpoints.sort(key=_endpoint_sort_key)
    by_module: dict[str, list[HelpEndpoint]] = {}
    by_detail: dict[tuple[str, str], HelpEndpoint] = {}
    by_key: dict[str, HelpEndpoint] = {}
    for endpoint in endpoints:
        by_module.setdefault(endpoint.module, []).append(endpoint)
        by_detail[(endpoint.module, endpoint.slug)] = endpoint
        by_key[endpoint.key] = endpoint

    return HelpIndex(
        endpoints=tuple(endpoints),
        by_module={key: tuple(value) for key, value in by_module.items()},
        by_detail=by_detail,
        by_key=by_key,
    )


def iter_openapi_operations(openapi_schema: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    operations: list[tuple[str, str, dict[str, Any]]] = []
    for path, path_item in openapi_schema.get("paths", {}).items():
        if _is_internal_path(path):
            continue
        for method, operation in path_item.items():
            if method.lower() not in HTTP_METHODS:
                continue
            operations.append((path, method.upper(), operation))
    return operations


def endpoint_search_text(endpoint: HelpEndpoint) -> str:
    return " ".join(
        [
            endpoint.method,
            endpoint.path,
            endpoint.module,
            endpoint.title,
            endpoint.summary,
            endpoint.description,
            " ".join(endpoint.related),
        ]
    ).lower()


def request_body_schema_label(endpoint: HelpEndpoint) -> str:
    return _request_body_schema(endpoint.operation)


def response_schema_label(endpoint: HelpEndpoint) -> str:
    return _response_schema(endpoint.operation)


def parameter_rows(endpoint: HelpEndpoint) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for param in endpoint.operation.get("parameters", []):
        rows.append(
            {
                "name": str(param.get("name", "parameter")),
                "location": str(param.get("in", "query")),
                "required": "required" if param.get("required") else "optional",
                "type": _schema_label(param.get("schema", {})),
                "description": str(param.get("description") or ""),
            }
        )
    return rows


def curl_command(endpoint: HelpEndpoint, base_url: str) -> str:
    url = f"{base_url.rstrip('/')}{endpoint.curl_path}"
    lines = [f"curl -s -X {endpoint.method} {json.dumps(url)}"]
    if endpoint.request_example is not None and endpoint.method in {"POST", "PUT", "PATCH"}:
        payload = json.dumps(endpoint.request_example, separators=(",", ":"))
        lines.append('  -H "Content-Type: application/json"')
        lines.append(f"  -d {json.dumps(payload)}")
    lines.append("  | python3 -m json.tool")
    return " \\\n".join(lines)


def render_index_page(openapi_schema: dict[str, Any], base_url: str) -> str:
    index = build_help_index(openapi_schema)
    module_cards = "\n".join(_module_card(module, routes) for module, routes in _ordered_modules(index))
    route_cards = "\n".join(_endpoint_card(endpoint) for endpoint in index.endpoints)
    script = """
<script>
const input = document.querySelector("#route-search");
const cards = Array.from(document.querySelectorAll("[data-route-card]"));
input.addEventListener("input", () => {
  const value = input.value.trim().toLowerCase();
  for (const card of cards) {
    const haystack = card.dataset.search || "";
    card.hidden = value !== "" && !haystack.includes(value);
  }
});
</script>
"""
    content = f"""
<section class="hero">
  <p class="eyebrow">Internal API Guide</p>
  <h1>NL2SQL Route Help</h1>
  <p class="lead">Browse the available service routes, inspect request and response shapes, and copy terminal-ready examples.</p>
  <label class="search-label" for="route-search">Search routes</label>
  <input id="route-search" class="search-input" type="search" placeholder="Filter by path, method, module, or description" autocomplete="off">
</section>
<section>
  <h2>Modules</h2>
  <div class="module-grid">{module_cards}</div>
</section>
<section>
  <h2>All Routes</h2>
  <div class="route-grid">{route_cards}</div>
</section>
"""
    return _layout("NL2SQL API Help", content, base_url=base_url, script=script)


def render_module_page(module: str, openapi_schema: dict[str, Any], base_url: str) -> str | None:
    index = build_help_index(openapi_schema)
    endpoints = index.by_module.get(module)
    if endpoints is None:
        return None
    label = MODULE_LABELS.get(module, module.title())
    route_cards = "\n".join(_endpoint_card(endpoint) for endpoint in endpoints)
    content = f"""
<nav class="breadcrumb"><a href="/help">Help</a><span>{escape(label)}</span></nav>
<section class="hero compact">
  <p class="eyebrow">{escape(label)}</p>
  <h1>{escape(label)} Routes</h1>
  <p class="lead">{len(endpoints)} documented endpoint{"s" if len(endpoints) != 1 else ""} in this module.</p>
</section>
<section>
  <div class="route-grid">{route_cards}</div>
</section>
"""
    return _layout(f"{label} Help", content, base_url=base_url)


def render_detail_page(module: str, route_slug: str, openapi_schema: dict[str, Any], base_url: str) -> str | None:
    index = build_help_index(openapi_schema)
    endpoint = index.by_detail.get((module, route_slug))
    if endpoint is None:
        return None

    module_label = MODULE_LABELS.get(endpoint.module, endpoint.module.title())
    params = _parameters_table(endpoint.operation.get("parameters", []))
    body_schema = _request_body_schema(endpoint.operation)
    response_schema = _response_schema(endpoint.operation)
    request_example = _example_block(endpoint.request_example, empty="No request body.")
    response_example = _example_block(endpoint.response_example, empty="No curated sample response.")
    curl = _curl_block(endpoint, base_url)
    related = _related_links(endpoint, index)
    notes = _list_items(endpoint.notes)
    errors = _list_items(endpoint.error_cases or ("HTTP 422 for invalid request shape when applicable.",))

    content = f"""
<nav class="breadcrumb"><a href="/help">Help</a><a href="/help/{escape(endpoint.module)}">{escape(module_label)}</a><span>{escape(endpoint.title)}</span></nav>
<section class="hero compact">
  <p class="eyebrow">{escape(module_label)}</p>
  <h1>{escape(endpoint.title)}</h1>
  <div class="route-line"><span class="method method-{endpoint.method.lower()}">{endpoint.method}</span><code>{escape(endpoint.path)}</code></div>
  <p class="lead">{escape(endpoint.summary)}</p>
</section>
<section class="detail-grid">
  <article class="panel">
    <h2>What It Does</h2>
    <p>{escape(endpoint.description)}</p>
  </article>
  <article class="panel">
    <h2>Authentication</h2>
    <p>{escape(endpoint.auth)}</p>
  </article>
</section>
<section class="panel">
  <h2>Parameters</h2>
  {params}
</section>
<section class="detail-grid">
  <article class="panel">
    <h2>Request Body</h2>
    <p class="schema-label">{escape(body_schema)}</p>
    {request_example}
  </article>
  <article class="panel">
    <h2>Expected Return Format</h2>
    <p class="schema-label">{escape(response_schema)}</p>
    {response_example}
  </article>
</section>
<section class="panel">
  <h2>How To Call</h2>
  {curl}
</section>
<section class="detail-grid">
  <article class="panel">
    <h2>Error Responses</h2>
    <ul>{errors}</ul>
  </article>
  <article class="panel">
    <h2>Related Routes</h2>
    {related}
  </article>
</section>
{f'<section class="panel"><h2>Notes</h2><ul>{notes}</ul></section>' if endpoint.notes else ''}
"""
    return _layout(f"{endpoint.title} Help", content, base_url=base_url)


def _fallback_doc(path: str, method: str, operation: dict[str, Any]) -> RouteDoc:
    tags = operation.get("tags") or ["ops"]
    module = tags[0] if tags[0] in MODULE_LABELS else "ops"
    summary = operation.get("summary") or f"{method.upper()} {path}"
    description = operation.get("description") or "This route is included from FastAPI OpenAPI metadata. Curated help copy has not been added yet."
    return RouteDoc(
        method=method,
        path=path,
        module=module,
        slug=_slug_for(method, path),
        title=summary,
        summary=summary,
        description=description,
        error_cases=("HTTP 422 for invalid request shape when applicable.",),
    )


def _endpoint_sort_key(endpoint: HelpEndpoint) -> tuple[int, str, int, str]:
    return (
        MODULE_ORDER.get(endpoint.module, 99),
        endpoint.path,
        METHOD_ORDER.get(endpoint.method, 99),
        endpoint.slug,
    )


def _ordered_modules(index: HelpIndex) -> list[tuple[str, tuple[HelpEndpoint, ...]]]:
    return sorted(index.by_module.items(), key=lambda item: MODULE_ORDER.get(item[0], 99))


def _is_internal_path(path: str) -> bool:
    return path in {"/docs", "/redoc", "/openapi.json"} or path.startswith("/help")


def _slug_for(method: str, path: str) -> str:
    parts = [method.lower(), *re.findall(r"[A-Za-z0-9]+", path)]
    return "-".join(parts)


def _sample_path(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "1", path)


def _layout(title: str, content: str, *, base_url: str, script: str = "") -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f7f8fa;
      --surface: #ffffff;
      --text: #1f2937;
      --muted: #5f6b7a;
      --line: #d9dee7;
      --accent: #0f766e;
      --accent-dark: #115e59;
      --code: #111827;
      --code-bg: #eef2f7;
      --get: #2563eb;
      --post: #0f766e;
      --delete: #b42318;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.5;
    }}
    a {{ color: var(--accent-dark); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .shell {{ max-width: 1180px; margin: 0 auto; padding: 24px; }}
    .topbar {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
      padding: 12px 0 24px;
    }}
    .brand {{ font-weight: 700; color: var(--text); }}
    .base-url {{ color: var(--muted); font-size: 0.9rem; overflow-wrap: anywhere; }}
    .hero {{
      padding: 32px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
      margin-bottom: 24px;
    }}
    .hero.compact {{ padding: 24px; }}
    .eyebrow {{
      margin: 0 0 8px;
      color: var(--accent-dark);
      font-size: 0.78rem;
      font-weight: 700;
      letter-spacing: 0;
      text-transform: uppercase;
    }}
    h1, h2, h3 {{ line-height: 1.2; letter-spacing: 0; }}
    h1 {{ margin: 0 0 12px; font-size: 2rem; }}
    h2 {{ margin: 0 0 16px; font-size: 1.2rem; }}
    h3 {{ margin: 0 0 8px; font-size: 1rem; }}
    .lead {{ max-width: 760px; margin: 0 0 20px; color: var(--muted); }}
    .search-label {{ display: block; margin-bottom: 6px; font-weight: 700; }}
    .search-input {{
      width: 100%;
      max-width: 720px;
      padding: 12px 14px;
      border: 1px solid var(--line);
      border-radius: 6px;
      font: inherit;
    }}
    section {{ margin-bottom: 24px; }}
    .module-grid, .route-grid, .detail-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 14px;
    }}
    .module-card, .route-card, .panel {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
      padding: 18px;
    }}
    .route-card[hidden] {{ display: none; }}
    .route-head, .route-line {{
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
      margin-bottom: 10px;
    }}
    .method {{
      display: inline-flex;
      min-width: 58px;
      justify-content: center;
      border-radius: 6px;
      padding: 4px 8px;
      color: #fff;
      font-weight: 800;
      font-size: 0.78rem;
      letter-spacing: 0;
    }}
    .method-get {{ background: var(--get); }}
    .method-post {{ background: var(--post); }}
    .method-delete {{ background: var(--delete); }}
    code {{
      border-radius: 6px;
      background: var(--code-bg);
      color: var(--code);
      padding: 2px 6px;
      overflow-wrap: anywhere;
    }}
    pre {{
      overflow-x: auto;
      white-space: pre;
      border-radius: 8px;
      background: #101827;
      color: #f8fafc;
      padding: 14px;
      font-size: 0.9rem;
    }}
    pre code {{ background: transparent; color: inherit; padding: 0; }}
    .summary, .muted, .schema-label {{ color: var(--muted); }}
    .meta-list {{ display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px; }}
    .pill {{
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 3px 8px;
      color: var(--muted);
      font-size: 0.82rem;
    }}
    .breadcrumb {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin: 0 0 16px;
      color: var(--muted);
      font-size: 0.92rem;
    }}
    .breadcrumb > *::after {{ content: "/"; margin-left: 8px; color: var(--muted); }}
    .breadcrumb > *:last-child::after {{ content: ""; margin: 0; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ text-align: left; border-bottom: 1px solid var(--line); padding: 10px 8px; vertical-align: top; }}
    th {{ color: var(--muted); font-size: 0.85rem; }}
    ul {{ margin: 0; padding-left: 20px; }}
    li + li {{ margin-top: 6px; }}
    @media (max-width: 640px) {{
      .shell {{ padding: 16px; }}
      .hero {{ padding: 20px; }}
      h1 {{ font-size: 1.55rem; }}
      .topbar {{ align-items: flex-start; flex-direction: column; }}
    }}
  </style>
</head>
<body>
  <main class="shell">
    <header class="topbar">
      <a class="brand" href="/help">NL2SQL Help</a>
      <span class="base-url">{escape(base_url)}</span>
    </header>
    {content}
  </main>
  {script}
</body>
</html>"""


def _module_card(module: str, endpoints: tuple[HelpEndpoint, ...]) -> str:
    label = MODULE_LABELS.get(module, module.title())
    return f"""
<a class="module-card" href="/help/{escape(module)}">
  <h3>{escape(label)}</h3>
  <p class="summary">{len(endpoints)} endpoint{"s" if len(endpoints) != 1 else ""}</p>
</a>
"""


def _endpoint_card(endpoint: HelpEndpoint) -> str:
    search = endpoint_search_text(endpoint)
    fallback = '<span class="pill">OpenAPI fallback</span>' if endpoint.fallback else ""
    return f"""
<article class="route-card" data-route-card data-search="{escape(search)}">
  <div class="route-head"><span class="method method-{endpoint.method.lower()}">{endpoint.method}</span><code>{escape(endpoint.path)}</code></div>
  <h3><a href="/help/{escape(endpoint.module)}/{escape(endpoint.slug)}">{escape(endpoint.title)}</a></h3>
  <p class="summary">{escape(endpoint.summary)}</p>
  <div class="meta-list">
    <span class="pill">{escape(MODULE_LABELS.get(endpoint.module, endpoint.module.title()))}</span>
    <span class="pill">{escape(_required_input_summary(endpoint))}</span>
    {fallback}
  </div>
</article>
"""


def _required_input_summary(endpoint: HelpEndpoint) -> str:
    parameters = endpoint.operation.get("parameters", [])
    required = [param.get("name", "parameter") for param in parameters if param.get("required")]
    if endpoint.operation.get("requestBody") is not None:
        required.append("JSON body")
    if not required:
        return "No required params"
    return "Requires " + ", ".join(str(item) for item in required[:3])


def _parameters_table(parameters: list[dict[str, Any]]) -> str:
    if not parameters:
        return '<p class="muted">No path or query parameters.</p>'
    rows = []
    for param in parameters:
        schema = param.get("schema", {})
        rows.append(
            "<tr>"
            f"<td><code>{escape(str(param.get('name', 'parameter')))}</code></td>"
            f"<td>{escape(str(param.get('in', 'query')))}</td>"
            f"<td>{'required' if param.get('required') else 'optional'}</td>"
            f"<td>{escape(_schema_label(schema))}</td>"
            f"<td>{escape(str(param.get('description') or ''))}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>Name</th><th>Location</th><th>Required</th><th>Type</th><th>Description</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _request_body_schema(operation: dict[str, Any]) -> str:
    body = operation.get("requestBody")
    if not body:
        return "No request body."
    content = body.get("content", {})
    media = content.get("application/json") or next(iter(content.values()), {})
    return _schema_label(media.get("schema", {}))


def _response_schema(operation: dict[str, Any]) -> str:
    responses = operation.get("responses", {})
    response = responses.get("200") or next(iter(responses.values()), {})
    content = response.get("content", {}) if isinstance(response, dict) else {}
    media = content.get("application/json") or next(iter(content.values()), {})
    schema = media.get("schema", {})
    return _schema_label(schema) if schema else "See sample response."


def _schema_label(schema: dict[str, Any]) -> str:
    if not schema:
        return "object"
    if "$ref" in schema:
        return schema["$ref"].rsplit("/", 1)[-1]
    for key in ("oneOf", "anyOf", "allOf"):
        if key in schema:
            return " or ".join(_schema_label(item) for item in schema[key])
    if schema.get("type") == "array":
        return f"array of {_schema_label(schema.get('items', {}))}"
    if "title" in schema:
        return str(schema["title"])
    return str(schema.get("type", "object"))


def _example_block(value: Any | None, *, empty: str) -> str:
    if value is None:
        return f'<p class="muted">{escape(empty)}</p>'
    return f"<pre><code>{escape(json.dumps(value, indent=2, default=str))}</code></pre>"


def _curl_block(endpoint: HelpEndpoint, base_url: str) -> str:
    return f"<pre><code>{escape(curl_command(endpoint, base_url))}</code></pre>"


def _related_links(endpoint: HelpEndpoint, index: HelpIndex) -> str:
    if not endpoint.related:
        return '<p class="muted">No related routes listed.</p>'
    items = []
    for related_path in endpoint.related:
        matches = [candidate for candidate in index.endpoints if candidate.path == related_path]
        if not matches:
            items.append(f"<li><code>{escape(related_path)}</code></li>")
            continue
        links = ", ".join(
            f'<a href="/help/{escape(match.module)}/{escape(match.slug)}">{escape(match.method)} {escape(match.path)}</a>'
            for match in matches
        )
        items.append(f"<li>{links}</li>")
    return f"<ul>{''.join(items)}</ul>"


def _list_items(items: tuple[str, ...]) -> str:
    return "".join(f"<li>{escape(item)}</li>" for item in items)
