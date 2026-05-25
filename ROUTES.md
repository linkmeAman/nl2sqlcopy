# NL2SQL Route Reference

Current route reference for the standalone FastAPI service.

Base URL examples assume `http://localhost:8080`.

## Shared Behavior

- DB-backed routes return HTTP `503` when the PostgreSQL pool is unavailable.
- Retrieval and generation routes can use in-memory and DB-backed caches.
- `/generate-sql` and `/ask` return additive cache metadata:
  - `cache_hit`
  - `cache_source`
- `cache_source` values:
  - `none`
  - `memory_exact`
  - `memory_semantic`
  - `db_exact`
  - `db_semantic`
- Only `status="ok"` `/generate-sql` and `/ask` responses are cached.
- Teach and ingest mutations bump the persistent cache epoch and clear
  in-memory caches.

## Route List

### Ops

- `GET /health`
- `GET /telemetry/recent`
- `GET /telemetry/summary`
- `GET /failures`
- `GET /cache/stats`
- `POST /cache/clear`
- `GET /governance/rules`
- `POST /governance/validate`
- `POST /benchmark/cases`
- `GET /benchmark/cases`

### Retrieval and Ingest

- `POST /ingest`
- `POST /query`
- `POST /ingest/groups`
- `POST /ingest/knowledge`
- `POST /ingest/patterns`
- `POST /ingest/instructions`
- `POST /query/groups`

### Learning

- `POST /teach`
- `POST /teach/confirm`
- `GET /instructions`
- `DELETE /instructions/{instruction_id}`
- `POST /patterns/feedback`

### Generation

- `POST /generate-sql`
- `POST /ask`
- `POST /ask/stream`

## GET /cache/stats

Returns in-memory cache sizes and TTLs.

Response fields:

- `embed_cache_size`
- `sql_cache_size`
- `semantic_sql_cache_size`
- `ask_cache_size`
- `embed_cache_ttl_seconds`
- `sql_cache_ttl_seconds`
- `ask_cache_ttl_seconds`

Note:

- persistent PostgreSQL query cache state is not cleared by TTL inspection here
- current DB cache epoch can be inferred from the backing tables, not this route

## GET /failures

Returns the most recent entries from the `nl2sql_failure_log` table.

Query parameters:

- `limit` — integer, default `100`
- `endpoint` — optional string filter (e.g. `/ask`)

Each entry includes:

- `id`, `request_id`, `endpoint`, `query_text`
- `warning_codes` — JSONB array of warning code strings
- `error_source` — derived from the first warning, nullable
- `sql_preview` — SQL that caused the failure, nullable
- `tables_attempted` — text array
- `latency_ms`
- `suggest_teach` — pre-built teach payload (see below)
- `created_at`

The `suggest_teach` field contains:

```json
{
  "instruction_type": "term_mapping",
  "content": "When the user asks '...', map ambiguous terms to correct table names.",
  "tables_affected": [],
  "source_query": "...",
  "warning_codes": ["REQUEST_TIMEOUT"],
  "sql_preview": ""
}
```

This payload can be posted directly to `POST /teach` to teach the system
how to handle the failed query.

### Failure Log Population

Entries are written automatically from the `/ask` endpoint on all rejection
paths:

- `asyncio.TimeoutError` (upstream timeout)
- SQL generation `status == "rejected"`
- Execution warnings (e.g. `TABLE_OUT_OF_SCOPE`)
- Answer generation returning `None`

The table is created automatically on service boot via the DDL bootstrap.

## POST /cache/clear

Clears in-memory caches only.

Response fields:

- `embed_cleared`
- `sql_cleared`
- `semantic_sql_cleared`
- `ask_cleared`

## POST /ingest/groups

Embeds schema-group chunks built from `rag_schema`.

Request body:

```json
{
  "group_names": ["inquiry_lifecycle", "sales_invoice_billing"]
}
```

Response fields:

- `inserted`
- `updated`
- `skipped`
- `source`
- `failure_count`
- `failed_groups`
- `enrichment_summary`

Behavior:

- unchanged chunks are skipped before embedding
- successful completion bumps cache epoch

## POST /ingest/knowledge

Embeds enriched knowledge sources such as column catalog, SQL examples,
relations, graph, view registry, and onboarding rules.

Request body supports:

- `include_column_catalog`
- `include_sql_examples`
- `include_relations`
- `include_graph`
- `include_view_registry`
- `include_onboarding_rules`
- `column_limit`
- `sql_example_limit`
- `relation_limit`
- `graph_limit`
- `view_registry_limit`

Response fields:

- `inserted`
- `updated`
- `skipped`
- `source`

Behavior:

- unchanged chunks are skipped before embedding
- successful completion bumps cache epoch

## POST /ingest/patterns

Manually embeds active learned patterns.

Response fields:

- `inserted`
- `updated`
- `skipped`
- `embedded`
- `source`

Behavior:

- successful completion bumps cache epoch

## POST /ingest/instructions

Manually embeds active user instructions above the confidence threshold.

Response fields:

- `inserted`
- `updated`
- `skipped`
- `embedded`
- `source`

Behavior:

- successful completion bumps cache epoch

## POST /teach

Stores user-provided database knowledge.

Request body:

```json
{
  "instruction_type": "term_mapping",
  "content": "counselor means employee",
  "tables_affected": ["employee"],
  "source_query": "show counselors with unpaid invoices"
}
```

Response fields:

- `learning_status`
- `message`
- `instruction_id`
- `similar_instructions`
- `requires_confirmation`
- `confirmation_token`

HTTP behavior:

- HTTP `200` for controlled outcomes including saved, similar, conflict,
  confirmed, rejected, and controlled learning failures
- HTTP `503` only when the DB pool is unavailable

Mutation behavior:

- when the teach action changes effective knowledge, the service bumps cache
  epoch and clears in-memory caches

## POST /teach/confirm

Resolves a pending teach conflict.

Request body:

```json
{
  "confirmation_token": "TOKEN",
  "action": "replace"
}
```

`action` values:

- `confirm`
- `replace`
- `reject`

Response shape matches `/teach`.

HTTP behavior:

- HTTP `200` for controlled outcomes
- HTTP `503` only when the DB pool is unavailable

## GET /instructions

Lists saved instructions for review.

Query params:

- `instruction_type`
- `active_only`

Response:

- array of instruction objects

## POST /generate-sql

Generates SQL without execution.

Request body:

```json
{
  "query": "show unpaid invoices by counselor",
  "top_k": 5,
  "request_id": "optional-request-id"
}
```

Success response fields:

- `status: "ok"`
- `sql`
- `warnings`
- `tables_used`
- `matched_groups`
- `attempt_count`
- `cache_hit`
- `cache_source`
- `react_trace`

Clarification response fields:

- `status: "clarification_needed"`
- `question`
- `suggestions`
- `original_query`
- `failure_reason`
- `cache_hit`
- `cache_source`
- `react_trace`

Rejected response fields:

- `status: "rejected"`
- `sql`
- `warnings`
- `attempt_count`
- `cache_hit`
- `cache_source`
- `react_trace`

Cache lookup order:

1. memory exact
2. memory semantic
3. DB exact for current epoch
4. DB semantic for current epoch
5. full generation pipeline

## POST /ask

Runs generate -> execute -> answer.

Request body:

```json
{
  "query": "newest payment",
  "top_k": 5,
  "request_id": "optional-request-id"
}
```

Success response fields:

- `status: "ok"`
- `answer`
- `sql`
- `warnings`
- `row_count`
- `columns`
- `tables_used`
- `matched_groups`
- `attempt_count`
- `cache_hit`
- `cache_source`
- `react_trace`

Clarification and rejected responses mirror `/generate-sql` status patterns.

Important notes:

- execution is capped to 50 rows
- successful `status="ok"` answers are cached even when `row_count = 0`
- learned pattern saving remains restricted to successful non-empty result sets

## POST /ask/stream

Streaming form of `/ask`.

Content type:

- `application/x-ndjson`

Event names:

- `started`
- `sql_generation_started`
- `sql_generation_running`
- `sql_generation_finished`
- `sql_generation_rejected`
- `row_cap_applied`
- `execution_started`
- `execution_finished`
- `execution_failed`
- `answer_generation_started`
- `answer_generation_running`
- `answer_generation_finished`
- `answer_generation_failed`
- `final`

## Example Cache-Aware Success

```json
{
  "status": "ok",
  "sql": "SELECT id FROM invoice WHERE status = 'unpaid' LIMIT 5",
  "warnings": [],
  "tables_used": ["invoice"],
  "matched_groups": ["billing"],
  "attempt_count": 1,
  "cache_hit": true,
  "cache_source": "db_exact",
  "react_trace": null
}
```

## Current Defaults

From `nl2sql_service/config.py`:

```env
TOP_K=5
SQL_GENERATION_TIMEOUT=90
ASK_TIMEOUT=105
EMBED_CACHE_TTL_SECONDS=3600
SQL_CACHE_TTL_SECONDS=3600
ASK_CACHE_TTL_SECONDS=300
SQL_CACHE_ENABLED=true
ASK_CACHE_ENABLED=true
ASK_CACHE_SEMANTIC_THRESHOLD=0.97
SQL_CACHE_SEMANTIC_THRESHOLD=0.96
```
