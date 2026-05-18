# NL2SQL API Routes Guide

This document is a route-by-route reference for the FastAPI service in this repository.

## Change Log

- 2026-05-11: Added governance rulebook injection, advisory SQL review gate, `GET /governance/rules`, `POST /governance/validate`, `REVIEW_FAILED` warnings, and telemetry summary visibility for review-gate hits.
- 2026-05-09: Added in-memory embed/SQL caches, `GET /cache/stats`, `POST /cache/clear`, `cache_hit` SQL generation fields, structured answer generation, and `ANSWER_HALLUCINATION` warnings.
- 2026-05-06: Added public browser route help at `/help`, `/help/{module}`, and `/help/{module}/{route_slug}`.
- 2026-05-06: Added terminal route help browser via `python -m nl2sql_service.help_tui`; it reuses the same OpenAPI-backed help registry as `/help`.
- 2026-05-02: Added fail-open DeepSeek/Ollama query rewriting before embedding for `/query`, `/query/groups`, `/generate-sql`, `/ask`, and `/ask/stream`.
- 2026-05-02: `POST /ingest/groups` now returns HTTP 200 partial instead of HTTP 500 when groups exceed the 400-token limit. Response extended with `partial`, `failure_count`, and `failed_groups` fields.
- 2026-05-02: Added answer-style config knobs (`ANSWER_STRICT_CONCISE`, `ANSWER_MAX_WORDS`, `ANSWER_MAX_TOKENS`, `ANSWER_ALLOW_REASONING`) that influence `/ask` and `/ask/stream` answer generation behaviour.
- 2026-05-02: Added full-route smoke matrix script `scripts/nl2sql_smoke_test.py`; it now covers API routes, browser help routes, and terminal help checks.
- 2026-04-30: Extended `scripts/nl2sql_replay_benchmark.py` with `--output FILE` (JSON/CSV report exporter) and `--fail-on-slices SLICES` (CI gate mode — exit non-zero only when named slices regress).
- 2026-04-29: Added telemetry KPI endpoint `GET /telemetry/summary` and benchmark replay script support.
- 2026-04-29: Added ops endpoints for telemetry inspection and benchmark-case management: `GET /telemetry/recent`, `POST /benchmark/cases`, `GET /benchmark/cases`.
- 2026-04-29: Added request-level telemetry persistence (`nl2sql_request_events`) and optional `request_id` on `/generate-sql`, `/ask`, and `/ask/stream`.
- 2026-04-29: Added interactive user-instruction learning routes, instruction review/soft delete, manual instruction embedding, prompt injection, and confidence outcome tracking.
- 2026-04-28: Added learned-pattern storage, retrieval injection, manual pattern embedding, and pattern feedback routes.
- 2026-04-28: Added `clarification_needed` response for ReAct logic failures after the full loop runs.
- 2026-04-26: Added `POST /ask` route contract.
- 2026-04-26: Documented `/ask` bounded execution rule (max 50 rows), rejection behavior, and warning surfaces.

Base URL examples below assume:
- http://localhost:8080

## Quick Route List

Documentation/help routes:

- GET /help
- GET /help/{module}
- GET /help/{module}/{route_slug}

API routes:

- GET /health
- GET /telemetry/recent
- GET /telemetry/summary
- GET /cache/stats
- GET /governance/rules
- POST /cache/clear
- POST /governance/validate
- POST /benchmark/cases
- GET /benchmark/cases
- POST /ingest
- POST /query
- POST /ingest/groups
- POST /ingest/knowledge
- POST /ingest/patterns
- POST /ingest/instructions
- POST /query/groups
- POST /patterns/feedback
- POST /teach
- POST /teach/confirm
- GET /instructions
- DELETE /instructions/{instruction_id}
- POST /generate-sql
- POST /ask
- POST /ask/stream

## Common Behavior Across Routes

- Content type for POST routes: application/json
- `/help` routes are public, DB-free, and do not require PostgreSQL, MySQL, embedding services, or Ollama
- `/help` routes are browser-oriented HTML pages backed by FastAPI OpenAPI metadata plus curated documentation in `nl2sql_service/help_docs.py`
- The terminal help browser (`python -m nl2sql_service.help_tui`) uses the same documentation source as `/help`
- `/governance/rules` and `/governance/validate` are public read-only ops routes when governance is enabled
- When `GOVERNANCE_ENABLED=false`, `/governance/*` returns HTTP 503 with `Governance disabled`
- If database is unavailable, DB-backed routes return HTTP 503
- Embedding upstream timeout or upstream failure returns HTTP 502 for embedding-backed ingest/retrieval routes
- Query rewrite timeout, upstream failure, or malformed response falls back to the original query and does not change the public response shape
- Query rewrite is skipped entirely for queries with 3 or fewer words
- Retrieval-time query embeddings can be served from the in-memory embed cache; ingest embeddings are not cached
- `/generate-sql` can return cached successful responses with `cache_hit: true`; rejected and clarification responses are not cached
- When `GOVERNANCE_ENABLED=true`, prompt-level governance rules are injected into ReAct planning, SQL generation, and answer generation according to the `GOVERNANCE_INJECT_*` flags
- When `GOVERNANCE_ENABLED=true`, accepted SQL passes through a second advisory review gate after static validators and MySQL EXPLAIN; review failures add `REVIEW_FAILED` warnings but do not change `status`
- `/generate-sql` returns HTTP 200 with `status: "rejected"` only for Ollama transport/malformed failures where the model could not run
- `/generate-sql` returns HTTP 200 with `status: "clarification_needed"` for ReAct logic failures after the loop runs, including `GIVE_UP` and iteration exhaustion
- `/generate-sql` does not execute SQL; it only returns generated SQL, clarification, or transport rejection warnings
- `/ask` returns HTTP 200 with `status: "clarification_needed"` when SQL generation needs user clarification and does not execute SQL in that case
- `/ask` returns HTTP 200 with `status: "rejected"` for transport failures or SQL-execution failures; answer-model failures return a compact fallback answer when SQL execution succeeded
- `/ask` answer generation uses a structured `ANSWER` / `KEY FIGURES` / `DETAILS` template and may add non-blocking `ANSWER_HALLUCINATION` warnings when answer numbers are not present in returned rows
- `/ask` executes SQL only after `/generate-sql` succeeds, with a hard execution cap of 50 rows
- When `/ask` succeeds with `row_count > 0`, it saves a learned SQL pattern in the background without delaying the response
- When generation succeeds or fails after a ReAct path, matching user-instruction counters are updated in the background
- `/ask/stream` runs the same ask workflow as `/ask`, but returns progress as newline-delimited JSON events
- `/generate-sql`, `/ask`, and `/ask/stream` accept optional `request_id`; if omitted, the service generates one
- Request outcomes and stage timings are persisted in `nl2sql_request_events` for evaluation and replay workflows
- Invalid request body shape returns HTTP 422
- Versioned ingestion routes (`/ingest/groups`, `/ingest/knowledge`, `/ingest/patterns`, `/ingest/instructions`) pre-check existing `schema_version` and skip unchanged chunks before embedding
- `/teach` returns HTTP 200 for saved, similar, conflict, confirmed, rejected, and controlled learning-failure responses
- `/teach` returns HTTP 503 only when the DB pool is unavailable

## Storage Tables

- `nl2sql_embeddings`: pgvector chunks for text, schema groups, knowledge, manually embedded learned patterns, and manually embedded user instructions.
- `nl2sql_learned_patterns`: successful `/ask` examples with query text, SQL used, tables used, extracted join conditions, matched groups, use count, timestamps, and active/deactivated state.
- `nl2sql_user_instructions`: user-provided table relationships, business rules, query methodology, term mappings, filter rules, corrections, confidence scores, verification flags, conflict links, and outcome counters.
- `nl2sql_request_events`: request-level telemetry with endpoint, status, latency, stage timings, warning codes, error-source classification, and `metadata.review_failed` for governance review tracking.
- `nl2sql_benchmark_cases`: replay benchmark cases with expected status, optional gold SQL, slices, and labels.

---

## GET /help

What this route does:
- Renders a browser-friendly documentation hub for all documented service routes.
- Lists modules, methods, paths, summaries, required inputs, and links to detail pages.
- Includes client-side search/filter by path, method, module, title, summary, and related route text.

Data source:
- FastAPI OpenAPI route metadata from the running app.
- Curated route descriptions, examples, error cases, auth notes, and related-route links from `nl2sql_service/help_docs.py`.

Runtime requirements:
- Public route.
- Does not require PostgreSQL, MySQL, embedding service, Ollama, or external APIs.

How to use:
```bash
curl -s http://localhost:8080/help
```

Browser URL:
```text
http://localhost:8080/help
```

---

## GET /help/{module}

What this route does:
- Renders a module-specific help page.
- Supported modules:
  - `ops`
  - `ingestion`
  - `retrieval`
  - `learning`
  - `generation`

Path parameters:
- `module`: required module name.

Runtime requirements:
- Public route.
- DB-free and safe to use while the service is degraded.

How to use:
```bash
curl -s http://localhost:8080/help/generation
```

Browser URL:
```text
http://localhost:8080/help/generation
```

---

## GET /help/{module}/{route_slug}

What this route does:
- Renders detailed route documentation for one endpoint.
- Shows method, path, purpose, parameters, body example, response example, error cases, auth notes, curl command, expected return format, and related routes.

Path parameters:
- `module`: required module name.
- `route_slug`: required stable route slug, for example `ask`, `ask-stream`, `generate-sql`, `ingest-groups`, or `instructions-delete`.

Runtime requirements:
- Public route.
- DB-free and safe to use while the service is degraded.

How to use:
```bash
curl -s http://localhost:8080/help/generation/ask
```

Browser URL:
```text
http://localhost:8080/help/generation/ask
```

---

## Terminal Help Browser

What this tool does:
- Provides a developer-focused terminal browser for the same documentation used by `/help`.
- Supports interactive keyboard navigation when a capable TTY is available.
- Falls back to plain text output for non-interactive terminals and scripted usage.

Entry points:
```bash
python -m nl2sql_service.help_tui
python scripts/help_tui.py
```

Non-interactive examples:
```bash
python -m nl2sql_service.help_tui --plain
python -m nl2sql_service.help_tui --module generation --plain
python -m nl2sql_service.help_tui --route generation/ask --plain
python -m nl2sql_service.help_tui --search sql --plain
```

Interactive keys:
- `↑` / `↓` or `j` / `k`: move selection or scroll details
- `Enter`: open selected route detail
- `/`: search
- `1`-`5`: switch modules
- `a`: show all routes
- `b`: back/reset
- `q`: quit

Runtime requirements:
- Uses only the Python standard library for terminal rendering.
- Does not require the service server to be running.
- Does not require PostgreSQL, MySQL, embedding service, Ollama, or external APIs.

---

## GET /health

What this route does:
- Returns service liveness and current DB connectivity status.

Request parameters:
- None

Response body:
- status: string
- db: string

Example response:
```json
{
  "status": "ok",
  "db": "connected"
}
```

How to use:
```bash
curl -s http://localhost:8080/health | python3 -m json.tool
```

---

## GET /telemetry/recent

What this route does:
- Returns recent request telemetry events captured in `nl2sql_request_events`.

Query parameters:
- `limit`: integer, default `50`, capped at `500`
- `endpoint`: optional endpoint filter, for example `/ask` or `/generate-sql`

Response body:
- `results`: array of telemetry rows ordered by `created_at` descending

How to use:
```bash
curl -s 'http://localhost:8080/telemetry/recent?limit=20&endpoint=/ask' \
  | python3 -m json.tool
```

---

## GET /telemetry/summary

What this route does:
- Returns aggregate KPIs from `nl2sql_request_events` for monitoring and release gates.

Query parameters:
- `endpoint`: optional endpoint filter, for example `/ask`
- `since_minutes`: time window in minutes, default `1440` (last 24h)

Response fields:
- `total_requests`
- `ok_count`, `clarification_count`, `rejected_count`
- `ok_rate`, `clarification_rate`, `rejected_rate`
- `review_failed_count`, `review_failed_rate`
- `avg_latency_ms`, `p50_latency_ms`, `p95_latency_ms`
- `error_sources` (count grouped by `error_source`)

How to use:
```bash
curl -s 'http://localhost:8080/telemetry/summary?endpoint=/ask&since_minutes=60' \
  | python3 -m json.tool
```

---

## GET /governance/rules

What this route does:
- Returns the current governance rulebook seen by the service.
- Shows all known rule identifiers plus whether each rule is currently enabled.
- Useful for debugging prompt behavior and confirming env-driven rule toggles.

Runtime requirements:
- Public route.
- Does not require PostgreSQL.
- Returns HTTP 503 with `Governance disabled` when `GOVERNANCE_ENABLED=false`.

Response fields:
- `total_rules`
- `enabled_rules`
- `governance_enabled`
- `rules`: array of rule objects with `name`, `category`, `severity`, `enabled`, and `description`

How to use:
```bash
curl -s http://localhost:8080/governance/rules | python3 -m json.tool
```

---

## POST /governance/validate

What this route does:
- Runs the advisory governance SQL reviewer without executing the full `/generate-sql` pipeline.
- Lets you test whether the review gate would flag a given SQL/query pair.
- Does not execute SQL and does not write to PostgreSQL telemetry tables.

Body fields:
- `sql`: string (required)
- `query`: string (required)
- `tables_in_scope`: array of strings (optional, default `[]`)

Runtime requirements:
- Public route.
- Does not require PostgreSQL.
- Best-effort live column loading uses app MySQL credentials when `tables_in_scope` is provided; if MySQL is unavailable, review still runs with an empty known-column set.
- Returns HTTP 503 with `Governance disabled` when `GOVERNANCE_ENABLED=false`.

Response fields:
- `passes`: boolean
- `violations`: array of strings
- `sql`: string
- `query`: string

How to use:
```bash
curl -s -X POST http://localhost:8080/governance/validate \
  -H "Content-Type: application/json" \
  -d '{"sql":"SELECT id FROM invoice WHERE status='\''unpaid'\''","query":"show unpaid invoices","tables_in_scope":["invoice"]}' \
  | python3 -m json.tool
```

---

## GET /cache/stats

What this route does:
- Returns current in-memory cache sizes and TTL settings.
- Does not require PostgreSQL, MySQL, embedding service, or Ollama.

Response fields:
- `embed_cache_size`: number of cached retrieval embeddings
- `sql_cache_size`: number of cached successful SQL generation responses
- `embed_cache_ttl_seconds`: current embed cache TTL
- `sql_cache_ttl_seconds`: current SQL cache TTL

Example response:
```json
{
  "embed_cache_size": 12,
  "sql_cache_size": 4,
  "embed_cache_ttl_seconds": 1800,
  "sql_cache_ttl_seconds": 300
}
```

How to use:
```bash
curl -s http://localhost:8080/cache/stats | python3 -m json.tool
```

---

## POST /cache/clear

What this route does:
- Clears both in-memory caches.
- Intended for ops use after schema changes, re-ingest, or troubleshooting stale SQL generation.
- Does not require PostgreSQL, MySQL, embedding service, or Ollama.

Response fields:
- `embed_cleared`: number of embedding cache entries removed
- `sql_cleared`: number of SQL cache entries removed

Example response:
```json
{
  "embed_cleared": 12,
  "sql_cleared": 4
}
```

How to use:
```bash
curl -s -X POST http://localhost:8080/cache/clear | python3 -m json.tool
```

---

## POST /benchmark/cases

What this route does:
- Adds a benchmark case for replay and regression gates.

Body fields:
- `query`: string (required)
- `gold_sql`: string or null (optional)
- `expected_status`: one of `ok`, `clarification_needed`, `rejected` (optional, default `ok`)
- `slices`: array of strings (optional)
- `error_label`: string or null (optional)
- `source`: string (optional, default `manual`)
- `metadata`: object (optional)

Example body:
```json
{
  "query": "show unpaid invoices by counselor",
  "gold_sql": "SELECT id FROM invoice WHERE status='unpaid'",
  "expected_status": "ok",
  "slices": ["joins", "aggregation"],
  "source": "manual"
}
```

How to use:
```bash
curl -s -X POST http://localhost:8080/benchmark/cases \
  -H "Content-Type: application/json" \
  -d '{"query":"newest payment","expected_status":"ok","slices":["single_table"]}' \
  | python3 -m json.tool
```

---

## GET /benchmark/cases

What this route does:
- Lists stored benchmark cases ordered by newest first.

Query parameters:
- `limit`: integer, default `100`, capped at `1000`
- `active_only`: boolean, default `true`

How to use:
```bash
curl -s 'http://localhost:8080/benchmark/cases?limit=50&active_only=true' \
  | python3 -m json.tool
```

---

## POST /ingest

What this route does:
- Ingests content into embeddings storage.
- Supports two modes via the type field:
  - text mode: free text chunking + embedding
  - schema mode: one embedding per provided schema table record

### Mode A: text ingest

Required body fields:
- type: must be "text"
- source: string label used for traceability
- text: input text to chunk and embed

Example body:
```json
{
  "type": "text",
  "source": "docs:faq",
  "text": "Long business documentation text..."
}
```

### Mode B: schema ingest

Required body fields:
- type: must be "schema"
- source: string label used for traceability
- tables: array of schema table objects

SchemaTable object fields:
- database: string (required)
- object_name: string (required)
- object_type: string (optional, default: "table")
- full_object_name: string (required)
- text: string (required)
- chunk_index: integer (optional, default: 1)
- total_chunks: integer (optional, default: 1)
- column_count: integer or null (optional)
- source_kind: string (optional, default: "schema_export")

Example body:
```json
{
  "type": "schema",
  "source": "schema_export_2026_04_24",
  "tables": [
    {
      "database": "app_db",
      "object_name": "invoice",
      "object_type": "table",
      "full_object_name": "app_db.invoice",
      "text": "Table invoice columns: id, amount, status, created_at",
      "chunk_index": 1,
      "total_chunks": 1,
      "column_count": 4,
      "source_kind": "schema_export"
    }
  ]
}
```

Response body:
- inserted: integer
- updated: integer
- source: string

How to use:
```bash
curl -s -X POST http://localhost:8080/ingest \
  -H "Content-Type: application/json" \
  -d '{"type":"text","source":"notes","text":"sample text"}' \
  | python3 -m json.tool
```

---

## POST /query

What this route does:
- Rewrites the input query for retrieval when `QUERY_REWRITE_ENABLED=true`.
- Skips rewrite for queries with 3 or fewer words.
- Embeds the rewritten search text, falling back to the input query when rewriting fails.
- Uses the in-memory embed cache when `EMBED_CACHE_ENABLED=true`.
- Performs cosine-similarity retrieval across all chunk types.
- Returns top matching chunks with metadata.
- Returns similarity scores and metadata, not raw embedding vectors.

Body fields:
- query: string (required)
- top_k: integer or null (optional)
- request_id: string or null (optional, caller correlation id)

Notes:
- If top_k is omitted or null, server default TOP_K is used.

Example body:
```json
{
  "query": "show unpaid invoices by counselor",
  "top_k": 5
}
```

Response body:
- results: array

Each result item:
- content: string
- similarity: number
- metadata: object

How to use:
```bash
curl -s -X POST http://localhost:8080/query \
  -H "Content-Type: application/json" \
  -d '{"query":"SQL select from invoice_payment_view","top_k":10}' \
  | python3 -m json.tool
```

---

## POST /ingest/groups

What this route does:
- Builds and embeds schema-group chunks from rag_schema entities.
- Upserts by source/chunk index and schema version.
- Supports full ingest or selected group ingest.

Body fields:
- group_names: array of strings or null (optional)

Behavior:
- group_names omitted or null: ingest all groups
- group_names provided: ingest only listed groups

Accepted group name styles:
- full entity ID (example: entity__inquiry_lifecycle)
- short name (example: inquiry_lifecycle)

Example body (all groups):
```json
{}
```

Example body (selected groups):
```json
{
  "group_names": ["inquiry_lifecycle", "sales_invoice_billing"]
}
```

Response body:
- inserted: integer
- updated: integer
- source: string
- partial: boolean — true when one or more groups were skipped due to errors
- failure_count: integer — number of groups that were skipped
- failed_groups: array of objects — each has `group` (string) and `error` (string)
- enrichment_summary: object or null
  - groups_with_columns: integer
  - groups_without_columns: integer
  - groups_with_aliases: integer
  - groups_with_examples: integer

Enrichment behavior:
- Group chunk text is enriched with live MySQL columns, business aliases, and example questions.
- Live columns are loaded using app DB credentials (`DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, and `DB_NAME` or `DB_CENTRAL`).
- If MySQL is unavailable, ingest still succeeds; chunk text includes `(columns unavailable)` and counters reflect this in `groups_without_columns`.
- `schema_version` remains entity-file based; MySQL column drift alone does not auto-bump it.
- Groups that exceed the 400-token ceiling after enrichment are skipped and reported in `failed_groups`; all other groups are still embedded. To fix an oversized group, reduce `example_questions` or `business_aliases` in the entity JSON file.
- The route returns HTTP 200 even when `partial: true`; HTTP 500 is only returned for unexpected infrastructure failures.

How to use:
```bash
curl -s -X POST http://localhost:8080/ingest/groups \
  -H "Content-Type: application/json" \
  -d '{"group_names":["inquiry_lifecycle"]}' \
  | python3 -m json.tool
```

Example response (all groups succeed):
```json
{
  "inserted": 8,
  "updated": 0,
  "source": "all groups",
  "partial": false,
  "failure_count": 0,
  "failed_groups": [],
  "enrichment_summary": {
    "groups_with_columns": 8,
    "groups_without_columns": 0,
    "groups_with_aliases": 6,
    "groups_with_examples": 8
  }
}
```

Example partial response (one group exceeded token limit):
```json
{
  "inserted": 7,
  "updated": 0,
  "source": "all groups",
  "partial": true,
  "failure_count": 1,
  "failed_groups": [
    {
      "group": "entity__inquiry_lifecycle",
      "error": "Group 'entity__inquiry_lifecycle' estimated 789 tokens after enrichment. Exceeds 400 limit."
    }
  ],
  "enrichment_summary": {
    "groups_with_columns": 7,
    "groups_without_columns": 0,
    "groups_with_aliases": 5,
    "groups_with_examples": 7
  }
}
```

---

## POST /ingest/knowledge

What this route does:
- Embeds enriched knowledge from all enabled sources:
  - column catalog chunks (docs JSONL)
  - SQL example chunks (view SQL)
  - relation-link chunks (`rag_schema/relations/*.json`)
  - table-node chunks (`rag_schema/graph/table_graph.json`)
  - view-node chunks (`rag_schema/graph/view_registry.json`)
  - schema-rule chunks (`rag_schema/rules/onboarding_rules.json`)
- Upserts using schema-version-aware logic.

Body fields:
- include_column_catalog: boolean (optional, default true)
- include_sql_examples: boolean (optional, default true)
- include_relations: boolean (optional, default true)
- include_graph: boolean (optional, default true)
- include_view_registry: boolean (optional, default true)
- include_onboarding_rules: boolean (optional, default true)
- column_limit: integer or null (optional)
- sql_example_limit: integer or null (optional, default 200)
- relation_limit: integer or null (optional)
- graph_limit: integer or null (optional)
- view_registry_limit: integer or null (optional)

Chunk types produced:
- column_catalog: column definitions from docs JSONL files
- sql_example: view SQL shapes from mysql_schema_export.txt
- relation_link: join conditions from rag_schema/relations/*.json
- table_node: table classification and cluster from rag_schema/graph/table_graph.json
- view_node: view role and derived tables from rag_schema/graph/view_registry.json
- schema_rule: onboarding/attachment rules from rag_schema/rules/onboarding_rules.json

Behavior examples:
- Defaults: ingest all enabled source types
- include_column_catalog false: skip columns
- include_sql_examples false: skip SQL examples
- include_relations false: skip relation-link chunks
- include_graph false: skip table-node chunks
- include_view_registry false: skip view-node chunks
- include_onboarding_rules false: skip schema-rule chunk
- limits reduce number of chunks embedded for that source type
- unchanged chunks with same `schema_version` are skipped before embedding

Example body (default behavior — all sources):
```json
{}
```

Example body (SQL + relations only):
```json
{
  "include_column_catalog": false,
  "include_sql_examples": true,
  "include_relations": true,
  "include_graph": false,
  "include_view_registry": false,
  "include_onboarding_rules": false
}
```

Response body:
- inserted: integer
- updated: integer
- source: string ("knowledge")

How to use:
```bash
curl -s -X POST http://localhost:8080/ingest/knowledge \
  -H "Content-Type: application/json" \
  -d '{"include_column_catalog":true,"include_sql_examples":true,"include_relations":true,"include_graph":true,"include_view_registry":true,"include_onboarding_rules":true,"column_limit":300,"sql_example_limit":200,"relation_limit":null,"graph_limit":null,"view_registry_limit":null}' \
  | python3 -m json.tool
```

---

## POST /ingest/patterns

What this route does:
- Manually embeds active learned patterns into `nl2sql_embeddings`.
- Reads from `nl2sql_learned_patterns`.
- Includes only active patterns with `use_count >= MIN_PATTERN_USE_COUNT`.
- Stores each embedded pattern as `metadata.type = "learned_pattern"`.
- Upserts by `source = "learned_pattern_{id}"`, `chunk_index = 0`, and content-derived `schema_version`.

Body fields:
- None

Notes:
- This route is manual/cron only. It is not called automatically by `/ask`.
- Recommended cadence: after roughly 10 successful `/ask` calls or on a scheduled job.
- Future live prompting does not require this route; `retrieve_groups()` reads relevant active patterns directly from `nl2sql_learned_patterns`.
- Embedding patterns makes them visible to mixed `/query` retrieval and stores a vector copy for inspection.

Response body:
- embedded: integer, number of inserted or updated pattern chunks
- source: string (`"learned_patterns"`)

How to use:
```bash
curl -s -X POST http://localhost:8080/ingest/patterns \
  -H "Content-Type: application/json" \
  -d '{}' \
  | python3 -m json.tool
```

Example response:
```json
{
  "embedded": 3,
  "source": "learned_patterns"
}
```

---

## POST /ingest/instructions

What this route does:
- Manually embeds active user instructions into `nl2sql_embeddings`.
- Reads from `nl2sql_user_instructions`.
- Includes active instructions with `confidence_score >= MIN_INSTRUCTION_CONFIDENCE`.
- Stores each embedded instruction as `metadata.type = "user_instruction"`.
- Upserts by `source = "user_instruction_{id}"`, `chunk_index = 0`, and content-derived `schema_version`.

Body fields:
- None

Notes:
- This route is manual/cron only.
- `/generate-sql` does not require this route; ReAct prompt injection reads live instructions directly from `nl2sql_user_instructions`.
- Embedding instructions makes them visible to mixed `/query` retrieval and stores a vector copy for inspection.

Response body:
- embedded: integer, number of inserted or updated instruction chunks
- source: string (`"user_instructions"`)

How to use:
```bash
curl -s -X POST http://localhost:8080/ingest/instructions \
  -H "Content-Type: application/json" \
  -d '{}' \
  | python3 -m json.tool
```

Example response:
```json
{
  "embedded": 2,
  "source": "user_instructions"
}
```

---

## POST /patterns/feedback

What this route does:
- Records simple human feedback for a learned pattern.
- Helpful feedback boosts future retrieval priority.
- Unhelpful feedback deactivates the pattern so it is no longer retrieved.

Body fields:
- pattern_id: integer (required)
- helpful: boolean (required)

Behavior:
- helpful true: increments `use_count` by 2 and updates `last_used_at`
- helpful false: sets `is_active = false` and updates `last_used_at`

Response body:
- pattern_id: integer
- action: `"boosted"` or `"deactivated"`

How to use:
```bash
curl -s -X POST http://localhost:8080/patterns/feedback \
  -H "Content-Type: application/json" \
  -d '{"pattern_id":1,"helpful":true}' \
  | python3 -m json.tool
```

Example response:
```json
{
  "pattern_id": 1,
  "action": "boosted"
}
```

---

## POST /teach

What this route does:
- Saves user-provided instructions that guide SQL generation.
- Detects simple structural conflicts before saving.
- Returns a confirmation token when the new instruction conflicts with an active existing instruction.
- Saves non-conflicting non-correction instructions as unverified with `confidence_score = 0.7`.
- Saves non-conflicting corrections as verified with `confidence_score = 1.0`.

Instruction types:
- `table_relationship`: how tables join
- `business_rule`: business meaning that should affect SQL
- `query_methodology`: ordering, grouping, limits, and query style
- `term_mapping`: business term to table/field mapping
- `filter_rule`: default WHERE conditions
- `correction`: correction to a previous instruction

Body fields:
- instruction_type: string enum (required)
- content: string (required)
- tables_affected: array of table names (optional, default `[]`)
- source_query: string or null (optional)

Response body:
- learning_status: string
- message: string
- instruction_id: integer or null
- similar_instructions: array
- requires_confirmation: boolean
- confirmation_token: string or null

Learning statuses:
- `saved_new`
- `similar_found`
- `conflict_detected`
- `confirmed`
- `rejected`
- `pending_confirmation`
- `updated_existing`

How to teach a join:
```bash
curl -s -X POST http://localhost:8080/teach \
  -H "Content-Type: application/json" \
  -d '{"instruction_type":"table_relationship","content":"employee.contact_id = contact.id","tables_affected":["employee","contact"]}' \
  | python3 -m json.tool
```

How to teach a term:
```bash
curl -s -X POST http://localhost:8080/teach \
  -H "Content-Type: application/json" \
  -d '{"instruction_type":"term_mapping","content":"counselor means employee table","tables_affected":["employee"]}' \
  | python3 -m json.tool
```

How to teach a default filter:
```bash
curl -s -X POST http://localhost:8080/teach \
  -H "Content-Type: application/json" \
  -d '{"instruction_type":"filter_rule","content":"exclude rows where is_deleted = 1","tables_affected":["employee"]}' \
  | python3 -m json.tool
```

Example saved response:
```json
{
  "learning_status": "saved_new",
  "message": "This instruction is new to me. I've saved it and will use it in future queries.",
  "instruction_id": 42,
  "similar_instructions": [],
  "requires_confirmation": false,
  "confirmation_token": null
}
```

Example conflict response:
```json
{
  "learning_status": "conflict_detected",
  "message": "I found an existing rule about employee, contact: 'employee links to contact via employee_id'. Do you want to replace it, keep both, or cancel?",
  "instruction_id": null,
  "similar_instructions": [
    {
      "id": 10,
      "instruction_type": "table_relationship",
      "content": "employee links to contact via employee_id",
      "confidence_score": 0.7,
      "is_verified": false,
      "use_count": 2
    }
  ],
  "requires_confirmation": true,
  "confirmation_token": "9f0c2a8b1234abcd"
}
```

---

## POST /teach/confirm

What this route does:
- Resolves a pending instruction created by `/teach`.
- Pending tokens are in-memory only and expire after 30 minutes.

Body fields:
- confirmation_token: string (required)
- action: one of `confirm`, `reject`, `replace` (required)

Action behavior:
- `confirm`: save the new instruction as verified and keep the old instruction active
- `reject`: discard the pending instruction
- `replace`: deactivate the conflicting instruction and save the new instruction as verified

How to replace a conflicting rule:
```bash
curl -s -X POST http://localhost:8080/teach/confirm \
  -H "Content-Type: application/json" \
  -d '{"confirmation_token":"9f0c2a8b1234abcd","action":"replace"}' \
  | python3 -m json.tool
```

Example response:
```json
{
  "learning_status": "confirmed",
  "message": "Instruction confirmed and replaced the conflicting rule.",
  "instruction_id": 43,
  "similar_instructions": [],
  "requires_confirmation": false,
  "confirmation_token": null
}
```

---

## GET /instructions

What this route does:
- Lists saved user instructions for review.
- Does not mutate counters or state.

Query parameters:
- instruction_type: optional instruction type filter
- active_only: boolean, default `true`

How to use:
```bash
curl -s 'http://localhost:8080/instructions?active_only=true' \
  | python3 -m json.tool
```

Example item:
```json
{
  "id": 1,
  "instruction_type": "term_mapping",
  "content": "counselor means employee table",
  "tables_affected": ["employee"],
  "confidence_score": 0.7,
  "is_verified": false,
  "is_active": true,
  "use_count": 0,
  "success_count": 0,
  "failure_count": 0,
  "last_used_at": null,
  "created_at": "2026-04-29T10:00:00"
}
```

---

## DELETE /instructions/{instruction_id}

What this route does:
- Soft-deletes a user instruction by setting `is_active = false`.
- Also marks the matching embedded `nl2sql_embeddings` metadata as inactive if it exists.
- Never hard-deletes the instruction row.

How to use:
```bash
curl -s -X DELETE http://localhost:8080/instructions/1 \
  | python3 -m json.tool
```

Example response:
```json
{
  "deactivated": true,
  "instruction_id": 1
}
```

---

## POST /query/groups

What this route does:
- Retrieves only schema-group chunks.
- Rewrites the input query for retrieval when `QUERY_REWRITE_ENABLED=true`.
- Skips rewrite for queries with 3 or fewer words.
- Embeds the rewritten search text, while preserving the original query for instruction and pattern context.
- Uses the in-memory embed cache when `EMBED_CACHE_ENABLED=true`.
- Returns a group-focused context package ready for LLM prompting.
- Prepends relevant user instructions to the context when active instructions overlap the retrieved tables and meet `MIN_INSTRUCTION_CONFIDENCE`.
- Appends relevant learned patterns to the context when active patterns overlap the retrieved tables and meet `MIN_PATTERN_USE_COUNT`.

Body fields:
- query: string (required)
- top_k: integer or null (optional)
- request_id: string or null (optional, caller correlation id)

Notes:
- If top_k is omitted or null, server default TOP_K is used.

Example body:
```json
{
  "query": "show unpaid invoices by counselor",
  "top_k": 3
}
```

Response body:
- matched_groups: array of strings
- tables_in_scope: array of strings
- context: string, optionally including `USER-PROVIDED RULES` and `PREVIOUSLY LEARNED PATTERNS` sections
- results: array of query result objects

How to use:
```bash
curl -s -X POST http://localhost:8080/query/groups \
  -H "Content-Type: application/json" \
  -d '{"query":"show unpaid invoices by counselor","top_k":3}' \
  | python3 -m json.tool
```

---

## POST /generate-sql

What this route does:
- Retrieves schema-group context with `retrieve_groups()`.
- Checks the in-memory SQL result cache first when `SQL_CACHE_ENABLED=true`.
- Injects governance rules into ReAct and SQL-generation prompts when `GOVERNANCE_ENABLED=true`.
- Adds relevant user-provided instructions before learned patterns and schema context when available.
- Adds relevant learned patterns to retrieved context when available.
- Loads live MySQL columns for the tables in scope when app DB credentials are available.
- Runs a ReAct loop where `qwen3:4b` decides the next action.
- Calls `deepseek-coder:6.7b` only when the selected action is `GENERATE_SQL`.
- Validates generated SQL with read-only guardrails, table scope checks, column checks, and MySQL EXPLAIN.
- Runs an advisory SQL reviewer after static validation succeeds when `GOVERNANCE_ENABLED=true`.
- Returns generated SQL, clarification, or a transport rejected response.
- Never executes generated SQL.

Body fields:
- query: string (required)
- top_k: integer or null (optional)
- request_id: string or null (optional, caller correlation id)

Notes:
- If top_k is omitted, null, or 0, server default TOP_K is used.
- Cache keys use normalized `query` plus effective `top_k`.
- Only `status="ok"` responses are cached; clarification and rejected responses are never cached.
- Cached responses return `cache_hit: true`; fresh responses return `cache_hit: false`.
- Output dialect is controlled by `SQL_DIALECT`; default is `mysql`.
- Default reasoning model is `qwen3:4b`.
- Default generation model is `deepseek-coder:6.7b`.
- LLM calls go through `nl2sql_service/model_client.py`; `LLM_PROVIDER=ollama` is the current supported provider.
- `LLM_BASE_URL` is independent from `EMBEDDING_API_URL`.
- `REACT_MAX_ITERATIONS` controls the maximum Thought/Action/Observation cycles.
- `LLM_MAX_RETRIES` is retained as a legacy setting and is not the ReAct loop limit.
- `SQL_GENERATION_TIMEOUT` caps the full ReAct SQL-generation workflow and returns a controlled `status="rejected"` response with `REQUEST_TIMEOUT` if exceeded.
- `qwen3:4b` is called with top-level `think=true`, `num_predict=800`, and `REASONING_TEMPERATURE`.
- `deepseek-coder:6.7b` is called with `stream=false` and temperature `0.0`.
- Governance review uses the reasoning model with `think=false`, temperature `0.0`, `max_tokens=150`, and timeout `15s`.
- Request `top_k` is preserved across `RETRIEVE_MORE_CONTEXT` refinement steps.
- `RETRIEVE_MORE_CONTEXT` also refreshes the known column set for the new tables in scope.
- If `USER-PROVIDED RULES` are present in context, the ReAct prompt tells the planner to follow them strictly and treat them as higher priority than defaults.
- Generated SQL is validated immediately in the same ReAct iteration; simple valid queries usually return with `attempt_count=1`.
- Retry `GENERATE_SQL` calls include the prior SQL, blocking validation errors, and the planner instruction.
- Refinement retries must fix listed validation errors, avoid disallowed tables/columns from prior SQL, and follow SQL guardrails if planner hints conflict.
- Review-gate parse failures, malformed reviewer output, or reviewer timeouts fail open and do not block accepted SQL.
- Review-gate failures add `REVIEW_FAILED` to `warnings` but still return `status: "ok"`.
- Ollama transport, upstream, and malformed-response failures return HTTP 200 with `status` set to `"rejected"`.
- SQL validation failures, explicit `GIVE_UP`, `ASK_CLARIFICATION`, and loop exhaustion return HTTP 200 with `status` set to `"clarification_needed"`.
- The ReAct loop always runs before clarification; there is no pre-loop confidence check.
- Clarification uses `qwen3:4b` with `think=false`, temperature `0.3`, and `stream=false`.
- DB pool failures still return HTTP 503.
- Generated SQL should be validated against your app DB schema before relying on it for reporting.
- In common setups, the service `DATABASE_URL` targets pgvector metadata storage, while business rows are queried in a separate MySQL DB.

ReAct actions:
- `RETRIEVE_MORE_CONTEXT`: re-query schema groups with refined terms.
- `FETCH_SCHEMA`: load live MySQL columns for one or more tables.
- `GENERATE_SQL`: call `deepseek-coder:6.7b` to write SQL.
- `VALIDATE_AND_RETURN`: run validators and return success if no blocking warnings remain.
- `ASK_CLARIFICATION`: terminal action when the agent tried context retrieval but still needs the user to rephrase.
- `GIVE_UP`: terminal action when the agent cannot safely generate valid SQL.

Example body:
```json
{
  "query": "show unpaid invoices by counselor",
  "top_k": 3
}
```

Successful response body:
- status: "ok"
- sql: string
- warnings: array of warning objects
- tables_used: array of strings
- matched_groups: array of strings
- attempt_count: integer, equal to completed ReAct iterations
- cache_hit: boolean
- react_trace: object or null

Example success:
```json
{
  "status": "ok",
  "sql": "SELECT id, amount FROM invoice WHERE status = 'unpaid'",
  "warnings": [],
  "tables_used": ["invoice"],
  "matched_groups": ["billing"],
  "attempt_count": 1,
  "cache_hit": false,
  "react_trace": {
    "steps": [
      {
        "iteration": 1,
        "thought": "I should generate SQL for billing tables.",
        "action": "GENERATE_SQL",
        "action_input": "generate select",
        "observation": "Generated: SELECT id, amount FROM invoice WHERE status = 'unpaid'\nAuto-validation: PASSED: SQL is valid and safe."
      }
    ],
    "total_iterations": 1,
    "final_action": "VALIDATE_AND_RETURN"
  }
}
```

Clarification response body:
- status: "clarification_needed"
- question: string
- suggestions: array of 2-3 refined query strings
- original_query: string
- failure_reason: string
- cache_hit: boolean
- react_trace: object or null

Clarification responses do not include:
- sql
- warnings
- tables_used
- matched_groups
- attempt_count

Example clarification response:
```json
{
  "status": "clarification_needed",
  "question": "Are you searching for an employee record or a contact record?",
  "suggestions": [
    "find employee with contact name aman",
    "search contact by name aman"
  ],
  "original_query": "fetch aman",
  "failure_reason": "Cannot determine correct table structure",
  "cache_hit": false,
  "react_trace": {
    "steps": [
      {
        "iteration": 1,
        "thought": "The schema context is insufficient to continue safely.",
        "action": "GIVE_UP",
        "action_input": "Cannot determine correct table structure",
        "observation": "Agent gave up: Cannot determine correct table structure"
      }
    ],
    "total_iterations": 1,
    "final_action": "GIVE_UP"
  }
}
```

Rejected response body (transport failures only):
- status: "rejected"
- sql: null
- warnings: array of warning objects
- attempt_count: integer, equal to completed ReAct iterations
- cache_hit: boolean
- react_trace: object or null

Rejected responses do not include:
- question
- suggestions
- tables_used
- matched_groups

Example rejected response:
```json
{
  "status": "rejected",
  "sql": null,
  "warnings": [
    {
      "code": "OLLAMA_TIMEOUT",
      "message": "Reasoning model timed out after 45s"
    }
  ],
  "attempt_count": 0,
  "cache_hit": false,
  "react_trace": null
}
```

Warning codes:
- REQUEST_TIMEOUT
- OLLAMA_TIMEOUT
- OLLAMA_UPSTREAM
- OLLAMA_MALFORMED
- SQL_EMPTY
- SQL_MULTI_STATEMENT
- SQL_DESTRUCTIVE
- SQL_NOT_SELECT
- TABLE_OUT_OF_SCOPE
- COLUMN_OUT_OF_SCOPE
- MYSQL_EXPLAIN_ERROR
- MYSQL_EXPLAIN_UNAVAILABLE
- REVIEW_FAILED
- MAX_RETRIES_EXCEEDED

SQL guardrails:
- SQL must be non-empty.
- SQL must be exactly one statement.
- SQL must be SELECT or WITH...SELECT.
- SQL must not contain destructive keywords outside comments or string literals.
- Tables in FROM/JOIN clauses must be within `tables_in_scope`.
- Columns must be within the known live-schema columns when available.
- MySQL EXPLAIN must pass when app DB is reachable.
- `MYSQL_EXPLAIN_UNAVAILABLE` is informational and does not cause rejection.
- Schema-qualified table names are normalized to bare table names.
- CTE aliases are not treated as unknown tables.
- Explicit `GIVE_UP` returns `clarification_needed`.
- Loop-exhausted SQL validation failures return `clarification_needed` with `failure_reason` containing warning codes such as `TABLE_OUT_OF_SCOPE` and `MAX_RETRIES_EXCEEDED`.
- Reasoning-model timeouts or upstream failures do not add a synthetic loop-exhausted warning.

Golden clarification-trace pattern:
- A validation-driven clarification run can end with `final_action = VALIDATE_AND_RETURN` when every generated SQL remains invalid through the iteration ceiling.
- In that case, `react_trace.steps` can contain only `GENERATE_SQL` actions, with `Auto-validation: FAILED:` included in each observation.

Manual execution checklist (run yourself):
1. Generate SQL:
```bash
curl -s -X POST http://localhost:8080/generate-sql \
  -H "Content-Type: application/json" \
  -d '{"query":"list 20 unpaid invoices with counselor details","top_k":5}' \
  > /tmp/gen.json
```
2. Inspect response:
```bash
python3 - <<'PY'
import json
with open('/tmp/gen.json', 'r', encoding='utf-8') as f:
    d = json.load(f)
print('status =', d.get('status'))
print('warnings =', d.get('warnings'))
print('question =', d.get('question'))
print('suggestions =', d.get('suggestions'))
print('sql =', d.get('sql'))
PY
```
3. Execute only when `status=ok` and SQL is non-empty:
```bash
set -a
. ./.env
set +a
SQL=$(python3 - <<'PY'
import json
with open('/tmp/gen.json', 'r', encoding='utf-8') as f:
    d = json.load(f)
print((d.get('sql') or '').strip().rstrip(';'))
PY
)
MYSQL_PWD="$DB_PASSWORD" mysql \
  -h "$DB_HOST" -P "$DB_PORT" -u "$DB_USER" -D pf_TickleRight_9210 \
  -e "${SQL} LIMIT 20"
```
4. If MySQL returns unknown column/table errors, inspect schema and re-ask:
```bash
MYSQL_PWD="$DB_PASSWORD" mysql -h "$DB_HOST" -P "$DB_PORT" -u "$DB_USER" -D pf_TickleRight_9210 -e "SHOW COLUMNS FROM invoice;"
```

How to use:
```bash
curl -s -X POST http://localhost:8080/generate-sql \
  -H "Content-Type: application/json" \
  -d '{"query":"show unpaid invoices by counselor","top_k":3}' \
  | python3 -m json.tool
```

---

## Practical Testing Order

1. Check service health:
```bash
curl -s http://localhost:8080/health | python3 -m json.tool
```

2. Ingest schema groups:
```bash
./.venv/bin/python scripts/nl2sql_ingest_groups.py --url http://localhost:8080
```

3. Ingest enriched knowledge:
```bash
./.venv/bin/python scripts/nl2sql_ingest_groups.py --url http://localhost:8080 --knowledge
```

Optional one-command serial ingest (groups + full knowledge):
```bash
./.venv/bin/python scripts/nl2sql_ingest_groups.py --url http://localhost:8080 --all --timeout 600
```

4. Query mixed retrieval:
```bash
curl -s -X POST http://localhost:8080/query \
  -H "Content-Type: application/json" \
  -d '{"query":"find invoice status columns and related sql","top_k":10}' \
  | python3 -m json.tool
```

5. Query group-focused retrieval:
```bash
curl -s -X POST http://localhost:8080/query/groups \
  -H "Content-Type: application/json" \
  -d '{"query":"inquiry lifecycle ownership and assignment","top_k":5}' \
  | python3 -m json.tool
```

6. Teach a user instruction:
```bash
curl -s -X POST http://localhost:8080/teach \
  -H "Content-Type: application/json" \
  -d '{"instruction_type":"term_mapping","content":"counselor means employee table","tables_affected":["employee"]}' \
  | python3 -m json.tool
```

7. Verify instruction injection in group context:
```bash
curl -s -X POST http://localhost:8080/query/groups \
  -H "Content-Type: application/json" \
  -d '{"query":"show counselor contact details","top_k":5}' \
  | python3 -m json.tool
```

8. Generate SQL:
```bash
curl -s -X POST http://localhost:8080/generate-sql \
  -H "Content-Type: application/json" \
  -d '{"query":"show unpaid invoices by counselor","top_k":3}' \
  | python3 -m json.tool
```

9. Manually embed user instructions for mixed vector retrieval:
```bash
curl -s -X POST http://localhost:8080/ingest/instructions \
  -H "Content-Type: application/json" \
  -d '{}' \
  | python3 -m json.tool
```

10. Manually embed learned patterns when enough successful `/ask` calls have accumulated:
```bash
curl -s -X POST http://localhost:8080/ingest/patterns \
  -H "Content-Type: application/json" \
  -d '{}' \
  | python3 -m json.tool
```

---

## POST /ask

What this route does:
- Reuses `/generate-sql` first (same guardrails and ReAct workflow).
- Executes SQL on the app MySQL database only when generation succeeds.
- Enforces a maximum execution bound of 50 rows.
- Calls an answer model with a strict `ANSWER` / `KEY FIGURES` / `DETAILS` template, all result columns, the first 10 rows, and a capped token budget.
- Appends governance grounding rules to the answer prompt when `GOVERNANCE_ENABLED=true`.
- Parses the template into a clean natural-language answer.
- Falls back to a compact deterministic row summary if the answer model times out, returns malformed output, or does not follow the template after SQL execution succeeds.
- Adds a non-blocking `ANSWER_HALLUCINATION` warning when the final model answer contains numbers not present in returned row values.
- May also surface non-blocking `REVIEW_FAILED` warnings inherited from the successful `/generate-sql` path.
- Returns a controlled `status: "rejected"` response with `REQUEST_TIMEOUT` if the end-to-end `/ask` workflow exceeds `ASK_TIMEOUT`.
- Returns answer text plus SQL metadata.
- Saves a learned pattern in the background when the final response is `status: "ok"` and `row_count > 0`.
- The underlying ReAct generation path records user-instruction success/failure counters in the background.

Body fields:
- query: string (required)
- top_k: integer or null (optional)

Notes:
- `/generate-sql` behavior remains unchanged and still never executes SQL.
- Row-cap logic:
  - no LIMIT in generated SQL: execution uses `LIMIT 50`
  - smaller LIMIT: preserved
  - larger LIMIT: execution capped to 50
- Raw rows are not returned by default.
- `ANSWER_MODEL` defaults to `REASONING_MODEL` when unset.
- `ANSWER_STRICT_CONCISE=true` still enforces the configured word cap after template parsing.
- `ASK_TIMEOUT` should be lower than any upstream gateway/client timeout so callers receive this JSON response instead of a transport timeout.
- SQL generation clarification returns `status: "clarification_needed"` and does not execute SQL.
- SQL generation transport rejection returns `status: "rejected"` and does not execute SQL.
- Pattern saving uses `asyncio.create_task()` and is not awaited by the request path.

Successful response body:
- status: "ok"
- answer: string
- sql: string
- warnings: array of warning objects
- row_count: integer
- columns: array of strings
- tables_used: array of strings
- matched_groups: array of strings
- attempt_count: integer
- react_trace: object or null

Clarification response body:
- status: "clarification_needed"
- question: string
- suggestions: array of 2-3 refined query strings
- original_query: string
- failure_reason: string
- react_trace: object or null

Clarification responses do not include:
- answer
- sql
- warnings
- row_count
- columns
- tables_used
- matched_groups
- attempt_count

Rejected response body:
- status: "rejected"
- answer: null
- sql: string or null
- warnings: array of warning objects
- attempt_count: integer
- react_trace: object or null

`sql` semantics in rejected responses:
- `null` when SQL generation failed due to transport/upstream/malformed response
- non-null when SQL generation succeeded but execution failed

Example body:
```json
{
  "query": "newest payment",
  "top_k": 5
}
```

Example success:
```json
{
  "status": "ok",
  "answer": "The latest payment is from member A with amount 1200.",
  "sql": "SELECT payment.* FROM payment ORDER BY payment.date DESC LIMIT 1;",
  "warnings": [],
  "row_count": 1,
  "columns": ["id", "member_id", "amount", "date"],
  "tables_used": ["payment"],
  "matched_groups": ["legacy_invoice_billing"],
  "attempt_count": 1,
  "react_trace": null
}
```

Warning codes commonly seen in `/ask`:
- REQUEST_TIMEOUT
- OLLAMA_TIMEOUT
- OLLAMA_UPSTREAM
- OLLAMA_MALFORMED
- MYSQL_QUERY_ERROR
- ANSWER_TIMEOUT
- ANSWER_UPSTREAM
- ANSWER_MALFORMED
- ANSWER_HALLUCINATION
- REVIEW_FAILED

How to use:
```bash
curl -s -X POST http://localhost:8080/ask \
  -H "Content-Type: application/json" \
  -d '{"query":"newest payment","top_k":5}' \
  | python3 -m json.tool
```

---

## POST /ask/stream

What this route does:
- Runs the same guarded generate -> execute -> answer workflow as `/ask`.
- Streams newline-delimited JSON progress events as each stage completes.
- Ends with a `final` event whose `response` field matches the normal `/ask` response shape.
- If SQL generation returns `clarification_needed`, the stream emits `sql_generation_rejected` with `question` and `suggestions`, then the final clarification response.
- `answer_generation_finished` can include `ANSWER_HALLUCINATION` warnings from the structured answer guard.

Body fields:
- query: string (required)
- top_k: integer or null (optional)

Response content type:
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

How to use:
```bash
curl -N -s -X POST http://localhost:8080/ask/stream \
  -H "Content-Type: application/json" \
  -d '{"query":"show me the 5 most recent inquiries","top_k":3}'
```

Example output:
```jsonl
{"event":"started","message":"Received question.","query":"show me the 5 most recent inquiries","top_k":3}
{"event":"sql_generation_started","message":"Retrieving schema context and generating guarded SQL."}
{"event":"sql_generation_finished","message":"SQL generated and validated.","sql":"SELECT ...","warnings":[]}
{"event":"execution_started","message":"Executing bounded SQL on the app MySQL database."}
{"event":"execution_finished","message":"SQL execution finished.","row_count":5,"columns":["id","created_at"]}
{"event":"answer_generation_started","message":"Generating final answer from bounded result rows."}
{"event":"answer_generation_finished","message":"Final answer is ready.","warnings":[]}
{"event":"final","response":{"status":"ok","answer":"...","sql":"..."}}
```
