# NL2SQL Service

FastAPI service for retrieval-augmented NL2SQL generation, bounded query execution,
interactive teaching, and version-aware ingest.

## Current Scope

- PostgreSQL + pgvector store for retrieval and persistent query cache
- MySQL app DB introspection for column validation and bounded `/ask` execution
- ReAct SQL generation with validation and optional governance review
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
- `POST /teach`, `POST /teach/confirm`
  - save or resolve user-provided instructions
- `POST /ingest/groups`
- `POST /ingest/knowledge`
- `POST /ingest/patterns`
- `POST /ingest/instructions`
- `GET /instructions`

## Persistent Query Cache

The fast path is now two-layered:

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

The service maintains a DB-backed cache epoch. After successful
knowledge-changing operations it:

1. bumps the epoch in PostgreSQL
2. clears in-memory caches

Epoch bump triggers:

- `/teach`
- `/teach/confirm`
- `/ingest/groups`
- `/ingest/knowledge`
- `/ingest/patterns`
- `/ingest/instructions`

Old DB cache rows are not deleted immediately. They become inactive because
lookups are restricted to the current epoch.

## Teach Semantics

`/teach` and `/teach/confirm` intentionally use controlled application
responses.

- HTTP `200`
  - `saved_new`
  - `similar_found`
  - `conflict_detected`
  - `confirmed`
  - `rejected`
  - other controlled learning outcomes
- HTTP `503`
  - only when the DB pool or backing store is unavailable

## Version-Aware Ingest

The four ingest routes are version-aware and skip unchanged chunks before
embedding. Their responses now include `skipped`.

- `/ingest/groups`
  - also returns `failure_count`, `failed_groups`, and `enrichment_summary`
- `/ingest/knowledge`
- `/ingest/patterns`
- `/ingest/instructions`

## Important Tables

- `nl2sql_embeddings`
- `nl2sql_learned_patterns`
- `nl2sql_user_instructions`
- `nl2sql_request_events`
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

## Quick Usage

Generate SQL:

```bash
curl -s -X POST http://localhost:8080/generate-sql \
  -H "Content-Type: application/json" \
  -d '{"query":"show unpaid invoices by counselor","top_k":5}' \
  | python3 -m json.tool
```

Ask:

```bash
curl -s -X POST http://localhost:8080/ask \
  -H "Content-Type: application/json" \
  -d '{"query":"newest payment","top_k":5}' \
  | python3 -m json.tool
```

Teach:

```bash
curl -s -X POST http://localhost:8080/teach \
  -H "Content-Type: application/json" \
  -d '{"instruction_type":"term_mapping","content":"counselor means employee","tables_affected":["employee"]}' \
  | python3 -m json.tool
```

Confirm or replace a conflict:

```bash
curl -s -X POST http://localhost:8080/teach/confirm \
  -H "Content-Type: application/json" \
  -d '{"confirmation_token":"TOKEN","action":"replace"}' \
  | python3 -m json.tool
```

List instructions:

```bash
curl -s 'http://localhost:8080/instructions?active_only=true' \
  | python3 -m json.tool
```

Ingest knowledge:

```bash
curl -s -X POST http://localhost:8080/ingest/knowledge \
  -H "Content-Type: application/json" \
  -d '{}' \
  | python3 -m json.tool
```

Inspect cache stats:

```bash
curl -s http://localhost:8080/cache/stats | python3 -m json.tool
```

## Docs

- Route reference: `ROUTES.md`
- Current implementation summary: `implementation_complete.md`
- Historical planning notes: `nl2sql_refactor_plan.md`
