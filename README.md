# NL2SQL Service

FastAPI service for retrieval-augmented NL2SQL generation, bounded query
execution, interactive teaching, trace telemetry, and version-aware ingest.

## Current Scope

- PostgreSQL + pgvector store for retrieval, traces, failure logs, and persistent query cache
- MySQL app DB introspection for column validation and bounded `/ask` execution
- ReAct SQL generation with validation and optional governance review
- Provider-agnostic LLM layer for SQL, reasoning, query rewrite, and answers
- Provider-agnostic embedding layer with the existing custom HTTP embedding server as the default
- Streaming Ask path with live NDJSON progress and trace events
- Interactive instruction learning via `/teach` and `/teach/confirm`, with DB-backed pending confirmations
- Version-aware ingest routes that skip unchanged chunks
- In-memory exact/semantic caches plus DB-backed exact/semantic query cache

## Provider-Agnostic Models

All model calls go through `nl2sql_service/llm/`. Business logic depends on the
unified provider interface instead of provider SDKs directly. Switching models
should be an environment/config change, not a code change.

Canonical LLM layer entry points:

- `nl2sql_service.llm` - public import surface for `get_model_client`,
  `LLMFactory`, `LLMRequest`, `LLMResponse`, `LLMChunk`, `GenerateInput`, and
  `ProviderConfig`
- `nl2sql_service.llm.factory` - provider selection, role routing, and fallback chains
- `nl2sql_service.llm.interfaces` - canonical shared request/response/provider dataclasses and interfaces
- `nl2sql_service.llm.providers.*` - concrete provider implementations
- `nl2sql_service.llm.adapters.openai` - normalized OpenAI-compatible HTTP adapter

Compatibility note:

- `nl2sql_service.llm.types` remains only as a thin re-export for older imports.
  It is not a second source of truth for LLM dataclasses.

Supported LLM providers:

- `ollama`
- `openai`
- `anthropic`
- `gemini`
- `groq`
- `openrouter`
- `togetherai`

Supported embedding providers:

- `custom` - current external bge/TEI-style HTTP embedding endpoint
- `openai`
- `gemini`
- `ollama`
- `voyageai`

Role-specific routing lets different workloads use different models:

```env
LLM_PROVIDER=ollama
LLM_MODEL=deepseek-coder:6.7b

SQL_MODEL_PROVIDER=anthropic
SQL_MODEL=claude-sonnet-4
SQL_MODEL_API_KEY=env:ANTHROPIC_API_KEY

REASONING_MODEL_PROVIDER=openai
REASONING_MODEL=gpt-4.1-mini
REASONING_MODEL_API_KEY=env:OPENAI_API_KEY

QUERY_REWRITE_MODEL_PROVIDER=groq
QUERY_REWRITE_MODEL=llama-3.3-70b-versatile
QUERY_REWRITE_FAST_MODEL=
QUERY_REWRITE_MODEL_API_KEY=env:GROQ_API_KEY

ANSWER_MODEL_PROVIDER=openrouter
ANSWER_MODEL=openai/gpt-4.1-mini
ANSWER_MODEL_API_KEY=file:/run/secrets/openrouter_api_key
```

Fallbacks are configured independently:

```env
LLM_FALLBACK_PROVIDER=openai
LLM_FALLBACK_MODEL=gpt-4.1-mini
LLM_FALLBACK_API_KEY=env:OPENAI_API_KEY

SQL_FALLBACK_PROVIDER=ollama
SQL_FALLBACK_MODEL=deepseek-coder:6.7b
```

Secrets are never hardcoded. Config values can be raw env values, `env:NAME`,
or `file:/path/to/secret` for Docker/Kubernetes secret mounts.

Runtime routing is separate from the env file. Use `/config/model-routing` to
inspect or patch the full active process routing at runtime, or
`/config/ask-model` when you only want to change the model used for `/ask`
answer generation. Runtime route changes are not persisted across restart; edit
`.env` when you want new startup defaults. For persistent DB-backed role
defaults, use `/config/active-model/{role}` after registering providers and
models in the provider registry.

## Key Runtime Paths

- `GET /help`, `GET /help/{module}`, `GET /help/{module}/{route_slug}`
  - render in-app HTML route documentation for operators and integrators
- `GET /health`, `GET /health/config`, `GET /health/runtime`, `GET /health/llm`, `GET /health/vector`
  - expose compact and detailed readiness for provider config, MySQL execution,
    schema assets, vector connectivity, and role-specific LLM health
- `GET /config/model-routing`, `PATCH /config/model-routing`
  - inspect and patch the live task-to-model routing used by SQL, reasoning,
    query rewrite, answer, embedding, and fallback selection
- `GET /config/ask-model`, `PATCH /config/ask-model`
  - inspect and patch only the model used by `/ask` and `/ask/stream` answer
    generation
- `GET /providers`, `POST /providers`, `GET /providers/{id}`, `PATCH /providers/{id}`, `DELETE /providers/{id}`
  - manage DB-backed provider records including local endpoints and cloud base URLs
- `POST /providers/{id}/test`, `GET /providers/{id}/models`
  - probe provider connectivity and list discoverable models without exposing raw secrets
- `POST /providers/{id}/keys`, `GET /providers/{id}/keys`, `DELETE /providers/{id}/keys/{key_id}`
  - manage encrypted provider API keys; responses expose only hash prefixes for operator identification
- `GET /model-registry`, `POST /model-registry`, `PATCH /model-registry/{id}`, `DELETE /model-registry/{id}`, `POST /model-registry/{id}/set-default`, `GET /model-registry/default`
  - manage DB-backed model registry entries and persistent per-role defaults
- `PATCH /config/active-model/{role}`
  - persist a role's default model in the DB registry and patch the live process routing immediately
- `GET /metrics/llm`, `GET /metrics/teach`, `GET /metrics/prometheus`
  - surface provider usage metrics, teach-confirmation operational drift, and Prometheus-formatted backend observability metrics
- `GET /logs/days`, `GET /logs/recent`, `GET /logs/stream`
  - inspect repo-local day-wise JSON log files and tail the active log over HTTP
- `GET /telemetry/recent`, `GET /telemetry/summary`, `GET /telemetry/trace/{request_id}`
  - inspect request outcomes, aggregate KPIs, and per-request stage traces
- `GET /failures`
  - list recent failed requests with pre-built teach suggestions
- `GET /cache/stats`, `POST /cache/clear`
  - inspect cache sizes/TTLs and clear in-memory plus DB-backed query cache state
- `GET /governance/rules`, `POST /governance/validate`
  - inspect active governance rules and run the standalone SQL review gate
- `POST /benchmark/cases`, `GET /benchmark/cases`
  - persist and list benchmark cases for replay/regression gates
- `GET /ingest/groups/status`
  - compare current schema group hashes against embedded versions
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
- `GET /health`, `GET /health/config`, `GET /health/runtime`, `GET /health/llm`, `GET /health/vector`, `GET /metrics/llm`, `GET /metrics/teach`
  - validate provider connectivity and inspect provider/model usage
  - `GET /health` now also surfaces compact teach-confirmation warning status for monitoring
- `POST /teach`, `POST /teach/confirm`
  - save or resolve user-provided instructions
  - pending confirmations persist across service restarts until their 30-minute TTL expires
- `GET /teach/pending`, `POST /teach/pending/cleanup`
  - inspect and explicitly clean up pending teach confirmations
  - `/metrics/teach` includes threshold-based alerts for backlog and expired-token drift
- `GET /instructions`, `DELETE /instructions/{instruction_id}`
  - review or deactivate saved instructions
- `POST /patterns/feedback`
  - boost or deactivate learned patterns from downstream feedback
- `POST /query`, `POST /query/groups`
  - run raw retrieval or schema-group retrieval without SQL generation
- `POST /ingest`, `POST /ingest/groups`, `POST /ingest/knowledge`, `POST /ingest/patterns`, `POST /ingest/instructions`
  - ingest free text, schema groups, enriched knowledge, learned patterns, and user instructions

## NL2SQL Evaluation CLI

Production evaluation runs live in `scripts/nl2sql_evaluate.py` and are designed to diagnose
why a request failed, not just whether it failed.

It reuses the existing backend lifecycle:

- `POST /ask` and `POST /ask/stream` for execution
- `GET /telemetry/trace/{request_id}` for full trace collection
- `GET /failures` for failure-log enrichment
- `POST /benchmark/cases` when `--sync-db` is enabled

Benchmark suites live in `benchmarks/` and are split by difficulty:

- `level1_basic.json`
- `level2_intermediate.json`
- `level3_advanced.json`
- `level4_expert.json`
- `level5_stress.json`

Each case carries expected tables, keywords, SQL characteristics, and failure hints so the analyzer can
classify the root cause into retrieval, chunking, reranking, schema retrieval, planning, SQL generation,
SQL validation, execution, answer generation, cache, or provider failure buckets.

Typical usage:

```bash
./.venv/bin/python scripts/nl2sql_evaluate.py \
  --url http://localhost:8080 \
  --benchmarks benchmarks \
  --output-dir reports/evaluation \
  --parallel 4 \
  --require-ready
```

Outputs:

- `reports/evaluation/evaluation_failures.jsonl`
- `reports/evaluation/evaluation_summary.json`

The `Makefile` target `make evaluate` runs the same CLI with the default local service URL.

## Current ReAct Planner

The current ReAct planner is no longer a single repeated retrieval loop.

It now uses:

- `RETRIEVE_PAST_CORRECTIONS` on the first iteration to pull similar teach corrections
- `RETRIEVE_SCHEMA_FOR_TABLES` for targeted schema retrieval
- `RETRIEVE_JOIN_PATHS` when multiple tables need relation evidence
- `RETRIEVE_SAMPLE_QUERIES` when ambiguity remains high
- a duplicate-action guard keyed by `(action, target)` and retrieved tables
- a `context_confidence_score` to decide when retrieval is sufficient

Important runtime behavior:

- iteration 1 bootstraps correction memory and initial schema retrieval together before iteration 2 planning begins
- past corrections are injected into learned instructions before SQL planning
- when broad group retrieval leaves too many tables in scope, SQL generation is focused down to the top `SQL_GENERATION_MAX_TABLES` tables by column similarity
- if broad scope exceeds that cap and no column-level schema was refreshed yet, the planner runs a focused `RETRIEVE_SCHEMA_FOR_TABLES` step before allowing `GENERATE_SQL`
- duplicate retrieval of the same table/action is blocked within a request
- retrieval/setup actions do not consume the same retry budget as SQL-generation attempts
- ReAct trace `details` include `context_confidence_score` and `context_confidence_details`
- a `sql_context_focus` trace event records `tables_before_focus`, `tables_after_focus`, and `column_hits_used` before SQL generation
- planner logic remains schema-driven and does not hardcode database-specific table names

## Deterministic Fast Path

The deterministic SQL path is no longer enabled by query keywords alone.

It now requires all of the following:

- the query embedding's top column-similarity hits all belong to one table
- no top column hit points to another table, which would imply a join
- the candidate table from the top column hit is confirmed by retrieved schema context
  such as `tables_in_scope` from schema-group retrieval

If any top column hit belongs to another table, or if the candidate table is not
confirmed by retrieved schema context, the service skips deterministic SQL and
falls back to the normal ReAct planner.

## Streaming Ask

Example:

```bash
curl -N -s -X POST http://localhost:8080/ask/stream \
  -H "Content-Type: application/json" \
  -d '{"query":"newest payment","top_k":5,"request_id":"req-demo"}'
```

Example NDJSON lines:

```json
{"event":"started","request_id":"req-demo","trace_id":"trace-demo","workflow_id":"req-demo","message":"Received question.","query":"newest payment","top_k":5}
{"event":"trace","request_id":"req-demo","trace_id":"trace-demo","workflow_id":"req-demo","seq":2,"layer":"nl2sql-service","stage":"schema_retrieval","status":"completed","message":"Schema-group vector search completed.","provider":"custom","model":"bge-large-en-v1.5","duration_ms":42,"warning_codes":[],"error_source":null,"output_summary":{"matched_groups":["billing"],"selected_tables":["payment"]},"metadata":{"details":{"top_k":5}}}
{"event":"final","request_id":"req-demo","trace_id":"trace-demo","workflow_id":"req-demo","response":{"status":"ok","request_id":"req-demo","trace_id":"trace-demo","workflow_id":"req-demo","answer":"...","sql":"SELECT ...","warnings":[]}}
```

The stream has explicit service timeout handling. If SQL generation, MySQL
execution, or answer generation exceeds the Ask budget, the service emits a
`timeout` event followed by a `final` rejected response with
`REQUEST_TIMEOUT`.

Schema-confirmed single-table deterministic queries bypass the answer LLM and
return a direct fallback answer. This path is intended for simple requests where
column-level similarity signals prove the answer can be satisfied from one table
without joins. If that proof fails, the final answer step still uses the
configured answer model and can be slower.

## Documentation Maintenance

For any major behavioral change, routing change, new config key, planner action
change, cache strategy change, or observability event change:

- update `README.md`
- update `ROUTES.md`
- update operator-facing architecture docs when the runtime contract changes

Treat documentation updates as part of the code change, not a follow-up task.

## Provider Management

The provider layer now supports an optional DB-backed registry in addition to
the existing env-based configuration.

- env config still works as the fallback path and remains required for bootstrap-only deployments
- DB-backed defaults win when a role is explicitly switched through the provider registry flow
- encrypted provider keys are stored in `nl2sql_llm_api_keys`; raw keys never appear in API responses, logs, or trace payloads
- `PROVIDER_KEY_ENCRYPTION_SECRET` must be configured before `POST /providers/{id}/keys` is allowed
- the live process can still be patched ephemerally through `/config/model-routing`; `/config/active-model/{role}` is the persistent path

## Trace Events

Trace events are persisted in `nl2sql_trace_events` and exposed through:

```bash
curl -s http://localhost:8080/telemetry/trace/req-demo?limit=500 | python -m json.tool
```

Shared event shape:

```json
{
  "request_id": "req-demo",
  "trace_id": "otel-trace-id",
  "correlation_id": "corr-123",
  "session_id": "session-123",
  "workflow_id": "req-demo",
  "seq": 1,
  "event": "request_received",
  "layer": "nl2sql-service",
  "stage": "request_received",
  "status": "started",
  "message": "Received ask request.",
  "span_id": "otel-span-id",
  "parent_span_id": null,
  "duration_ms": null,
  "provider": null,
  "model": null,
  "retry_count": 0,
  "reasoning_summary": null,
  "input_summary": {
    "query_preview": "newest payment",
    "top_k": 5
  },
  "output_summary": {},
  "warning_codes": [],
  "error_source": null,
  "token_usage": {},
  "errors": [],
  "details": {},
  "metadata": {},
  "started_at": "2026-06-02T08:00:00Z",
  "ended_at": null,
  "schema_version": "nl2sql.observability.v1",
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

The current backend implementation also adds:

- request-scoped `trace_id`, `correlation_id`, `session_id`, and `workflow_id`
- async trace persistence through the in-process observability pipeline
- safe `reasoning_summary`, `input_summary`, `output_summary`, and `token_usage`
- provider and fallback instrumentation for LLM and embedding calls
- Prometheus metrics for request, stage, retrieval, and provider activity
- optional OpenTelemetry bootstrap through the `otel_*` settings

## Observability Configuration

Relevant backend settings now include:

```env
OBSERVABILITY_ENABLED=true
OBSERVABILITY_SERVICE_NAME=nl2sql-api
OBSERVABILITY_QUEUE_SIZE=5000
OBSERVABILITY_BATCH_SIZE=50
OBSERVABILITY_FLUSH_INTERVAL_SECONDS=0.2
OBSERVABILITY_PROMPT_CHAR_LIMIT=4000
OBSERVABILITY_SQL_CHAR_LIMIT=1000
OBSERVABILITY_FILE_LOGGING_ENABLED=true
OBSERVABILITY_LOG_DIR=logs
OBSERVABILITY_LOG_FILE_BASENAME=nl2sql.log
OBSERVABILITY_LOG_RETENTION_DAYS=30
OTEL_ENABLED=true
OTEL_EXPORTER_OTLP_ENDPOINT=
```

When the OTLP exporter endpoint is not set, the service still emits structured
JSON logs, persisted trace events, and Prometheus metrics locally.

When `OBSERVABILITY_FILE_LOGGING_ENABLED=true`, the service also writes JSON
logs into the repo-local `logs/` directory. The active file is
`logs/nl2sql.log`, and older files rotate at midnight into day-wise files such
as `logs/nl2sql.log.2026-06-02`. By default, the service keeps 30 daily files.

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
LLM_PROVIDER=ollama
LLM_BASE_URL=http://localhost:11434
LLM_MODEL=deepseek-coder:6.7b
LLM_TIMEOUT=60
LLM_MAX_RETRIES=2
LLM_RETRY_BASE_DELAY=0.5

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
OBSERVABILITY_LOG_DIR=logs
OBSERVABILITY_LOG_RETENTION_DAYS=30
PROVIDER_KEY_ENCRYPTION_SECRET=<32+ char secret>
```

## Production Env Contract

The service now enforces provider configuration at startup instead of relying on
hidden dev defaults.

- `LLM_BASE_URL` is required whenever the resolved provider for a generation role is `ollama`.
- `EMBEDDING_API_URL` is required only when `EMBEDDING_PROVIDER=custom`.
- `EMBEDDING_DIMENSION` must match the configured embedding model. The default
  `bge-large-en-v1.5` uses `1024`; `bge-small-en-v1.5` uses `384` and can reduce
  embedding latency by roughly 3x with a modest quality tradeoff.
- Cloud providers such as `openai`, `groq`, `anthropic`, `gemini`, and `voyageai`
  require a resolved API key. `env:NAME` and `file:/path` references are checked
  during settings validation, not just at request time.
- Role-specific overrides and fallbacks must be complete enough to run on their
  own. For example, `QUERY_REWRITE_FALLBACK_PROVIDER=openai` without a usable API
  key now fails fast.
- There is no hardcoded remote Ollama host anymore. Production must set every
  host explicitly.
- `STARTUP_ENFORCEMENT_MODE=strict` turns provider/runtime readiness failures
  into startup failures. In `warn` mode, the service still starts and exposes
  those failures through `/health` and `/health/runtime`.
- `PROVIDER_KEY_ENCRYPTION_SECRET` is required only for provider-key creation
  and registry-backed cloud-secret storage. If it is unset, the service still
  starts, but `POST /providers/{id}/keys` returns `503`.

Use `GET /health/config` to inspect the resolved provider readiness report and
`GET /health` to confirm the service is starting with `provider_config.status=ok`.
Use `GET /health/runtime` to inspect MySQL execution readiness and required
schema/docs assets on disk.

Provider-specific keys are optional until that provider is selected. Once
selected, missing API keys, missing Ollama base URLs, and incomplete fallback
configs fail fast during settings validation.

Recommended production setting:

```env
STARTUP_ENFORCEMENT_MODE=strict
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

Check provider and runtime health:

```bash
curl -s 'http://localhost:8080/health' | python -m json.tool
curl -s 'http://localhost:8080/health/config' | python -m json.tool
curl -s 'http://localhost:8080/health/runtime' | python -m json.tool
curl -s 'http://localhost:8080/health/llm?role=sql' | python -m json.tool
curl -s 'http://localhost:8080/health/vector' | python -m json.tool
curl -s 'http://localhost:8080/metrics/llm' | python -m json.tool
```

Interpret `GET /health/llm` like this:

- `provider_config.status="ok"` means the selected provider/model settings are valid.
- top-level `status="ok"` means the short live probe succeeded for that role.
- top-level `status="unavailable"` means the role's live probe failed, timed out, or returned an upstream error even though the config itself parsed correctly.

Inspect repo-local daily logs:

```bash
ls -lh logs/
tail -f logs/nl2sql.log
curl -s 'http://localhost:8080/logs/days' | python -m json.tool
curl -s 'http://localhost:8080/logs/recent?day=current&lines=50' | python -m json.tool
curl -N -s 'http://localhost:8080/logs/stream?day=current&backlog=20&follow=true'
```

`/health` now includes compact summaries for PostgreSQL connectivity, provider
config, MySQL execution readiness, schema asset readiness, and teach
confirmation alerts.

For deployment gating, use:

```bash
make smoke-deploy
```

That runs the smoke matrix with `--require-ready`, which fails if `/health`,
`/health/config`, or `/health/runtime` return anything other than `status=ok`.

## Verification

Focused regression command used for trace, stream, cache, and teach behavior:

```bash
python -m pytest tests/test_ask.py tests/test_cache.py tests/test_interactive_learning.py
```

For cold local environments, set `DATABASE_URL`, `LLM_PROVIDER`, `LLM_MODEL`,
`LLM_BASE_URL` when using `ollama`, `EMBEDDING_PROVIDER`, `EMBEDDING_API_URL`
when using `custom`, and `RAG_SCHEMA_DIR` before running the tests.

## Docs

- Route reference: `ROUTES.md`
