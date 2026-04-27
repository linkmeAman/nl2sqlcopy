# NL2SQL Service (Current Setup)

This repository currently contains a production-oriented NL2SQL context service built with FastAPI, PostgreSQL, and pgvector.

It also contains older Qdrant/FastEmbed scripts that are still useful for offline corpus work, but they are not part of the primary runtime path.

## 0) Change Log

- 2026-04-26: Added `POST /ask` for end-to-end ask -> guarded SQL generation -> bounded MySQL execution -> natural-language answer generation.
- 2026-04-26: Added `/ask` settings (`ANSWER_MODEL`, `ANSWER_TIMEOUT`, `ANSWER_TEMPERATURE`) and documented row-cap behavior (max 50 rows).

## 1) Architecture at a Glance

- API service: FastAPI app in `nl2sql_service/main.py`
- Primary vector store: PostgreSQL + pgvector table `nl2sql_embeddings`
- Embedding provider: remote TEI-compatible endpoint (`inputs` payload) configured via env vars
- SQL planning provider: Ollama `qwen3:4b` for ReAct Thought/Action decisions
- SQL generation provider: Ollama `deepseek-coder:6.7b` for SQL writing only
- Chunking paths:
  - free text chunking
  - schema table chunking
  - schema-group chunking (driven by `rag_schema/` JSON files)
  - enriched knowledge chunking from docs + `rag_schema/` metadata sources

Current serving flow:

1. Request arrives at `/ingest`, `/query`, `/ingest/groups`, `/ingest/knowledge`, `/query/groups`, `/generate-sql`, or `/ask`
2. Text is embedded through the configured embedding API
3. Vectors are written to or searched from `nl2sql_embeddings`
4. Retrieval results return similarity-scored context and metadata
5. `/generate-sql` runs a ReAct loop: retrieve schema context, let `qwen3:4b` choose the next action, call `deepseek-coder:6.7b` only for SQL generation, validate, and return accepted or rejected SQL without executing it
6. `/ask` reuses `/generate-sql`, executes bounded SQL on app MySQL (max 50 rows), then calls an answer model to produce natural-language output, with a compact row-based fallback if the answer model fails
7. `/ask/stream` runs the same ask pipeline but emits newline-delimited JSON progress events before the final response

## 2) What Has Been Implemented

### Core service

- Async FastAPI app with startup/shutdown lifecycle
- Non-fatal DB startup: service starts even if DB is temporarily unreachable
- Shared async embedding client with retries and timeout handling
- Centralized settings via `.env` (`pydantic-settings`)

### Ingestion

- `POST /ingest`
  - `type="text"`: token-aware chunk splitting
  - `type="schema"`: one chunk per provided schema table text
- `POST /ingest/groups`
  - builds group chunks from entity JSON files in `rag_schema/entities/`
  - enriches each group chunk with:
    - live MySQL columns for the group tables (when app DB is reachable)
    - entity business aliases (`business_aliases`)
    - entity NL examples (`example_questions`)
  - `schema_version` is the MD5 of the raw entity file bytes (first 8 chars); changes automatically when PHP regenerates the file
  - inserts or updates by `(source, chunk_index)` and `schema_version`
  - returns inserted and updated counts plus `enrichment_summary`
  - accepts entity IDs (`entity__inquiry_lifecycle`) or short names (`inquiry_lifecycle`)
- `POST /ingest/knowledge`
  - embeds column-catalog chunks from:
    - `ignore_docs_now/docs/nl2sql-columns.jsonl`
    - `ignore_docs_now/docs/generated/nl2sql_schema_tables.jsonl`
    - `ignore_docs_now/docs/generated/nl2sql_schema_views.jsonl`
  - embeds SQL-example chunks from view definitions in `ignore_docs_now/docs/mysql_schema_export.txt`
  - embeds relation-link chunks from `rag_schema/relations/*.json`
  - embeds table-node chunks from `rag_schema/graph/table_graph.json`
  - embeds view-node chunks from `rag_schema/graph/view_registry.json`
  - embeds schema-rule chunks from `rag_schema/rules/onboarding_rules.json`
  - pre-checks existing `(source, chunk_index, schema_version)` rows and skips unchanged chunks before calling embedding API
  - uses version-aware upsert keyed by `(source, chunk_index)` and `schema_version`
  - request body supports:
    - `include_column_catalog` (bool)
    - `include_sql_examples` (bool)
    - `include_relations` (bool)
    - `include_graph` (bool)
    - `include_view_registry` (bool)
    - `include_onboarding_rules` (bool)
    - `column_limit` (int or null)
    - `sql_example_limit` (int or null)
    - `relation_limit` (int or null)
    - `graph_limit` (int or null)
    - `view_registry_limit` (int or null)

### Retrieval

- `POST /query`
  - cosine similarity retrieval across all chunk types (`schema_group`, `column_catalog`, `sql_example`, etc.)
- `POST /query/groups`
  - retrieves only `metadata.type = schema_group`
  - returns:
    - `matched_groups`
    - `tables_in_scope`
    - composed `context` block
    - raw `results`

### SQL generation

- `POST /generate-sql`
  - reuses `retrieve_groups()` to select matched schema groups and tables in scope
  - loads live MySQL columns for retrieved tables when app DB credentials are configured
  - uses `qwen3:4b` as a ReAct planning agent with hybrid thinking enabled (`think=true`)
  - uses `deepseek-coder:6.7b` only for the `GENERATE_SQL` action
  - generates MySQL-compatible SELECT syntax by default (`SQL_DIALECT=mysql`)
  - validates that output is one read-only SELECT or WITH...SELECT statement
  - rejects destructive SQL, multi-statement SQL, empty output, non-SELECT SQL, tables outside the retrieved scope, unknown columns, and blocking MySQL EXPLAIN errors
  - ignores destructive-looking words inside comments and string literals during safety checks
  - allows CTE aliases without treating them as unknown tables
  - can retrieve more context, fetch schema, regenerate SQL, validate, or give up within `REACT_MAX_ITERATIONS`
  - preserves the request `top_k` across `RETRIEVE_MORE_CONTEXT` refinement steps
  - refreshes the known column set whenever `RETRIEVE_MORE_CONTEXT` changes the tables in scope
  - validates generated SQL immediately in the same ReAct iteration; simple valid queries usually return with `attempt_count=1`
  - SQL retries include the prior SQL, blocking validation errors, and the planner instruction before regenerating
  - refinement retries are strict: they must correct listed validation errors, avoid disallowed tables/columns from prior attempts, and honor guardrails over planner hints
  - treats `MYSQL_EXPLAIN_UNAVAILABLE` as a non-blocking warning
  - returns a `react_trace` in both success and rejected responses
  - never executes generated SQL
  - returns HTTP 200 for both accepted SQL (`status="ok"`) and controlled generation failures (`status="rejected"`)
  - generated SQL is still returned for caller-controlled execution; use a read-only MySQL user before production use

### Query execution and answer generation

- `POST /ask`
  - blocking JSON response
  - suitable for app/backend callers that want one final payload
- `POST /ask/stream`
  - newline-delimited JSON streaming response
  - suitable for terminal/debug use when you want progress while waiting
  - emits stage events and ends with `event="final"`
  - reuses the exact `/generate-sql` guardrail pipeline first
  - executes SQL only when generation returns `status="ok"`
  - enforces a 50-row execution cap
    - no LIMIT: appends `LIMIT 50`
    - smaller LIMIT: preserved
    - larger LIMIT: capped to 50
  - sends a concise, selected-column result summary to an answer model with `think=false` and a capped answer length
  - falls back to a compact deterministic row summary if the answer model times out or returns malformed output
  - returns natural-language `answer` plus SQL metadata
  - does not return raw rows by default
  - returns HTTP 200 with `status="rejected"` for SQL-generation or MySQL-execution failures

### Manual app DB execution context

- The service runtime `DATABASE_URL` is for the pgvector store (for example, `ragdb` and table `nl2sql_embeddings`).
- Business-row execution should use your app DB connection values (`DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, and target DB name).
- The `/generate-sql` endpoint only generates and validates guardrails; it does not run SQL.
- The `/ask` endpoint executes bounded SELECT SQL on the app DB only after `/generate-sql` returns `status="ok"`.

### Data and index behavior

- Table bootstrap is automatic (`nl2sql_embeddings`)
- HNSW index is enforced at startup
- Legacy IVFFlat index name cleanup is included
- Metadata is stored in JSONB for flexible chunk types

### Schema loader and chunk quality

- `nl2sql_service/schema_loader.py` reads entity, relation, classification, and chunking-rule JSON files from `rag_schema/` on every call — no restart required when files change
- `RAG_SCHEMA_DIR` env var controls the directory (default: repo root `rag_schema/`)
- `validate_loader()` runs at import time; service fails to start if the directory is missing
- Group chunk builder enforces a practical token ceiling (`~400`) to avoid oversized context
- Related table context is automatically derived from relation JSON files in `rag_schema/relations/`
- Group chunk metadata now includes enrichment flags:
  - `has_columns`
  - `has_aliases`
  - `has_examples`
  - `column_source` (`mysql_live` or `unavailable`)

## 3) Current Endpoints

- `GET /health`
- `POST /ingest`
- `POST /query`
- `POST /ingest/groups`
- `POST /ingest/knowledge`
- `POST /query/groups`
- `POST /generate-sql`
- `POST /ask`
- `POST /ask/stream`

## 4) Local Setup

Create and use a virtual environment, then install dependencies:

```bash
python -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

Run the API:

```bash
./.venv/bin/uvicorn nl2sql_service.main:app --host 0.0.0.0 --port 8080
```

> Note: in this repo, scripts are expected to run with `./.venv/bin/python`.

Install as a systemd service on this server:

```bash
cd /var/www/py-workspace/nl2sql
sudo cp deploy/systemd/nl2sql.service /etc/systemd/system/nl2sql.service
sudo systemctl daemon-reload
sudo systemctl enable nl2sql
sudo systemctl restart nl2sql
sudo systemctl status nl2sql
```

Useful service commands:

```bash
sudo systemctl restart nl2sql
sudo systemctl stop nl2sql
sudo journalctl -u nl2sql -f
```

## 5) Required Environment Variables

Create `.env` at repo root:

```env
DATABASE_URL=postgresql://user:password@host:5432/ragdb
EMBEDDING_API_URL=http://embedding-host:8000/embed

# Optional (defaults shown)
EMBEDDING_MODEL=bge-large-en-v1.5
BATCH_SIZE=32
EMBEDDING_DIMENSION=1024
EMBED_TIMEOUT=30
EMBED_MAX_RETRIES=3
EMBED_RETRY_BASE_DELAY=1

# Ollama on local PC via Tailscale.
# Separate from EMBEDDING_API_URL - do not merge.
LLM_PROVIDER=ollama
LLM_BASE_URL=http://100.120.187.84:11434
LLM_MODEL=deepseek-coder:6.7b
LLM_TIMEOUT=60
LLM_MAX_RETRIES=2
REASONING_MODEL=qwen3:4b
REASONING_TEMPERATURE=0.6
REASONING_TIMEOUT=45
REACT_MAX_ITERATIONS=4
ANSWER_MODEL=qwen3:4b
ANSWER_TIMEOUT=45
ANSWER_TEMPERATURE=0.2

# Output SQL syntax only - unrelated to pgvector store engine.
SQL_DIALECT=mysql

# Optional app DB credentials for manual SQL execution checks
# (also used by ingest-time live column enrichment for /ingest/groups)
DB_HOST=localhost
DB_PORT=3306
DB_USER=readonly_user
DB_PASSWORD=***
DB_NAME=pf_TickleRight_9210
# Optional fallback if DB_NAME is not set
# DB_CENTRAL=pf_TickleRight_9210

TOP_K=5
RAG_SCHEMA_DIR=/var/www/py-workspace/nl2sql/rag_schema
NL2SQL_DOCS_DIR=/var/www/py-workspace/nl2sql/ignore_docs_now/docs
```

> `RAG_SCHEMA_DIR` defaults to `rag_schema/` relative to the repo root. Set it explicitly if the service runs from a different working directory.
> `NL2SQL_DOCS_DIR` defaults to `ignore_docs_now/docs` relative to the repo root.
> `LLM_BASE_URL` is the Ollama base URL, not the TEI embedding URL. Ollama generation is called at `{LLM_BASE_URL}/api/generate`.
> `LLM_MODEL` is used by `deepseek-coder` for SQL generation only. `REASONING_MODEL` is used by `qwen3:4b` for ReAct Thought/Action steps.
> `qwen3:4b` is called with top-level `think=true`, `num_predict=800`, and `REASONING_TEMPERATURE=0.6`.
> `REACT_MAX_ITERATIONS` controls the ReAct loop cycles. `LLM_MAX_RETRIES` is retained for legacy compatibility and is not the ReAct loop limit.
> `ANSWER_MODEL` is used by `/ask` answer generation and defaults to `REASONING_MODEL` when not set.
> `ANSWER_TIMEOUT` and `ANSWER_TEMPERATURE` control answer generation only.
> `DB_HOST`/`DB_PORT`/`DB_USER`/`DB_PASSWORD` are for manual app DB verification and are intentionally separate from `DATABASE_URL`.
> The same app DB credentials are also used by `/ingest/groups` and `/generate-sql` to load live column names.
> `/ask` uses the same app DB credentials to execute bounded SQL.

## 6) Quick Usage

Ingest all schema groups:

```bash
./.venv/bin/python scripts/nl2sql_ingest_groups.py --url http://localhost:8080
```

Typical `/ingest/groups` response now includes enrichment counters:

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

Ingest specific groups (by entity ID or short name):

```bash
./.venv/bin/python scripts/nl2sql_ingest_groups.py --url http://localhost:8080 \
  --groups inquiry_lifecycle sales_invoice_billing
```

Ingest enriched knowledge (all enabled sources):

```bash
./.venv/bin/python scripts/nl2sql_ingest_groups.py --url http://localhost:8080 --knowledge
```

Ingest all sources serially in one command (groups + full knowledge):

```bash
./.venv/bin/python scripts/nl2sql_ingest_groups.py --url http://localhost:8080 --all --timeout 600
```

Ingest only SQL examples with a limit:

```bash
./.venv/bin/python scripts/nl2sql_ingest_groups.py --url http://localhost:8080 \
  --knowledge --skip-columns --sql-example-limit 100
```

Ingest knowledge with selected sources (example: SQL + relations only):

```bash
./.venv/bin/python scripts/nl2sql_ingest_groups.py --url http://localhost:8080 \
  --knowledge --skip-columns --skip-graph --skip-view-registry --skip-rules
```

Query groups:

```bash
curl -s -X POST http://localhost:8080/query/groups \
  -H "Content-Type: application/json" \
  -d '{"query":"show unpaid invoices by counselor","top_k":3}'
```

Generate SQL:

```bash
curl -s -X POST http://localhost:8080/generate-sql \
  -H "Content-Type: application/json" \
  -d '{"query":"show unpaid invoices by counselor","top_k":3}' \
  | python3 -m json.tool
```

Ask (generate + execute + answer):

```bash
curl -s -X POST http://localhost:8080/ask \
  -H "Content-Type: application/json" \
  -d '{"query":"newest payment","top_k":5}' \
  | python3 -m json.tool
```

Ask with progress updates:

```bash
curl -N -s -X POST http://localhost:8080/ask/stream \
  -H "Content-Type: application/json" \
  -d '{"query":"show me the 5 most recent inquiries","top_k":3}'
```

Example progress events:

```jsonl
{"event":"started","message":"Received question.","query":"show me the 5 most recent inquiries","top_k":3}
{"event":"sql_generation_started","message":"Retrieving schema context and generating guarded SQL."}
{"event":"sql_generation_running","message":"Still generating and validating SQL."}
{"event":"sql_generation_finished","message":"SQL generated and validated.","sql":"SELECT ...","warnings":[]}
{"event":"execution_started","message":"Executing bounded SQL on the app MySQL database."}
{"event":"execution_finished","message":"SQL execution finished.","row_count":5,"columns":["id","created_at"]}
{"event":"answer_generation_started","message":"Generating final answer from bounded result rows."}
{"event":"answer_generation_running","message":"Still generating final answer."}
{"event":"answer_generation_finished","message":"Final answer is ready.","warnings":[]}
{"event":"final","response":{"status":"ok","answer":"...","sql":"..."}}
```

Manual self-check against app DB (run yourself):

```bash
# 1) Ask your own NL question
curl -s -X POST http://localhost:8080/generate-sql \
  -H "Content-Type: application/json" \
  -d '{"query":"list 20 unpaid invoices with counselor details","top_k":5}' \
  > /tmp/gen.json

# 2) Inspect status and SQL
python3 - <<'PY'
import json
with open('/tmp/gen.json', 'r', encoding='utf-8') as f:
    d = json.load(f)
print('status =', d.get('status'))
print('attempt_count =', d.get('attempt_count'))
print('warnings =', d.get('warnings'))
print('sql =', d.get('sql'))
PY

# 3) If status=ok, execute in app DB (read-only account recommended)
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

Accepted SQL response:

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
        "thought": "I should generate SQL for the billing tables.",
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

Rejected SQL response:

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

## 7) Repository Status: Active vs Legacy Paths

### Active (primary runtime path)

- `nl2sql_service/` (FastAPI + pgvector service)
  - `schema_loader.py` — reads `rag_schema/` JSON files; no restart required on schema changes
  - `react_agent.py` — ReAct planning loop, qwen reasoning calls, action execution, and trace capture
  - `sql_generator.py` — deepseek SQL prompting, SQL extraction, safety/table/column validation, MySQL EXPLAIN, and thin `generate_sql()` wrapper
- `rag_schema/` — PHP-generated entity, relation, graph, and rule JSON files
- `scripts/nl2sql_ingest_groups.py` — pure HTTP client; no service imports

### Legacy or auxiliary tooling

- Qdrant/FastEmbed scripts under `scripts/`:
  - `nl2sql_ingest_qdrant.py`
  - `nl2sql_query_qdrant.py`
  - `nl2sql_generate_gemini.py`
  - `nl2sql_build_corpus.py`
  - `nl2sql_generate_semantic_layer.py`
  - `nl2sql_audit_corpus.py`
  - `nl2sql_validate_sql.py`

These can coexist, but they represent a different pipeline than the current FastAPI + pgvector service.

## 8) What Can Be Done Next (Recommended)

1. Add integration tests for the remaining endpoints (`/health`, `/ingest`, `/query`, `/ingest/groups`, `/ingest/knowledge`, `/query/groups`).
2. Add observability: request IDs, latency metrics, and structured logs for embedding and DB calls.
3. Add a unified command wrapper (`make` or `just`) for setup, run, ingest, and smoke tests.
4. Add a `POST /ingest/groups/status` endpoint to report per-group `schema_version` and last-ingested timestamp without triggering a re-embed.
5. Wire PHP's schema export step into a deploy hook so `rag_schema/` is always in sync with the application DB before ingestion runs.

## 9) Known Operational Notes

- If PostgreSQL is unreachable at startup, the app still boots and endpoints requiring DB return 503 until DB connectivity is restored.
- Embedding endpoint must return vectors with dimension equal to `EMBEDDING_DIMENSION`.
- `metadata` parsing in retrieval already supports both JSONB dicts and stringified JSON.
- Repeat ingest runs are idempotent and now fast for unchanged data: chunks with matching `schema_version` are filtered before embedding.
- SQL generation failures are controlled application responses: `/generate-sql` returns HTTP 200 with `status="rejected"` and warning codes.
- `/ask` failures are controlled application responses: it returns HTTP 200 with `status="rejected"` for SQL-generation and MySQL-execution failures. Answer-model failures return a compact fallback answer with an answer warning when SQL execution succeeded.
- DB or pool failures still return HTTP 503 through the shared `_require_pool` check.
- In many deployments, `DATABASE_URL` points to the vector store only; business tables may live in a separate MySQL DB.
- `/ingest/groups` will still succeed when MySQL app DB is unavailable, but chunk text uses `(columns unavailable)` and `enrichment_summary.groups_without_columns` increases.
- `/generate-sql` validates known columns when live schema is available. If MySQL is unreachable, live column validation is skipped and `MYSQL_EXPLAIN_UNAVAILABLE` is returned as a non-blocking warning.
- `/ask` enforces a max execution bound of 50 rows and does not return raw rows in the API response.
- `/ask/stream` uses the same guardrails as `/ask`, but returns `application/x-ndjson` progress events instead of one JSON object.
- Explicit `GIVE_UP` returns a single terminal warning; loop-exhausted warnings are reserved for actual iteration exhaustion.
- Reasoning-model transport or timeout failures are returned directly and do not append a synthetic loop-exhausted warning.
