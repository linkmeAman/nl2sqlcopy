# NL2SQL Implementation Status

**Last Updated:** 2026-05-25  
**Status:** Implemented

## Implemented Areas

- FastAPI service in `nl2sql_service/main.py`
- PostgreSQL + pgvector retrieval store
- ReAct SQL generation pipeline
- Bounded `/ask` execution and answer generation
- Interactive teaching and confirmation flow
- Instruction review route
- Version-aware ingest routes for groups, knowledge, patterns, and instructions
- Request telemetry and benchmark support
- In-memory exact/semantic caches
- Persistent PostgreSQL query cache with cache epoch invalidation

## Current Route Surface

- Retrieval: `/query`, `/query/groups`
- Ingest: `/ingest`, `/ingest/groups`, `/ingest/knowledge`,
  `/ingest/patterns`, `/ingest/instructions`
- Learning: `/teach`, `/teach/confirm`, `/instructions`,
  `/instructions/{instruction_id}`, `/patterns/feedback`
- Generation: `/generate-sql`, `/ask`, `/ask/stream`
- Ops: `/health`, `/cache/*`, `/telemetry/*`, `/governance/*`,
  `/benchmark/cases`

## Cache Status

Implemented:

- memory exact SQL cache
- memory semantic SQL cache
- memory ask cache
- PostgreSQL exact query cache
- PostgreSQL semantic query cache
- DB-backed cache epoch in `nl2sql_cache_state`

Behavior:

- `/generate-sql` and `/ask` look in memory first, then DB, then run the full
  pipeline
- only `status="ok"` responses are persisted
- cache metadata is returned in API responses
- teach/ingest mutations bump cache epoch and clear in-memory caches

## Contract Status

Implemented additive fields:

- `/generate-sql`: `cache_hit`, `cache_source`
- `/ask`: `cache_hit`, `cache_source`
- `/ingest/groups`: `skipped`
- `/ingest/knowledge`: `skipped`
- `/ingest/patterns`: `skipped`
- `/ingest/instructions`: `skipped`

Teach semantics aligned:

- HTTP `200` for controlled teach outcomes
- HTTP `503` only for DB pool unavailability

## Reference Docs

- `README.md`
- `ROUTES.md`
