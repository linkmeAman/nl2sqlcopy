# NL2SQL Implementation Status

**Last Updated:** April 25, 2026
**Status:** COMPLETE and RUNNING

This file reflects the current implemented system, not just planning artifacts.

## Current Runtime Architecture

- API service: `nl2sql_service/main.py` (FastAPI)
- Vector store: PostgreSQL + pgvector table `nl2sql_embeddings`
- Retrieval routes:
   - `POST /query` (all chunk types)
   - `POST /query/groups` (schema-group only)
- Generation route:
   - `POST /generate-sql` (schema-group RAG + ReAct planning + Ollama SQL generation)
- Ingestion routes:
   - `POST /ingest`
   - `POST /ingest/groups`
   - `POST /ingest/knowledge`

## Implemented SQL Generation

- Route: `POST /generate-sql`
- Retrieval source: existing `retrieve_groups()` output
- Reasoning agent: Ollama `qwen3:4b` at `{LLM_BASE_URL}/api/generate`
- SQL generator: Ollama `deepseek-coder:6.7b` at `{LLM_BASE_URL}/api/generate`
- Default dialect: `mysql`
- SQL is validated and returned, never executed.
- Controlled SQL or Ollama failures return HTTP 200 with `status="rejected"`.
- DB pool failures still return HTTP 503.

Response variants:
- `status="ok"` includes `sql`, `warnings`, `tables_used`, `matched_groups`, `attempt_count`, and `react_trace`.
- `status="rejected"` includes `status`, `sql=null`, `warnings`, `attempt_count`, and `react_trace`.
- `attempt_count` is the number of completed ReAct loop iterations, not raw SQL generation attempts.

Implemented guardrails:
- non-empty SQL
- exactly one statement
- SELECT or WITH...SELECT only
- destructive keyword rejection outside comments and string literals
- FROM/JOIN tables limited to retrieved `tables_in_scope`
- column names limited to live schema columns when available
- MySQL EXPLAIN check when app DB is reachable
- `MYSQL_EXPLAIN_UNAVAILABLE` treated as a non-blocking warning
- schema-qualified table normalization
- CTE aliases excluded from unknown-table rejection
- ReAct loop with actions: `RETRIEVE_MORE_CONTEXT`, `FETCH_SCHEMA`, `GENERATE_SQL`, `VALIDATE_AND_RETURN`, and `GIVE_UP`
- loop ceiling controlled by `REACT_MAX_ITERATIONS`
- request `top_k` is preserved across ReAct retrieval-refinement steps
- `RETRIEVE_MORE_CONTEXT` refreshes live columns for the new tables in scope
- retry `GENERATE_SQL` calls use the prior SQL, blocking validation errors, and planner instruction
- retry refinement is strict: fix listed validation errors, avoid disallowed tables/columns from prior SQL, and prioritize SQL guardrails over planner hints
- explicit `GIVE_UP` returns one terminal warning without also appending a synthetic loop-exhausted warning
- reasoning-model timeouts/upstream failures do not append a synthetic loop-exhausted warning

Reasoning/model settings:
- `REASONING_MODEL=qwen3:4b`
- `REASONING_TEMPERATURE=0.6`
- `REASONING_TIMEOUT=45`
- `REACT_MAX_ITERATIONS=4`
- qwen reasoning calls use `thinking=true` and `num_predict=800`
- deepseek generation calls use `stream=false` and temperature `0.0`

## Implemented Ingestion Sources

### Schema groups

- Source directory: `rag_schema/entities/`
- Ingestion route: `POST /ingest/groups`
- Group name input supports both:
   - entity IDs (example: `entity__inquiry_lifecycle`)
   - short names (example: `inquiry_lifecycle`)
- Layer 1 enrichment at ingest time includes:
   - live MySQL columns per group table (via `information_schema.COLUMNS`)
   - business aliases from entity `business_aliases`
   - example NL prompts from entity `example_questions`
- Group chunk metadata now includes:
   - `has_columns`
   - `has_aliases`
   - `has_examples`
   - `column_source` (`mysql_live` or `unavailable`)
- `/ingest/groups` response includes `enrichment_summary` counters.

### Enriched knowledge

- Ingestion route: `POST /ingest/knowledge`
- Embedded chunk types and sources:
   - `column_catalog` from docs JSONL files
   - `sql_example` from `ignore_docs_now/docs/mysql_schema_export.txt`
   - `relation_link` from `rag_schema/relations/*.json`
   - `table_node` from `rag_schema/graph/table_graph.json`
   - `view_node` from `rag_schema/graph/view_registry.json`
   - `schema_rule` from `rag_schema/rules/onboarding_rules.json`

## Versioning and Idempotency

- Upsert key: `(source, chunk_index)`
- Version field: `metadata.schema_version`
- Version generation: `MD5(raw content)[:8]` per source chunk file/line pattern
- Update behavior:
   - insert if key does not exist
   - update if key exists and `schema_version` changed
   - no-op if key exists and `schema_version` unchanged

## Performance Optimization Added (April 24, 2026)

- `ingest.py` now pre-checks existing `(source, chunk_index, schema_version)` rows before embedding.
- Unchanged chunks are filtered out before embedding API calls.
- Result:
   - repeat full ingest runs are fast
   - unchanged datasets no longer trigger unnecessary re-embedding

## CLI Script Status

`scripts/nl2sql_ingest_groups.py` supports:

- `--groups ...` for selected group ingestion
- `--knowledge` for knowledge-only ingestion
- `--all` for serial run (`/ingest/groups` then `/ingest/knowledge`)
- `--timeout` for long ingest requests
- Source controls:
   - `--skip-columns`
   - `--skip-sql`
   - `--skip-relations`
   - `--skip-graph`
   - `--skip-view-registry`
   - `--skip-rules`
- Source limits:
   - `--column-limit`
   - `--sql-example-limit`
   - `--relation-limit`
   - `--graph-limit`
   - `--view-registry-limit`

## Verified Operational Outcomes

- Health check succeeds: `GET /health` returns status OK and DB connected.
- Full serial ingest has been run successfully with all sources enabled.
- Subsequent repeat runs on unchanged data return:
   - `/ingest/groups`: `inserted=0, updated=0`
   - `/ingest/knowledge`: `inserted=0, updated=0`
- No application errors were reported in updated ingestion modules during checks.
- Latest regression tests (April 25, 2026):
   - `tests/test_chunk_enrichment.py`: pass
   - `tests/test_generate_sql.py`: pass
   - `tests/test_react_agent.py`: pass
   - Full run: `31 passed, 0 failed`
- `/generate-sql` tests cover valid SELECT, valid WITH...SELECT, preserved leading comments, destructive rejection, multi-statement rejection, table-scope rejection and correction, exhausted ReAct iterations, Ollama timeout rejection, response shape, and DB unavailable behavior.
- `/generate-sql` tests include a golden API rejection trace for validation-driven retry exhaustion (alternating `GENERATE_SQL` and `VALIDATE_AND_RETURN` with `FAILED:` observations).
- ReAct tests cover happy path, retrieve-more-context, fetch-schema, retry refinement prompting, give-up, max-iteration exhaustion, reasoning timeout, rejected `react_trace`, non-blocking `MYSQL_EXPLAIN_UNAVAILABLE`, `<think>` parsing, and action parsing.
- OpenAPI exposes `/generate-sql` with a `status`-discriminated response model.

## Reference Docs

- Route reference: `ROUTES.md`
- Main project documentation: `README.md`
- Historical planning artifact: `nl2sql_refactor_plan.md`
