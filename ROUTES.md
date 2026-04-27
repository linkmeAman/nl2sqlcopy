# NL2SQL API Routes Guide

This document is a route-by-route reference for the FastAPI service in this repository.

## Change Log

- 2026-04-26: Added `POST /ask` route contract.
- 2026-04-26: Documented `/ask` bounded execution rule (max 50 rows), rejection behavior, and warning surfaces.

Base URL examples below assume:
- http://localhost:8080

## Quick Route List

- GET /health
- POST /ingest
- POST /query
- POST /ingest/groups
- POST /ingest/knowledge
- POST /query/groups
- POST /generate-sql
- POST /ask
- POST /ask/stream

## Common Behavior Across Routes

- Content type for POST routes: application/json
- If database is unavailable, DB-backed routes return HTTP 503
- Embedding upstream timeout or upstream failure returns HTTP 502 for embedding-backed ingest/retrieval routes
- `/generate-sql` returns HTTP 200 with `status: "rejected"` for Ollama failures and SQL validation failures
- `/generate-sql` does not execute SQL; it only returns generated SQL or rejection warnings
- `/ask` returns HTTP 200 with `status: "rejected"` for SQL-generation and SQL-execution failures; answer-model failures return a compact fallback answer when SQL execution succeeded
- `/ask` executes SQL only after `/generate-sql` succeeds, with a hard execution cap of 50 rows
- `/ask/stream` runs the same ask workflow as `/ask`, but returns progress as newline-delimited JSON events
- Invalid request body shape returns HTTP 422
- Versioned ingestion routes (`/ingest/groups`, `/ingest/knowledge`) pre-check existing `schema_version` and skip unchanged chunks before embedding

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
- Embeds the input query.
- Performs cosine-similarity retrieval across all chunk types.
- Returns top matching chunks with metadata.
- Returns similarity scores and metadata, not raw embedding vectors.

Body fields:
- query: string (required)
- top_k: integer or null (optional)

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

How to use:
```bash
curl -s -X POST http://localhost:8080/ingest/groups \
  -H "Content-Type: application/json" \
  -d '{"group_names":["inquiry_lifecycle"]}' \
  | python3 -m json.tool
```

Example response:
```json
{
  "inserted": 8,
  "updated": 0,
  "source": "all groups",
  "enrichment_summary": {
    "groups_with_columns": 8,
    "groups_without_columns": 0,
    "groups_with_aliases": 6,
    "groups_with_examples": 8
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

## POST /query/groups

What this route does:
- Retrieves only schema-group chunks.
- Returns a group-focused context package ready for LLM prompting.

Body fields:
- query: string (required)
- top_k: integer or null (optional)

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
- context: string
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
- Loads live MySQL columns for the tables in scope when app DB credentials are available.
- Runs a ReAct loop where `qwen3:4b` decides the next action.
- Calls `deepseek-coder:6.7b` only when the selected action is `GENERATE_SQL`.
- Validates generated SQL with read-only guardrails, table scope checks, column checks, and MySQL EXPLAIN.
- Returns generated SQL or a rejected response.
- Never executes generated SQL.

Body fields:
- query: string (required)
- top_k: integer or null (optional)

Notes:
- If top_k is omitted, null, or 0, server default TOP_K is used.
- Output dialect is controlled by `SQL_DIALECT`; default is `mysql`.
- Default reasoning model is `qwen3:4b`.
- Default generation model is `deepseek-coder:6.7b`.
- `LLM_BASE_URL` is independent from `EMBEDDING_API_URL`.
- `REACT_MAX_ITERATIONS` controls the maximum Thought/Action/Observation cycles.
- `LLM_MAX_RETRIES` is retained as a legacy setting and is not the ReAct loop limit.
- `qwen3:4b` is called with top-level `think=true`, `num_predict=800`, and `REASONING_TEMPERATURE`.
- `deepseek-coder:6.7b` is called with `stream=false` and temperature `0.0`.
- Request `top_k` is preserved across `RETRIEVE_MORE_CONTEXT` refinement steps.
- `RETRIEVE_MORE_CONTEXT` also refreshes the known column set for the new tables in scope.
- Generated SQL is validated immediately in the same ReAct iteration; simple valid queries usually return with `attempt_count=1`.
- Retry `GENERATE_SQL` calls include the prior SQL, blocking validation errors, and the planner instruction.
- Refinement retries must fix listed validation errors, avoid disallowed tables/columns from prior SQL, and follow SQL guardrails if planner hints conflict.
- SQL and Ollama failures return HTTP 200 with `status` set to `"rejected"`.
- DB pool failures still return HTTP 503.
- Generated SQL should be validated against your app DB schema before relying on it for reporting.
- In common setups, the service `DATABASE_URL` targets pgvector metadata storage, while business rows are queried in a separate MySQL DB.

ReAct actions:
- `RETRIEVE_MORE_CONTEXT`: re-query schema groups with refined terms.
- `FETCH_SCHEMA`: load live MySQL columns for one or more tables.
- `GENERATE_SQL`: call `deepseek-coder:6.7b` to write SQL.
- `VALIDATE_AND_RETURN`: run validators and return success if no blocking warnings remain.
- `GIVE_UP`: return a controlled rejection.

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

Rejected response body:
- status: "rejected"
- sql: null
- warnings: array of warning objects
- attempt_count: integer, equal to completed ReAct iterations
- react_trace: object or null

Rejected responses do not include:
- tables_used
- matched_groups

Example rejected response:
```json
{
  "status": "rejected",
  "sql": null,
  "warnings": [
    {
      "code": "MAX_RETRIES_EXCEEDED",
      "message": "ReAct agent chose GIVE_UP: Cannot determine correct table structure"
    }
  ],
  "attempt_count": 1,
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

Warning codes:
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
- Explicit `GIVE_UP` returns one terminal warning; loop-exhausted warnings are reserved for actual iteration exhaustion.
- Reasoning-model timeouts or upstream failures do not add a synthetic loop-exhausted warning.

Golden rejected-trace pattern:
- A validation-driven rejected run can end with `final_action = VALIDATE_AND_RETURN` when every generated SQL remains invalid through the iteration ceiling.
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

6. Generate SQL:
```bash
curl -s -X POST http://localhost:8080/generate-sql \
  -H "Content-Type: application/json" \
  -d '{"query":"show unpaid invoices by counselor","top_k":3}' \
  | python3 -m json.tool
```

---

## POST /ask

What this route does:
- Reuses `/generate-sql` first (same guardrails and ReAct workflow).
- Executes SQL on the app MySQL database only when generation succeeds.
- Enforces a maximum execution bound of 50 rows.
- Calls an answer model with `think=false`, selected result columns, the first 10 displayed rows, and a capped answer length.
- Falls back to a compact deterministic row summary if the answer model times out or returns malformed output after SQL execution succeeds.
- Returns answer text plus SQL metadata.

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
- SQL generation rejection returns `status: "rejected"` and does not execute SQL.

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

Rejected response body:
- status: "rejected"
- answer: null
- sql: string or null
- warnings: array of warning objects
- attempt_count: integer
- react_trace: object or null

`sql` semantics in rejected responses:
- `null` when SQL generation itself failed
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
- OLLAMA_TIMEOUT
- OLLAMA_UPSTREAM
- OLLAMA_MALFORMED
- MYSQL_QUERY_ERROR
- ANSWER_TIMEOUT
- ANSWER_UPSTREAM
- ANSWER_MALFORMED

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
