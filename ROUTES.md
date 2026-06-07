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

- `GET /help`
- `GET /help/{module}`
- `GET /help/{module}/{route_slug}`
- `GET /health`
- `GET /health/config`
- `GET /health/runtime`
- `GET /health/llm`
- `GET /health/vector`
- `GET /config/model-routing`
- `PATCH /config/model-routing`
- `GET /config/ask-model`
- `PATCH /config/ask-model`
- `PATCH /config/active-model/{role}`
- `GET /providers`
- `POST /providers`
- `GET /providers/{id}`
- `PATCH /providers/{id}`
- `DELETE /providers/{id}`
- `POST /providers/{id}/test`
- `GET /providers/{id}/models`
- `POST /providers/{id}/keys`
- `GET /providers/{id}/keys`
- `DELETE /providers/{id}/keys/{key_id}`
- `GET /model-registry`
- `POST /model-registry`
- `PATCH /model-registry/{id}`
- `DELETE /model-registry/{id}`
- `POST /model-registry/{id}/set-default`
- `GET /model-registry/default`
- `GET /metrics/llm`
- `GET /metrics/teach`
- `GET /logs/days`
- `GET /logs/recent`
- `GET /logs/stream`
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

- `GET /ingest/groups/status`
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
- `GET /teach/pending`
- `POST /teach/pending/cleanup`
- `GET /instructions`
- `DELETE /instructions/{instruction_id}`
- `POST /patterns/feedback`

### Generation

- `POST /generate-sql`
- `POST /ask`
- `POST /ask/stream`

## GET /help

Renders the in-app HTML help index.

Related hidden routes:

- `/help/{module}`
- `/help/{module}/{route_slug}`

These are operator-facing documentation pages and are intentionally excluded
from the OpenAPI schema.

## GET /health/llm

Checks configured LLM connectivity for one workload role.

Query parameters:

- `role` - one of `sql`, `reasoning`, `query_rewrite`, `answer`, `default`

Behavior:

- resolves the same provider/model/fallback chain used by that workload
- performs a short generation probe
- returns provider, model, status, latency, and provider error details
- `provider_config.status="ok"` means the role config is valid
- top-level `status="unavailable"` means the live probe for that role did not succeed

Example:

```bash
curl -s 'http://localhost:8080/health/llm?role=sql' | python -m json.tool
```

## GET /health

Returns compact service and dependency readiness.

Response fields include:

- `status`
- `db`
- `provider_config.status`
- `provider_config.issue_count`
- `mysql_target.status`
- `mysql_target.issue_count`
- `schema_assets.status`
- `schema_assets.issue_count`
- `teach_confirmations.status`
- `teach_confirmations.alerts`

Status behavior:

- escalates to `warning` when teach confirmation alerts fire
- escalates to `error` when provider config, MySQL readiness, or schema assets are not ready

## GET /health/config

Returns the resolved provider configuration readiness report.

Response fields include:

- `status`
- `issues`
- `targets`

## GET /config/model-routing

Returns the active live routing snapshot for the current process.

Response fields include:

- `llm`
- `sql`
- `reasoning`
- `query_rewrite`
- `answer`
- `embedding`
- `startup_enforcement_mode`
- `provider_readiness`

## GET /config/ask-model

Returns the live model assignment used by `/ask` and `/ask/stream` for final answer generation.

Response fields include:

- `provider`
- `model`
- `base_url`
- `api_key_configured`
- `fallback_provider`
- `fallback_model`
- `fallback_base_url`
- `fallback_api_key_configured`

## PATCH /config/model-routing

Patches live task-to-model routing in the current process.

Request body fields are optional and mirror the runtime routing settings.

Behavior:

- invalid provider combinations are rejected with HTTP `422`
- successful updates apply immediately to the current process
- changes are not persisted across process restarts
- edit `.env` when you want different startup defaults after a restart

## PATCH /config/ask-model

Patches only the live model assignment used by `/ask` and `/ask/stream`.

Request body fields:

- `provider`
- `model`
- `api_key`
- `base_url`
- `fallback_provider`
- `fallback_model`
- `fallback_api_key`
- `fallback_base_url`

Behavior:

- invalid provider combinations are rejected with HTTP `422`
- successful updates apply immediately to the current process
- changes are not persisted across process restarts
- edit `.env` when you want different startup defaults after a restart

## PATCH /config/active-model/{role}

Persists the default model for a role in the DB-backed model registry and
patches the live process routing to match immediately.

Request body:

- `model_id`

Behavior:

- validates that the selected model exists
- validates that the selected model matches the path role
- updates the DB default for that role
- updates live `settings` so the current process uses that provider/model
- persists across restart because the DB registry becomes the source of truth

## Provider Management

Provider records:

- `GET /providers`
- `POST /providers`
- `GET /providers/{id}`
- `PATCH /providers/{id}`
- `DELETE /providers/{id}`

Provider probe and discovery:

- `POST /providers/{id}/test`
- `GET /providers/{id}/models`

Provider key management:

- `POST /providers/{id}/keys`
- `GET /providers/{id}/keys`
- `DELETE /providers/{id}/keys/{key_id}`

Security behavior:

- raw API keys are encrypted with AES-256-GCM before storage
- responses include only `key_label` plus `key_prefix`
- if `PROVIDER_KEY_ENCRYPTION_SECRET` is unset, key creation returns HTTP `503`

## Model Registry

- `GET /model-registry`
- `POST /model-registry`
- `PATCH /model-registry/{id}`
- `DELETE /model-registry/{id}`
- `POST /model-registry/{id}/set-default`
- `GET /model-registry/default`

Behavior:

- only one active default per role is allowed
- setting a new default unsets the old default for that role
- provider deactivation also deactivates its registered models

## GET /health/runtime

Returns detailed runtime readiness for SQL execution and local schema/docs assets.

Response fields include:

- `status`
- `mysql_target`
- `schema_assets`

Operational note:

- `STARTUP_ENFORCEMENT_MODE=strict` uses the same provider/runtime readiness
  checks during startup and aborts boot when they are not ready
- `make smoke-deploy` verifies `/health`, `/health/config`, and `/health/runtime`
  all return `status=ok` before treating the service as deploy-ready

## GET /health/vector

Checks pgvector database connectivity and reports vector/embedding config.

Response fields include:

- `status`
- `vector_db`
- `db`
- `embedding_provider`
- `embedding_model`
- `embedding_dimension`

## GET /metrics/llm

Returns in-memory provider usage counters captured by the LLM abstraction.

Each result includes:

- `role`
- `provider`
- `model`
- `requests`
- `failures`
- `prompt_tokens`
- `completion_tokens`
- `total_tokens`
- `total_latency_ms`
- `avg_latency_ms`
- `estimated_cost_usd`
- `retries`

## GET /metrics/teach

Returns operational counts for pending teach confirmations.

Response fields:

- `pending_active_count`
- `pending_expired_count`
- `oldest_pending_created_at`
- `next_pending_expiry_at`
- `status`
- `alerts`
- `thresholds`

Alert behavior:

- warns when expired pending confirmations meet or exceed `TEACH_PENDING_EXPIRED_WARN_THRESHOLD`
- warns when active pending confirmations meet or exceed `TEACH_PENDING_ACTIVE_WARN_THRESHOLD`

## GET /logs/days

Lists the active repo-local log file plus rotated daily log files.

Response fields:

- `log_dir`
- `results`

Each item in `results` includes:

- `day`
- `file`
- `path`
- `size_bytes`
- `modified_at`
- `is_active`

## GET /logs/recent

Returns the most recent lines from the selected repo-local log file.

Query params:

- `day` - `current` or `YYYY-MM-DD`
- `lines` - integer, default `200`

Response fields:

- `day`
- `file`
- `path`
- `lines`
- `total_lines_returned`

Notes:

- reads from `logs/nl2sql.log` when `day=current`
- reads from rotated files like `logs/nl2sql.log.2026-06-02` for older days
- returns raw JSON log lines as strings

## GET /logs/stream

Streams repo-local log lines as NDJSON.

Query params:

- `day` - `current` or `YYYY-MM-DD`
- `backlog` - integer, default `100`
- `follow` - boolean, default `true`
- `poll_interval_ms` - integer, default `1000`

Behavior:

- sends backlog lines first as `{"event":"log_line",...}` records
- when `day=current`, follows new lines written to the active log file
- when an older day is selected, the stream sends the requested backlog and ends
- output media type is `application/x-ndjson`

## GET /telemetry/recent

Returns recent request telemetry events for quick debugging.

Query params:

- `limit`
- `endpoint`

Response fields:

- `results`

## GET /telemetry/summary

Returns aggregate request KPIs over a time window.

Query params:

- `endpoint`
- `since_minutes`

Response fields include:

- `total_requests`
- `ok_count`
- `clarification_count`
- `rejected_count`
- `p50_latency_ms`
- `p95_latency_ms`
- `error_sources`

## GET /telemetry/trace/{request_id}

Returns ordered trace events for one request.

Query params:

- `limit`

Response fields:

- `request_id`
- `results`
- `total`

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

Behavior:

- when the PostgreSQL pool is available, also clears DB-backed query cache rows

## GET /governance/rules

Returns all governance rules plus enabled status.

Response fields include:

- `total_rules`
- `enabled_rules`
- `governance_enabled`
- `rules`

## POST /governance/validate

Runs the standalone SQL review gate against caller-supplied SQL.

Request body fields:

- `sql`
- `query`
- `tables_in_scope`

Response fields:

- `passes`
- `violations`
- `sql`
- `query`

## POST /benchmark/cases

Stores a benchmark case for replay/regression runs.

Response fields:

- `id`
- `query`
- `expected_status`

## GET /benchmark/cases

Lists stored benchmark cases.

Query params:

- `limit`
- `active_only`

Response fields:

- `results`

## GET /ingest/groups/status

Returns embedded-vs-current schema version status for each schema group.

Response fields:

- `groups`
- `current_count`
- `stale_count`
- `never_embedded_count`

## POST /ingest

Ingests either free text or explicit schema table payloads.

Response fields:

- `inserted`
- `updated`
- `source`

## POST /query

Runs raw retrieval against the main embeddings corpus.

Response fields:

- `results`

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

Pending confirmation tokens are stored in PostgreSQL, so they survive service
restarts until their 30-minute TTL expires.

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

## GET /teach/pending

Lists pending teach confirmations for operational or admin review.

Query params:

- `limit` — integer, default `100`, max `500`
- `include_expired` — boolean, default `false`

Response fields:

- `results`
- `stats`

Each result includes:

- `token`
- `instruction_type`
- `content`
- `tables_affected`
- `source_query`
- `conflicting_id`
- `created_at`
- `expires_at`
- `is_expired`

## POST /teach/pending/cleanup

Deletes expired pending teach confirmations immediately.

Response fields:

- `deleted`
- `stats`

## GET /instructions

Lists saved instructions for review.

Query params:

- `instruction_type`
- `active_only`

Response:

- array of instruction objects

## DELETE /instructions/{instruction_id}

Deactivates one saved instruction and marks its embedded chunk inactive.

Response fields:

- `deactivated`
- `instruction_id`

## POST /patterns/feedback

Applies downstream feedback to a learned pattern.

Request body fields:

- `pattern_id`
- `helpful`

Behavior:

- `helpful=true` boosts `use_count`
- `helpful=false` deactivates the pattern

## POST /query/groups

Runs schema-group retrieval and returns a ready-to-use context block.

Response fields include:

- `matched_groups`
- `tables_in_scope`
- `context`
- `results`

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
- deterministic single-table queries proven by column similarity and retrieved
  schema context bypass the answer model and return a direct fallback answer
- non-deterministic queries still use the configured answer model for the
  final natural-language response
- ReAct SQL generation now focuses broad schema scope down to the top
  `SQL_GENERATION_MAX_TABLES` tables by column similarity before calling the
  SQL model
- if scope exceeds that cap and no column-level schema has been refreshed yet,
  the planner inserts a focused `RETRIEVE_SCHEMA_FOR_TABLES` step before
  `GENERATE_SQL`

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

Operational note:

- deterministic single-table queries skip the answer LLM and usually complete
  much faster than non-deterministic asks
- if answer generation is still slow, inspect `/config/ask-model` and the
  frontend proxy timeout

Deterministic candidate logic is schema-driven:

- top column-similarity hits must all belong to the same table
- any foreign-table hit forces the ReAct path
- the candidate table from the top hit must also be confirmed by retrieved
  schema context, or the service emits a warning trace event and skips the
  deterministic fast path

ReAct SQL generation focus is also schema-driven:

- when `tables_in_scope` is broader than `SQL_GENERATION_MAX_TABLES`, the
  planner ranks in-scope tables by column similarity against the query
- before `GENERATE_SQL`, the planner emits a `sql_context_focus` trace event
  with the table count before focus, the focused table list, and the number of
  column hits used
- if no column-level schema has been refreshed yet, the planner forces a
  targeted schema refresh on those focused tables before allowing SQL
  generation

## Documentation Maintenance

Whenever a major or important runtime change is made, update `README.md` and
`ROUTES.md` in the same change. If the production/runtime contract changes,
update the operator-facing architecture docs as well.

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
LLM_PROVIDER=ollama
LLM_MODEL=deepseek-coder:6.7b
LLM_TIMEOUT=60
LLM_MAX_RETRIES=2

EMBEDDING_PROVIDER=custom
EMBEDDING_API_URL=http://<embedding-host>:8000/embed
EMBEDDING_MODEL=bge-large-en-v1.5
EMBEDDING_DIMENSION=1024

VECTOR_PROVIDER=pgvector
VECTOR_HNSW_EF_SEARCH=40
TOP_K=5
REACT_MAX_ITERATIONS=2
SQL_GENERATION_MAX_TABLES=5
SQL_GENERATION_TIMEOUT=90
ASK_TIMEOUT=105
EMBED_CACHE_TTL_SECONDS=3600
SQL_CACHE_TTL_SECONDS=3600
ASK_CACHE_TTL_SECONDS=300
SQL_CACHE_ENABLED=true
ASK_CACHE_ENABLED=true
CACHE_SEMANTIC_THRESHOLD_ASK=0.92
SQL_CACHE_SEMANTIC_THRESHOLD=0.96
PROVIDER_KEY_ENCRYPTION_SECRET=<32+ char secret>
```

Provider/model routing is environment-driven at startup. Role-specific
`SQL_*`, `REASONING_*`, `QUERY_REWRITE_*`, and `ANSWER_*` settings override the
global `LLM_*` defaults for those workloads. `PATCH /config/model-routing` and
`PATCH /config/ask-model` override the running process only and do not persist
across restart. `PATCH /config/active-model/{role}` persists the selected role
default in the DB-backed model registry and also patches the live process.
`QUERY_REWRITE_FAST_MODEL` must contain a model name, not a
base URL, and is used when `QUERY_REWRITE_MODEL` is unset. `EMBEDDING_DIMENSION`
must match the embedding model; `bge-small-en-v1.5` uses `384` dimensions and
is a lower latency alternative to the default `bge-large-en-v1.5`
`1024`-dimension model.
