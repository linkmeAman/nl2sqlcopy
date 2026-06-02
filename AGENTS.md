# NL2SQL Service

FastAPI service: RAG + ReAct NL2SQL pipeline — PostgreSQL (pgvector) for retrieval/trace/cache, MySQL as the execution target.

## Build & Test

```bash
make setup        # bootstrap virtualenv
make run          # dev server on :8080 (hot reload)
make test         # pytest tests/ -v
make ingest       # ingest all schema groups
make smoke        # smoke-test all routes
make smoke-deploy # smoke-test routes and require readiness endpoints to be ok
make benchmark    # replay benchmark cases → reports/
```

Tests use `pytest-asyncio`. `conftest.py` autouse fixtures **disable governance and query rewriting** and **clear in-memory caches** between tests — do not fight these fixtures.

## Architecture

- **Entry:** `nl2sql_service/main.py` — all routes, `TraceRecorder`, lifespan hooks
- **Config:** `nl2sql_service/config.py` — `Settings` (pydantic-settings) loaded from `.env`
- **Models:** `nl2sql_service/models.py` — all Pydantic v2 request/response types
- **Pipeline:** query rewrite → embed + cache check → RAG retrieval → ReAct loop → SQL generation → EXPLAIN → MySQL execution → answer generation

Key modules:

| Module | Role |
|--------|------|
| `react_agent.py` | ReAct loop with actions: `RETRIEVE_MORE_CONTEXT`, `FETCH_SCHEMA`, `GENERATE_SQL`, `VALIDATE_AND_RETURN`, `ASK_CLARIFICATION`, `GIVE_UP` |
| `sql_generator.py` | Prompt building + provider-agnostic SQL generation + SQL extraction/validation |
| `retrieve.py` | pgvector similarity search against `nl2sql_embeddings` |
| `cache.py` | In-memory exact+semantic caches; invalidation bumps DB epoch |
| `instruction_store.py` | `/teach` flow — active instruction storage plus DB-backed pending confirmations (30-min TTL) |
| `rulebook.py` | Hard governance rules injected into every prompt |
| `mysql_executor.py` | SQL execution with 50-row cap (`apply_row_cap`) |
| `llm/` | Provider-agnostic LLM, embedding, fallback, streaming, prompt, and metrics layer |

See [README.md](README.md) for the full API reference and [ROUTES.md](ROUTES.md) for all endpoints.

LLM layer conventions:

- Import runtime model access from `nl2sql_service.llm`, not from deleted legacy paths.
- `nl2sql_service.llm.interfaces` is the canonical source for shared LLM dataclasses and provider interfaces.
- `nl2sql_service.llm.types` is a compatibility re-export only; do not add new source-of-truth models there.
- Provider implementations belong under `nl2sql_service.llm.providers.*`; OpenAI-compatible HTTP normalization belongs in `nl2sql_service.llm.adapters.openai`.

## Required Env Vars

| Var | Purpose |
|-----|---------|
| `DATABASE_URL` | PostgreSQL URL — e.g. `postgresql://user:pass@<ip>:5432/ragdb` |
| `EMBEDDING_PROVIDER` | Embedding provider: `custom`, `openai`, `gemini`, `ollama`, `voyageai` |
| `EMBEDDING_API_URL` | Required for `EMBEDDING_PROVIDER=custom`; external bge/TEI-style endpoint |
| `LLM_PROVIDER/LLM_MODEL` | Default LLM provider and model for generation workloads |
| `LLM_API_KEY` | Required when the selected provider is a cloud API |
| `LLM_BASE_URL` | Required when the resolved provider is `ollama`; set it explicitly in every environment |
| `DB_HOST/PORT/USER/PASSWORD/DB_NAME` | MySQL execution target |

Role-specific `SQL_*`, `REASONING_*`, `QUERY_REWRITE_*`, and `ANSWER_*` settings override the global `LLM_*` defaults. Secrets can be raw env values, `env:NAME`, or `file:/path/to/secret`.

Provider validation is now strict:

- Any resolved `ollama` role needs an explicit base URL.
- `EMBEDDING_PROVIDER=custom` needs `EMBEDDING_API_URL`.
- Cloud providers need a resolved API key even when configured through `env:` or `file:` references.
- Role-specific fallback providers must also be fully configured.
- `STARTUP_ENFORCEMENT_MODE=strict` should be used in production to fail startup
  when provider config, MySQL readiness, or schema/docs assets are not ready.

## Key Conventions

- **Response shape:** all generation responses are discriminated unions — always check `status` before accessing `sql`/`answer` fields. `GenerateSqlResponse = GenerateSqlSuccess | GenerateSqlRejected | GenerateSqlClarification`
- **Governance:** `rulebook.py` hard rules are injected into every prompt. Never bypass in production. `GOVERNANCE_ENABLED=true` by default.
- **Cache invalidation:** teach/ingest mutations call `_invalidate_query_caches()` — bumps DB cache epoch and clears in-memory caches.
- **Trace events:** all stages emit trace events to `nl2sql_trace_events`. Sanitize: include action summaries, timings, SQL previews — never expose internal `<think>` reasoning text.

## Pitfalls

- `EMBEDDING_DIMENSION` is baked into the DDL at startup — changing it requires dropping and recreating `nl2sql_embeddings`.
- `DB_NAME` vs `DB_CENTRAL`: `mysql_executor.py` uses `db_name or db_central` — don't leave both empty or SQL execution fails silently.
- Teach confirmations are DB-backed and survive service restarts, but confirmation tokens still expire after 30 minutes.
- `GET /health/config` exposes provider readiness and should be part of production smoke checks.
- `GET /health/runtime` exposes MySQL execution readiness and schema/docs asset readiness and should be part of production smoke checks.
- `GET /config/model-routing` and `PATCH /config/model-routing` expose the live task-to-model routing for the current process.
- `make smoke-deploy` is the intended pre-rotation gate because it fails when readiness endpoints are not `ok`.
- ReAct reasoning uses the configured `reasoning` provider role and retries in non-thinking mode on timeout — `REASONING_TIMEOUT` budget is effectively doubled on failure.
- DB pool is non-fatal at startup — routes return `503` until the pool connects; `_ensure_pool()` rate-limits reconnects to 5 s.
