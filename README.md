# NL2SQL Service

FastAPI service for retrieval-augmented NL2SQL generation, bounded query
execution, interactive teaching, trace telemetry, and version-aware ingest.

## Current Scope

- PostgreSQL + pgvector store for retrieval, traces, failure logs, and persistent query cache
- MySQL app DB introspection for column validation and bounded `/ask` execution
- ReAct SQL generation with validation and optional governance review
- Streaming Ask path with live NDJSON progress and trace events
- Interactive instruction learning via `/teach` and `/teach/confirm`
- Version-aware ingest routes that skip unchanged chunks
- In-memory exact/semantic caches plus DB-backed exact/semantic query cache

## Key Runtime Paths

- `POST /generate-sql`
  - retrieve schema context
  - apply user instructions and learned patterns
  - generate guarded SQL
  - return SQL only, never execute it
- `POST /ask`
  - reuse `/generate-sql`
  - execute bounded SQL on MySQL
  - generate a natural-language answer
- `POST /ask/stream`
  - stream `application/x-ndjson` stage events while the same Ask workflow runs
  - emit sanitized `trace` events and a terminal `final` response event
- `GET /telemetry/trace/{request_id}`
  - return persisted trace events ordered by `seq`
- `POST /teach`, `POST /teach/confirm`
  - save or resolve user-provided instructions
- `GET /failures`
- `POST /ingest/groups`
- `POST /ingest/knowledge`
- `POST /ingest/patterns`
- `POST /ingest/instructions`
- `GET /instructions`

## Streaming Ask

Example:

```bash
curl -N -s -X POST http://localhost:8080/ask/stream \
  -H "Content-Type: application/json" \
  -d '{"query":"newest payment","top_k":5,"request_id":"req-demo"}'
```

Example NDJSON lines:

```json
{"event":"started","message":"Received question.","query":"newest payment","top_k":5,"request_id":"req-demo"}
{"event":"trace","request_id":"req-demo","seq":2,"layer":"nl2sql-service","stage":"schema_retrieval","status":"completed","message":"Retrieved 3 table(s) for SQL planning.","duration_ms":42,"warning_codes":[],"error_source":null,"details":{"tables_in_scope":["payment"]}}
{"event":"final","response":{"status":"ok","answer":"...","sql":"SELECT ...","warnings":[]}}
```

The stream has explicit service timeout handling. If SQL generation, MySQL
execution, or answer generation exceeds the Ask budget, the service emits a
`timeout` event followed by a `final` rejected response with
`REQUEST_TIMEOUT`.

## Trace Events

Trace events are persisted in `nl2sql_trace_events` and exposed through:

```bash
curl -s http://localhost:8080/telemetry/trace/req-demo?limit=500 | python -m json.tool
```

Shared event shape:

```json
{
  "request_id": "req-demo",
  "seq": 1,
  "layer": "nl2sql-service",
  "stage": "request_received",
  "status": "started",
  "message": "Received ask request.",
  "duration_ms": null,
  "warning_codes": [],
  "error_source": null,
  "details": {},
  "created_at": "2026-05-25T00:00:00Z"
}
```

Instrumented stages include request received, cache lookup, query rewrite,
schema retrieval, ReAct iteration/action, SQL generation, SQL validation,
EXPLAIN/review gate, MySQL execution, answer generation, failure logging, cache
write, and complete.

Trace output is sanitized. It may include action summaries, observations, SQL
previews, timings, warning codes, and provider/model errors. It must not expose
hidden reasoning/thought text.

## Failure Context And Teaching

Failures are written to `nl2sql_failure_log` and can be inspected by
`request_id` through trace telemetry. The frontend uses this context to ask the
user for intended meaning, correct tables or columns, business rules, and
expected output before saving a `/teach` correction. No instruction is created
without user-provided context.

## Persistent Query Cache

The fast path is two-layered:

1. in-memory exact / semantic cache
2. PostgreSQL exact / semantic cache for the active cache epoch
3. full pipeline on miss

Cached endpoints:

- `/generate-sql`
- `/ask`

Cache persistence rules:

- only `status="ok"` responses are stored
- stored fields include normalized query, endpoint, `top_k`, response JSON,
  query embedding, hit counters, timestamps, and cache epoch
- `/ask` stores successful answers even when `row_count = 0`

Returned metadata:

- `cache_hit`
- `cache_source`

`cache_source` values:

- `none`
- `memory_exact`
- `memory_semantic`
- `db_exact`
- `db_semantic`

## Cache Epoch Invalidation

After successful knowledge-changing operations the service bumps the DB-backed
query cache epoch and clears in-memory caches.

Epoch bump triggers:

- `/teach`
- `/teach/confirm`
- `/ingest/groups`
- `/ingest/knowledge`
- `/ingest/patterns`
- `/ingest/instructions`

## Important Tables

- `nl2sql_embeddings`
- `nl2sql_learned_patterns`
- `nl2sql_user_instructions`
- `nl2sql_request_events`
- `nl2sql_failure_log`
- `nl2sql_trace_events`
- `nl2sql_benchmark_cases`
- `nl2sql_query_cache`
- `nl2sql_cache_state`

## Core Settings

Key defaults from `nl2sql_service/config.py`:

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

Timeouts should be ordered from inner to outer:

```text
Standalone /generate-sql timeout: SQL_GENERATION_TIMEOUT
Standalone /ask timeout:         ASK_TIMEOUT
server_1 wrapper timeout:        longer than ASK_TIMEOUT
Next.js NL2SQL proxy timeout:    300000ms
External reverse proxy:          longer than frontend proxy
```

## Quick Usage

Generate SQL:

```bash
curl -s -X POST http://localhost:8080/generate-sql \
  -H "Content-Type: application/json" \
  -d '{"query":"show unpaid invoices by counselor","top_k":5}' \
  | python -m json.tool
```

Ask:

```bash
curl -s -X POST http://localhost:8080/ask \
  -H "Content-Type: application/json" \
  -d '{"query":"newest payment","top_k":5}' \
  | python -m json.tool
```

Find a stuck request:

```bash
curl -s 'http://localhost:8080/telemetry/trace/REQ_ID?limit=500' \
  | python -m json.tool
```

## Verification

Focused regression command used for trace, stream, cache, and teach behavior:

```bash
python -m pytest tests/test_ask.py tests/test_cache.py tests/test_interactive_learning.py
```

For cold local environments, set `DATABASE_URL`, `EMBEDDING_API_URL`, and
`RAG_SCHEMA_DIR` before running the tests.

## Docs

- Route reference: `ROUTES.md`
- Current implementation summary: `implementation_complete.md`
- Historical planning notes: `nl2sql_refactor_plan.md`
