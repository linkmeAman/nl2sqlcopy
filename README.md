# NL2SQL Service (Current Setup)

This repository currently contains a production-oriented NL2SQL context service built with FastAPI, PostgreSQL, and pgvector.

It also contains older Qdrant/FastEmbed scripts that are still useful for offline corpus work, but they are not part of the primary runtime path.

## 0) Change Log

- 2026-05-11: Added model-independent governance rulebook injection (`nl2sql_service/rulebook.py`), advisory post-validation SQL review, public governance inspection/validation endpoints, and telemetry visibility for `REVIEW_FAILED`.
- 2026-05-09: Added in-memory retrieval/SQL caches, public cache ops endpoints, structured `/ask` answer prompting with `ANSWER_HALLUCINATION` warnings, short-query rewrite skipping, and the `ModelClient` abstraction for Ollama calls.
- 2026-05-06: Added public browser documentation routes (`/help`, `/help/{module}`, `/help/{module}/{route_slug}`) backed by FastAPI OpenAPI metadata plus curated route docs.
- 2026-05-06: Added developer terminal help browser (`python -m nl2sql_service.help_tui`) with module filters, route search, route detail output, curl examples, and standard-library curses interactive mode.
- 2026-05-02: Added Layer 2 query rewriting before embedding: one fail-open DeepSeek/Ollama call expands business terms such as `counselor` to schema terms such as `employee` before cosine search.
- 2026-05-02: `/ingest/groups` now returns HTTP 200 partial success instead of HTTP 500 when one or more groups exceed the 400-token chunk limit. Failed groups are collected and returned in `failed_groups` / `failure_count` / `partial` response fields; passing groups are still embedded.
- 2026-05-02: Added answer-style config knobs: `ANSWER_STRICT_CONCISE`, `ANSWER_MAX_WORDS`, `ANSWER_MAX_TOKENS`, `ANSWER_ALLOW_REASONING` (all optional, see section 5).
- 2026-05-02: Added smoke matrix script (`scripts/nl2sql_smoke_test.py`) and `make smoke` / `make smoke-report` Makefile targets; it now covers API routes, browser help routes, and terminal help checks.
- 2026-04-30: Extended benchmark replay script with `--output FILE` (exports JSON or CSV report per run) and `--fail-on-slices SLICES` (CI gate — only fails the pipeline when named query-category slices regress).
- 2026-04-29: Added telemetry KPI endpoint (`GET /telemetry/summary`) and benchmark replay script (`scripts/nl2sql_replay_benchmark.py`).
- 2026-04-29: Added ops APIs to inspect telemetry and manage replay benchmark cases (`GET /telemetry/recent`, `POST /benchmark/cases`, `GET /benchmark/cases`).
- 2026-04-29: Added request-level telemetry persistence (`nl2sql_request_events`) and optional `request_id` support for `/generate-sql`, `/ask`, and `/ask/stream`.
- 2026-04-29: Added interactive user-instruction learning with `/teach`, conflict confirmation, instruction review, soft delete, prompt injection, manual instruction embedding, and confidence decay.
- 2026-04-28: Added learned-pattern storage, retrieval injection, manual pattern embedding, and pattern feedback.
- 2026-04-28: Added `clarification_needed` responses for ReAct logic failures after the full loop runs.
- 2026-04-26: Added `POST /ask` for end-to-end ask -> guarded SQL generation -> bounded MySQL execution -> natural-language answer generation.
- 2026-04-26: Added `/ask` settings (`ANSWER_MODEL`, `ANSWER_TIMEOUT`, `ANSWER_TEMPERATURE`) and documented row-cap behavior (max 50 rows).

## 1) Architecture at a Glance

- API service: FastAPI app in `nl2sql_service/main.py`
- Primary vector store: PostgreSQL + pgvector table `nl2sql_embeddings`
- Learned pattern store: PostgreSQL table `nl2sql_learned_patterns`
- User instruction store: PostgreSQL table `nl2sql_user_instructions`
- Request telemetry store: PostgreSQL table `nl2sql_request_events`
- Benchmark case store: PostgreSQL table `nl2sql_benchmark_cases`
- Help/documentation source: `nl2sql_service/help_docs.py` merges FastAPI OpenAPI route metadata with curated human-readable examples and failure notes
- Embedding provider: remote TEI-compatible endpoint (`inputs` payload) configured via env vars
- Query rewrite provider: Ollama `deepseek-coder:6.7b` expands retrieval search text before embedding
- SQL planning provider: Ollama `qwen3:4b` for ReAct Thought/Action decisions
- SQL generation provider: Ollama `deepseek-coder:6.7b` for SQL writing only
- Governance rulebook: `nl2sql_service/rulebook.py` injects deployment-specific hard/soft rules into planner, SQL-generation, and answer prompts when enabled
- LLM provider abstraction: `nl2sql_service/model_client.py`; `LLM_PROVIDER=ollama` is the current supported provider
- Chunking paths:
  - free text chunking
  - schema table chunking
  - schema-group chunking (driven by `rag_schema/` JSON files)
  - enriched knowledge chunking from docs + `rag_schema/` metadata sources

Current serving flow:

1. Request arrives at `/ingest`, `/query`, `/ingest/groups`, `/ingest/knowledge`, `/ingest/patterns`, `/ingest/instructions`, `/query/groups`, `/patterns/feedback`, `/teach`, `/teach/confirm`, `/instructions`, `/generate-sql`, `/ask`, `/ask/stream`, `/cache/stats`, `/cache/clear`, `/governance/rules`, or `/governance/validate`
2. Retrieval requests optionally rewrite the query with DeepSeek/Ollama before embedding; failures and queries with 3 or fewer words fall back to the original query
3. Text is embedded through the configured embedding API for ingest and retrieval paths; retrieval-time embeddings can be served from the in-memory embed cache
4. Vectors are written to or searched from `nl2sql_embeddings`
5. Retrieval results return similarity-scored context and metadata
6. `retrieve_groups()` injects relevant user-provided instructions first, then relevant active learned patterns, then schema context
7. `/generate-sql` checks the in-memory SQL result cache first, then runs a ReAct loop on cache miss: retrieve schema context, inject governance rules when enabled, let `qwen3:4b` choose the next action, call `deepseek-coder:6.7b` only for SQL generation, validate, advisory-review the accepted SQL, and return accepted SQL, clarification, or transport rejection without executing SQL
8. `/generate-sql`, `/ask`, and `/ask/stream` accept optional `request_id` for caller-side correlation (service auto-generates one when omitted)
9. `/ask` reuses `/generate-sql`, executes bounded SQL on app MySQL (max 50 rows), then calls an answer model with structured output plus governance grounding rules to produce natural-language output, with a compact row-based fallback if the answer model fails
10. Successful non-empty `/ask` responses save learned SQL patterns in the background and update instruction outcome counters
11. `/ask/stream` runs the same ask pipeline but emits newline-delimited JSON progress events before the final response
12. Request outcomes are written to `nl2sql_request_events` with endpoint, status, warning codes, stage latency breakdowns, and `metadata.review_failed` when the advisory review gate flags an accepted SQL
13. Ops endpoints can list recent telemetry, inspect/clear in-memory caches, inspect governance rules, validate SQL against the governance reviewer, and persist benchmark cases for replay-gated releases
14. Cache ops endpoints expose current cache size/TTL and allow clearing caches after schema or ingest changes
15. `/help` and `python -m nl2sql_service.help_tui` expose the same OpenAPI-backed route documentation without requiring DB, embedding, MySQL, or Ollama connectivity

## 2) What Has Been Implemented

### Core service

- Async FastAPI app with startup/shutdown lifecycle
- Non-fatal DB startup: service starts even if DB is temporarily unreachable
- Shared async embedding client with retries and timeout handling
- Centralized settings via `.env` (`pydantic-settings`)
- Browser-based in-app route documentation at `/help`
- Developer terminal help browser via `python -m nl2sql_service.help_tui`

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
  - groups that exceed the 400-token chunk ceiling are skipped with a per-group error collected in `failed_groups`; other groups are still embedded
  - returns HTTP 200 even when some groups fail; `partial: true` and non-empty `failed_groups` signal a partial result
  - returns HTTP 500 only for unexpected infrastructure failures (DB unavailable, embedding API unreachable)
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
- `POST /ingest/patterns`
  - manually embeds active learned patterns from `nl2sql_learned_patterns`
  - includes only patterns with `is_active = true` and `use_count >= MIN_PATTERN_USE_COUNT`
  - stores chunks as `metadata.type = "learned_pattern"`
  - upserts by `source = learned_pattern_{id}`, `chunk_index = 0`, and a content-derived `schema_version`
  - intended for cron/manual use after successful `/ask` traffic accumulates; it is not called automatically by `/ask`

### Pattern learning

- Successful `/ask` responses with `row_count > 0` save a learned pattern in the background.
- `save_pattern()` is fire-and-forget via `asyncio.create_task()` and does not add response latency.
- Join conditions are extracted with `sqlparse` from generated SQL joins and stored as JSON.
- `retrieve_groups()` reads relevant active patterns directly from `nl2sql_learned_patterns` and appends them to the ReAct context.
- `POST /patterns/feedback`
  - `helpful=true`: boosts `use_count` by 2
  - `helpful=false`: deactivates the pattern (`is_active=false`)

### Interactive instruction learning

- `POST /teach`
  - saves user-provided database knowledge as structured instructions
  - supported instruction types:
    - `table_relationship`
    - `business_rule`
    - `query_methodology`
    - `term_mapping`
    - `filter_rule`
    - `correction`
  - detects simple structural conflicts before saving
  - stores conflicting instructions in a 30-minute in-memory pending-confirmation queue
  - non-correction instructions start unverified with `confidence_score=0.7`
  - corrections are saved as verified with `confidence_score=1.0` when no matching prior instruction is found
- `POST /teach/confirm`
  - `confirm`: save the pending instruction as verified and keep the old instruction active
  - `replace`: deactivate the conflicting instruction and save the new one as verified
  - `reject`: discard the pending instruction
- `GET /instructions`
  - lists saved instructions for review
  - supports optional `instruction_type` and `active_only` query params
- `DELETE /instructions/{instruction_id}`
  - soft-deletes an instruction by setting `is_active=false`
  - marks the matching embedded copy inactive when one exists
- `POST /ingest/instructions`
  - manually embeds active instructions whose confidence is at least `MIN_INSTRUCTION_CONFIDENCE`
  - this is optional for `/generate-sql`; live prompt injection reads directly from `nl2sql_user_instructions`
- During SQL generation, relevant user instructions are injected under `USER-PROVIDED RULES` before learned patterns and schema context.
- Instruction outcome counters are updated in the background: successful generated SQL increases `success_count`; failed ReAct paths increase `failure_count` and decay confidence by 0.1 after repeated failures, with a floor of 0.3.

### Retrieval

- `POST /query`
  - cosine similarity retrieval across all chunk types (`schema_group`, `column_catalog`, `sql_example`, etc.)
  - rewrites the query before embedding when `QUERY_REWRITE_ENABLED=true`
  - skips rewrite for short queries of 3 or fewer words, such as `newest payment`
  - uses the in-memory embed cache when `EMBED_CACHE_ENABLED=true`
- `POST /query/groups`
  - retrieves only `metadata.type = schema_group`
  - rewrites the query before embedding while preserving the original query for instruction/pattern selection
  - skips rewrite for short queries of 3 or fewer words
  - prepends relevant user instructions under `USER-PROVIDED RULES` when available
  - appends relevant learned patterns under `PREVIOUSLY LEARNED PATTERNS` when available
  - context priority is: user instructions > learned patterns > schema group context
  - returns:
    - `matched_groups`
    - `tables_in_scope`
    - composed `context` block
    - raw `results`

### SQL generation

- `POST /generate-sql`
  - returns cached successful SQL when `SQL_CACHE_ENABLED=true` and the same normalized query/top_k pair is still within TTL
  - reuses `retrieve_groups()` to select matched schema groups and tables in scope
  - injects governance rulebook instructions into ReAct planning and SQL-generation prompts when `GOVERNANCE_ENABLED=true`
  - follows relevant user-provided rules from the retrieved context before model defaults
  - receives relevant learned patterns in the retrieved context when available
  - loads live MySQL columns for retrieved tables when app DB credentials are configured
  - uses `qwen3:4b` as a ReAct planning agent with hybrid thinking enabled (`think=true`)
  - uses `deepseek-coder:6.7b` only for the `GENERATE_SQL` action
  - generates MySQL-compatible SELECT syntax by default (`SQL_DIALECT=mysql`)
  - validates that output is one read-only SELECT or WITH...SELECT statement
  - rejects destructive SQL, multi-statement SQL, empty output, non-SELECT SQL, tables outside the retrieved scope, unknown columns, and blocking MySQL EXPLAIN errors
  - ignores destructive-looking words inside comments and string literals during safety checks
  - allows CTE aliases without treating them as unknown tables
  - can retrieve more context, fetch schema, regenerate SQL, validate, ask for clarification, or give up within `REACT_MAX_ITERATIONS`
  - preserves the request `top_k` across `RETRIEVE_MORE_CONTEXT` refinement steps
  - refreshes the known column set whenever `RETRIEVE_MORE_CONTEXT` changes the tables in scope
  - validates generated SQL immediately in the same ReAct iteration; simple valid queries usually return with `attempt_count=1`
  - SQL retries include the prior SQL, blocking validation errors, and the planner instruction before regenerating
  - refinement retries are strict: they must correct listed validation errors, avoid disallowed tables/columns from prior attempts, and honor guardrails over planner hints
  - runs an advisory LLM review after static validators and MySQL EXPLAIN pass; review failures add `REVIEW_FAILED` warnings but never change `status="ok"`
  - treats `MYSQL_EXPLAIN_UNAVAILABLE` as a non-blocking warning
  - returns a `react_trace` in success, clarification, and rejected responses
  - includes `cache_hit`; fresh generation returns `false`, cached success returns `true`
  - never executes generated SQL
  - returns HTTP 200 for accepted SQL (`status="ok"`), ReAct logic failures (`status="clarification_needed"`), and transport failures (`status="rejected"`)
  - returns `status="rejected"` only for Ollama timeout/upstream/malformed failures where the model did not run
  - returns `status="clarification_needed"` after the ReAct loop ends with `GIVE_UP`, `ASK_CLARIFICATION`, or iteration exhaustion
  - has no pre-loop confidence check; best-effort SQL generation is always attempted first
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
  - returns `status="clarification_needed"` without executing SQL when `/generate-sql` needs clarification
  - enforces a 50-row execution cap
    - no LIMIT: appends `LIMIT 50`
    - smaller LIMIT: preserved
    - larger LIMIT: capped to 50
  - sends a strict answer template with the first 10 result rows to an answer model with `think=false` and a capped token budget
  - appends governance grounding rules to the answer prompt when `GOVERNANCE_ENABLED=true`
  - parses `ANSWER`, `KEY FIGURES`, and `DETAILS` sections into a compact natural-language answer
  - warns with `ANSWER_HALLUCINATION` if the answer contains numbers not found in returned row values
  - can also surface `REVIEW_FAILED` from the underlying `/generate-sql` success path
  - falls back to a compact deterministic row summary if the answer model times out or returns malformed output
  - returns natural-language `answer` plus SQL metadata
  - does not return raw rows by default
  - returns HTTP 200 with `status="rejected"` for SQL-generation transport failures or MySQL-execution failures
  - saves a learned pattern in the background only when the final response is `status="ok"` and `row_count > 0`

### Manual app DB execution context

- The service runtime `DATABASE_URL` is for the pgvector store (for example, `ragdb` and table `nl2sql_embeddings`).
- Business-row execution should use your app DB connection values (`DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, and target DB name).
- The `/generate-sql` endpoint only generates and validates guardrails; it does not run SQL.
- The `/ask` endpoint executes bounded SELECT SQL on the app DB only after `/generate-sql` returns `status="ok"`.

### Data and index behavior

- Table bootstrap is automatic (`nl2sql_embeddings`, `nl2sql_learned_patterns`, `nl2sql_user_instructions`)
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

Documentation/help routes:

- `GET /help`
- `GET /help/{module}`
- `GET /help/{module}/{route_slug}`

API routes:

- `GET /health`
- `GET /telemetry/recent`
- `GET /telemetry/summary`
- `GET /cache/stats`
- `GET /governance/rules`
- `POST /cache/clear`
- `POST /governance/validate`
- `POST /benchmark/cases`
- `GET /benchmark/cases`
- `POST /ingest`
- `POST /query`
- `POST /ingest/groups`
- `POST /ingest/knowledge`
- `POST /ingest/patterns`
- `POST /ingest/instructions`
- `POST /query/groups`
- `POST /patterns/feedback`
- `POST /teach`
- `POST /teach/confirm`
- `GET /instructions`
- `DELETE /instructions/{instruction_id}`
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
# Answer style controls (optional)
# ANSWER_STRICT_CONCISE=false   # enforce word cap and strip chain-of-thought
# ANSWER_MAX_WORDS=80           # word cap enforced when ANSWER_STRICT_CONCISE=true
# ANSWER_MAX_TOKENS=300         # num_predict sent to the answer model
# ANSWER_ALLOW_REASONING=false  # pass think=true to the answer model (qwen3 only)

# Governance / rulebook system (optional, defaults shown)
GOVERNANCE_ENABLED=true
GOVERNANCE_ENABLED_RULES=all
GOVERNANCE_INJECT_REACT=true
GOVERNANCE_INJECT_SQL=true
GOVERNANCE_INJECT_ANSWER=true

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
EMBED_CACHE_ENABLED=true
EMBED_CACHE_TTL_SECONDS=1800
SQL_CACHE_ENABLED=true
SQL_CACHE_TTL_SECONDS=300
MIN_PATTERN_USE_COUNT=2
MIN_INSTRUCTION_CONFIDENCE=0.5
QUERY_REWRITE_ENABLED=true
QUERY_REWRITE_MODEL=deepseek-coder:6.7b
QUERY_REWRITE_TIMEOUT=8
QUERY_REWRITE_MAX_TOKENS=120
QUERY_REWRITE_HINTS="counselor,counsellor,counsellors -> employee"
RAG_SCHEMA_DIR=/var/www/py-workspace/nl2sql/rag_schema
NL2SQL_DOCS_DIR=/var/www/py-workspace/nl2sql/ignore_docs_now/docs
```

> `RAG_SCHEMA_DIR` defaults to `rag_schema/` relative to the repo root. Set it explicitly if the service runs from a different working directory.
> `NL2SQL_DOCS_DIR` defaults to `ignore_docs_now/docs` relative to the repo root.
> `LLM_BASE_URL` is the Ollama base URL, not the TEI embedding URL. Ollama generation is called at `{LLM_BASE_URL}/api/generate`.
> `LLM_PROVIDER=ollama` selects the current model client implementation. All LLM call sites go through `nl2sql_service/model_client.py`.
> `LLM_MODEL` is used by `deepseek-coder` for SQL generation only. `REASONING_MODEL` is used by `qwen3:4b` for ReAct Thought/Action steps.
> `qwen3:4b` is called with top-level `think=true`, `num_predict=800`, and `REASONING_TEMPERATURE=0.6`.
> `REACT_MAX_ITERATIONS` controls the ReAct loop cycles. `LLM_MAX_RETRIES` is retained for legacy compatibility and is not the ReAct loop limit.
> `ANSWER_MODEL` is used by `/ask` answer generation and defaults to `REASONING_MODEL` when not set.
> `ANSWER_TIMEOUT` and `ANSWER_TEMPERATURE` control answer generation only.
> `/ask` uses a structured answer template and parses `ANSWER`, `KEY FIGURES`, and `DETAILS`; `ANSWER_STRICT_CONCISE=true` still enforces the final word cap after parsing.
> `ANSWER_ALLOW_REASONING=true` passes `think=true` to the answer model (for qwen3 models); leave false when using non-reasoning models.
> `GOVERNANCE_ENABLED=false` disables prompt-level governance injection, disables the advisory SQL review gate, and makes `/governance/*` return HTTP 503.
> `GOVERNANCE_ENABLED_RULES=all` enables all 10 rulebook rules. You can toggle individual rules by listing the enabled rule names explicitly.
> `GOVERNANCE_INJECT_REACT`, `GOVERNANCE_INJECT_SQL`, and `GOVERNANCE_INJECT_ANSWER` independently control prompt injection for planner, SQL-generation, and answer prompts.
> `MIN_PATTERN_USE_COUNT` controls which learned patterns are injected into prompts and embedded by `/ingest/patterns`.
> `MIN_INSTRUCTION_CONFIDENCE` controls which user instructions are injected into prompts and embedded by `/ingest/instructions`.
> `QUERY_REWRITE_*` controls fail-open query expansion before embedding; the rewritten text is used only for cosine search, not SQL prompting. Queries with 3 or fewer words skip rewrite.
> `EMBED_CACHE_*` controls the retrieval-time in-memory embedding cache. Ingest embeddings are not cached.
> `SQL_CACHE_*` controls the in-memory `/generate-sql` cache. Only `status="ok"` SQL generation responses are cached.
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

Partial ingest response (one group exceeded the token limit):

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

Open browser route help:

```bash
curl -s http://localhost:8080/help
```

Use terminal route help:

```bash
python -m nl2sql_service.help_tui --plain
python -m nl2sql_service.help_tui --module generation --plain
python -m nl2sql_service.help_tui --route generation/ask --plain
python -m nl2sql_service.help_tui --search sql --plain
```

Interactive terminal help:

```bash
python -m nl2sql_service.help_tui
```

Interactive keys:
- `↑` / `↓` or `j` / `k`: move
- `Enter`: open route details
- `/`: search
- `1`-`5`: switch modules
- `a`: all routes
- `b`: back/reset
- `q`: quit

Inspect recent telemetry events:

```bash
curl -s 'http://localhost:8080/telemetry/recent?limit=20&endpoint=/ask' \
  | python3 -m json.tool
```

Inspect telemetry summary KPIs (last 60 minutes):

```bash
curl -s 'http://localhost:8080/telemetry/summary?endpoint=/ask&since_minutes=60' \
  | python3 -m json.tool
```

Inspect the active governance rulebook:

```bash
curl -s http://localhost:8080/governance/rules | python3 -m json.tool
```

Validate one SQL statement against the advisory governance reviewer:

```bash
curl -s -X POST http://localhost:8080/governance/validate \
  -H "Content-Type: application/json" \
  -d '{"sql":"SELECT id FROM invoice WHERE status='\''unpaid'\''","query":"show unpaid invoices","tables_in_scope":["invoice"]}' \
  | python3 -m json.tool
```

Add a benchmark case for replay:

```bash
curl -s -X POST http://localhost:8080/benchmark/cases \
  -H "Content-Type: application/json" \
  -d '{"query":"newest payment","expected_status":"ok","slices":["single_table"]}' \
  | python3 -m json.tool
```

Replay benchmark cases against `/generate-sql`:

```bash
./.venv/bin/python scripts/nl2sql_replay_benchmark.py --url http://localhost:8080 --limit 100
```

Export a JSON report after replay:

```bash
./.venv/bin/python scripts/nl2sql_replay_benchmark.py \
  --url http://localhost:8080 \
  --output reports/replay-$(date +%F).json
```

Gate a CI pipeline — fail only when `join` or `aggregation` slice cases regress:

```bash
./.venv/bin/python scripts/nl2sql_replay_benchmark.py \
  --url http://localhost:8080 \
  --fail-on-slices join,aggregation \
  --output reports/replay-$(date +%F).json
```

Run the smoke matrix (API routes, browser help routes, and terminal help checks; exits non-zero on any failure):

```bash
make smoke
# or with a JSON report:
make smoke-report
# or directly:
./.venv/bin/python scripts/nl2sql_smoke_test.py --url http://localhost:8080
```

Teach a table relationship:

```bash
curl -s -X POST http://localhost:8080/teach \
  -H "Content-Type: application/json" \
  -d '{"instruction_type":"table_relationship","content":"employee.contact_id = contact.id","tables_affected":["employee","contact"]}' \
  | python3 -m json.tool
```

Teach a term mapping:

```bash
curl -s -X POST http://localhost:8080/teach \
  -H "Content-Type: application/json" \
  -d '{"instruction_type":"term_mapping","content":"counselor means employee table","tables_affected":["employee"]}' \
  | python3 -m json.tool
```

Inspect what the system has learned from you:

```bash
curl -s 'http://localhost:8080/instructions?active_only=true' \
  | python3 -m json.tool
```

Check whether rules are being injected into generation context:

```bash
curl -s -X POST http://localhost:8080/query/groups \
  -H "Content-Type: application/json" \
  -d '{"query":"show counselor contact details","top_k":3}' \
  | python3 -m json.tool
```

Manually embed user instructions for mixed vector retrieval and inspection:

```bash
curl -s -X POST http://localhost:8080/ingest/instructions \
  -H "Content-Type: application/json" \
  -d '{}' \
  | python3 -m json.tool
```

Manually embed learned patterns after successful `/ask` traffic has accumulated:

```bash
curl -s -X POST http://localhost:8080/ingest/patterns \
  -H "Content-Type: application/json" \
  -d '{}' \
  | python3 -m json.tool
```

Mark a learned pattern as helpful:

```bash
curl -s -X POST http://localhost:8080/patterns/feedback \
  -H "Content-Type: application/json" \
  -d '{"pattern_id":1,"helpful":true}' \
  | python3 -m json.tool
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
print('question =', d.get('question'))
print('suggestions =', d.get('suggestions'))
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

Clarification response:

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

Transport rejected response:

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
  "react_trace": null
}
```

## 7) Repository Status: Active vs Legacy Paths

### Active (primary runtime path)

- `nl2sql_service/` (FastAPI + pgvector service)
  - `schema_loader.py` — reads `rag_schema/` JSON files; no restart required on schema changes
  - `react_agent.py` — ReAct planning loop, qwen reasoning calls, action execution, and trace capture
  - `sql_generator.py` — deepseek SQL prompting, SQL extraction, safety/table/column validation, MySQL EXPLAIN, and thin `generate_sql()` wrapper
  - `help_docs.py` — OpenAPI-backed route documentation registry shared by browser and terminal help
  - `help_tui.py` — standard-library terminal help browser for route discovery and detail views
  - `pattern_store.py` — learned pattern persistence, join extraction, prompt formatting, and feedback lookup helpers
  - `instruction_store.py` — user instruction persistence, conflict checks, confirmation tokens, prompt formatting, and confidence outcome tracking
- `rag_schema/` — PHP-generated entity, relation, graph, and rule JSON files
- `scripts/nl2sql_ingest_groups.py` — pure HTTP client; no service imports
- `scripts/help_tui.py` — direct script wrapper for the terminal help browser

### Legacy or auxiliary tooling

- Qdrant/FastEmbed scripts under `scripts/`:
  - `nl2sql_ingest_qdrant.py`
  - `nl2sql_query_qdrant.py`
  - `nl2sql_generate_gemini.py`
  - `nl2sql_build_corpus.py`
  - `nl2sql_generate_semantic_layer.py`
  - `nl2sql_audit_corpus.py`
  - `nl2sql_validate_sql.py`
  - `nl2sql_replay_benchmark.py` (replay benchmark cases against `/generate-sql`)

These can coexist, but they represent a different pipeline than the current FastAPI + pgvector service.

## 8) What Can Be Done Next (Recommended)

1. Add integration tests for the remaining endpoints (`/health`, `/ingest`, `/query`, `/ingest/groups`, `/ingest/knowledge`, `/query/groups`).
2. Add observability: request IDs, latency metrics, and structured logs for embedding and DB calls.
3. Add authentication or network controls if the route help pages are exposed beyond trusted developer environments.
4. Add CI coverage that runs the smoke matrix against a deployed staging service after schema refreshes.
5. Wire PHP's schema export step into a deploy hook so `rag_schema/` is always in sync with the application DB before ingestion runs.

## 9) Known Operational Notes

- If PostgreSQL is unreachable at startup, the app still boots and endpoints requiring DB return 503 until DB connectivity is restored.
- Embedding endpoint must return vectors with dimension equal to `EMBEDDING_DIMENSION`.
- `metadata` parsing in retrieval already supports both JSONB dicts and stringified JSON.
- Repeat ingest runs are idempotent and now fast for unchanged data: chunks with matching `schema_version` are filtered before embedding.
- SQL generation failures are controlled application responses: `/generate-sql` returns HTTP 200 with `status="clarification_needed"` for ReAct logic failures and `status="rejected"` for Ollama transport/upstream/malformed failures.
- Governance review failures are advisory only: accepted SQL can include `REVIEW_FAILED` warnings while still returning `status="ok"`.
- `/ask` failures are controlled application responses: it returns HTTP 200 with `status="clarification_needed"` for SQL-generation logic failures and `status="rejected"` for transport or MySQL-execution failures. Answer-model failures return a compact fallback answer with an answer warning when SQL execution succeeded.
- DB or pool failures still return HTTP 503 through the shared `_require_pool` check.
- In many deployments, `DATABASE_URL` points to the vector store only; business tables may live in a separate MySQL DB.
- `/ingest/groups` will still succeed when MySQL app DB is unavailable, but chunk text uses `(columns unavailable)` and `enrichment_summary.groups_without_columns` increases.
- `/generate-sql` validates known columns when live schema is available. If MySQL is unreachable, live column validation is skipped and `MYSQL_EXPLAIN_UNAVAILABLE` is returned as a non-blocking warning.
- `/ask` enforces a max execution bound of 50 rows and does not return raw rows in the API response.
- `/ask` saves learned patterns only after successful non-empty execution (`status="ok"` and `row_count > 0`), using a background task.
- `/ask/stream` uses the same guardrails as `/ask`, but returns `application/x-ndjson` progress events instead of one JSON object.
- Explicit `GIVE_UP` and loop exhaustion return `clarification_needed` after the full ReAct loop path has run.
- Reasoning-model transport or timeout failures are returned directly as `rejected` and do not append a synthetic loop-exhausted warning.
- `/governance/rules` and `/governance/validate` are public read-only ops endpoints, but both return HTTP 503 with `Governance disabled` when `GOVERNANCE_ENABLED=false`.
- Learned patterns can be manually embedded with `/ingest/patterns`; this is optional and not part of the `/ask` response path.
- Pattern feedback can boost useful patterns or deactivate bad ones through `/patterns/feedback`.
- User-provided instructions are injected before learned patterns and schema context when active and above `MIN_INSTRUCTION_CONFIDENCE`.
- `/teach` conflict tokens are in-memory only, expire after 30 minutes, and do not survive process restart.
- `/ingest/instructions` is optional for SQL generation because live instruction injection reads directly from `nl2sql_user_instructions`.
