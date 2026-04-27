## Completed Plan: Production NL-to-SQL Endpoint

`POST /generate-sql` has been upgraded to a ReAct pipeline. It reuses schema-group retrieval, calls `qwen3:4b` for Thought/Action planning, calls `deepseek-coder:6.7b` only for SQL generation, validates generated SQL with strict guardrails, and returns HTTP 200 with either `status="ok"` or `status="rejected"`. Generated SQL is never executed.

## Implemented Contract

### Request

- `query`: string
- `top_k`: integer or null

### Success Response

- `status`: `"ok"`
- `sql`: generated SQL string
- `warnings`: list of `{code, message}` warning objects, normally empty
- `tables_used`: in-scope tables referenced by the SQL
- `matched_groups`: schema groups selected by retrieval
- `attempt_count`: number of completed ReAct iterations
- `react_trace`: ReAct steps, total iterations, and final action

### Rejected Response

- `status`: `"rejected"`
- `sql`: null
- `warnings`: list of `{code, message}` warning objects
- `attempt_count`: number of completed ReAct iterations
- `react_trace`: ReAct steps, total iterations, and final action

Rejected responses intentionally do not include `tables_used` or `matched_groups`.

## Implemented Settings

- `LLM_PROVIDER=ollama`
- `LLM_BASE_URL=http://100.120.187.84:11434`
- `LLM_MODEL=deepseek-coder:6.7b`
- `LLM_TIMEOUT=60`
- `LLM_MAX_RETRIES=2`
- `REASONING_MODEL=qwen3:4b`
- `REASONING_TEMPERATURE=0.6`
- `REASONING_TIMEOUT=45`
- `REACT_MAX_ITERATIONS=4`
- `SQL_DIALECT=mysql`

`LLM_BASE_URL` is the Ollama base URL and is independent from `EMBEDDING_API_URL`. Both qwen and deepseek use `POST {LLM_BASE_URL}/api/generate`.

`REACT_MAX_ITERATIONS` controls the ReAct Thought/Action/Observation loop. `LLM_MAX_RETRIES` is retained for legacy compatibility and is not the ReAct loop limit.

`qwen3:4b` uses hybrid thinking mode (`thinking=true`) and temperature `0.6`. `deepseek-coder:6.7b` still uses temperature `0.0` for SQL generation.

`SQL_DIALECT` controls output SQL syntax only. It is unrelated to the PostgreSQL + pgvector storage engine.

## Implemented Flow

1. `main.py` receives `POST /generate-sql`.
2. `_require_pool()` enforces the existing DB availability behavior.
3. `sql_generator.generate_sql()` delegates to `react_agent.run()`.
4. `react_agent.run()` calls `retrieve_groups(query, top_k, pool)` and loads live columns for the tables in scope.
5. Each iteration builds a ReAct prompt with context, tables, known columns, history, and current error.
6. `call_reasoning_model()` calls qwen with `thinking=true` and parses the `<think>` block separately from the action answer.
7. `parse_action()` normalizes one of five actions: `RETRIEVE_MORE_CONTEXT`, `FETCH_SCHEMA`, `GENERATE_SQL`, `VALIDATE_AND_RETURN`, or `GIVE_UP`.
8. `GENERATE_SQL` calls `deepseek-coder:6.7b`, then `extract_sql()` normalizes fenced SQL, raw SQL, and leading prose while preserving leading SQL comments.
9. `VALIDATE_AND_RETURN` runs `validate_sql_safety()`, `validate_tables_used()`, `validate_columns_used()`, and `run_explain()`.
10. `MYSQL_EXPLAIN_UNAVAILABLE` is kept as an informational warning and does not cause rejection.
11. If validation passes, the route returns `status="ok"` with `react_trace`.
12. If reasoning fails, action execution fails, qwen chooses `GIVE_UP`, or max iterations are exhausted, the route returns `status="rejected"` with `react_trace`.

## Warning Codes

- `OLLAMA_TIMEOUT`
- `OLLAMA_UPSTREAM`
- `OLLAMA_MALFORMED`
- `SQL_EMPTY`
- `SQL_MULTI_STATEMENT`
- `SQL_DESTRUCTIVE`
- `SQL_NOT_SELECT`
- `TABLE_OUT_OF_SCOPE`
- `COLUMN_OUT_OF_SCOPE`
- `MYSQL_EXPLAIN_ERROR`
- `MYSQL_EXPLAIN_UNAVAILABLE`
- `MAX_RETRIES_EXCEEDED`

## Relevant Files

- `nl2sql_service/models.py` - request/response models, warning enum, discriminated union
- `nl2sql_service/config.py` - LLM, reasoning, ReAct, and dialect settings
- `nl2sql_service/react_agent.py` - ReAct orchestration, reasoning calls, action execution, and traces
- `nl2sql_service/sql_generator.py` - SQL prompt, deepseek call, extraction, validation, EXPLAIN, and thin wrapper
- `nl2sql_service/retrieve.py` - existing schema-group retrieval reused as-is
- `nl2sql_service/main.py` - route wiring
- `.env.example` - documented production defaults
- `requirements.txt` - SQL parser and test dependencies
- `tests/conftest.py` - ASGI and monkeypatch fixtures
- `tests/test_generate_sql.py` - endpoint and guardrail tests
- `tests/test_react_agent.py` - ReAct loop and parsing tests
- `README.md` - usage and operational docs
- `ROUTES.md` - route contract docs

## Verification

- `python -m pytest -q` passes all generator, ReAct, and guardrail tests.
- Timeout/upstream/malformed Ollama cases return HTTP 200 with `status="rejected"` and warning codes.
- Rejected payloads contain `status`, `sql`, `warnings`, `attempt_count`, and `react_trace`.
- Table validation accepts schema-qualified variants of allowed tables and rejects out-of-scope tables.
- Column validation rejects unknown columns when live schema is available.
- `MYSQL_EXPLAIN_UNAVAILABLE` does not cause rejection.
- CTE aliases do not trigger `TABLE_OUT_OF_SCOPE`.
- Destructive keywords inside comments or string literals do not trigger `SQL_DESTRUCTIVE`.
- Existing DB unavailable behavior still returns HTTP 503 through `_require_pool()`.
- OpenAPI exposes a `status`-discriminated response union.

## Decisions

- SQL generation failures are controlled responses, not server errors.
- DB and retrieval failures keep existing behavior, including 503 when the pool is unavailable.
- `LLM_BASE_URL` is explicit and is not derived from `EMBEDDING_API_URL`.
- `sqlparse` is the parser of record for statement-level checks.
- ReAct trace is part of both success and rejected responses for transparency.
- `scripts/` legacy tooling is out of scope and unchanged.
