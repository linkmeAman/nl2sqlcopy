---
description: "Ingest new or updated schema groups into the nl2sql service. Use when: adding a new entity group, re-ingesting after schema changes, or checking ingest status."
argument-hint: "group name(s) to ingest, e.g. member_profile billing — or 'all' / 'knowledge'"
agent: agent
---

Ingest schema groups into the running nl2sql service at `http://localhost:8080`.

**Argument:** `$args` — one or more group names (e.g. `member_profile billing`), `all`, `knowledge`, or leave blank to ingest everything.

## Steps

### 1. Determine what to ingest

- If argument is blank or `all`: ingest all groups + knowledge.
- If argument is `knowledge`: ingest enriched knowledge only.
- Otherwise: treat argument as space-separated group names.

Before ingesting, check the current status to identify stale or never-embedded groups:

```bash
curl -s http://localhost:8080/ingest/groups/status | python -m json.tool
```

### 2. Locate the entity file (for named groups)

Schema group definitions live in [rag_schema/entities/](../../rag_schema/entities/). Each file is named `entity__<group_name>.json` and contains `entity_id`, `chunk_group_name`, `root_table`, `included_tables`, etc.

For a new group, confirm the entity JSON exists before ingesting. If it doesn't exist, create it first — see [rag_schema/entities/entity__member_profile.json](../../rag_schema/entities/entity__member_profile.json) as a reference template.

### 3. Run the ingest

Use the ingest script (no service dependencies, pure HTTP):

```bash
# Specific groups
.venv/bin/python scripts/nl2sql_ingest_groups.py \
  --url http://localhost:8080 \
  --groups <group_name_1> <group_name_2>

# All schema groups
.venv/bin/python scripts/nl2sql_ingest_groups.py \
  --url http://localhost:8080 --all

# Knowledge (column catalog, SQL examples, relations, graph, etc.)
.venv/bin/python scripts/nl2sql_ingest_groups.py \
  --url http://localhost:8080 --knowledge

# All groups + knowledge in one pass
.venv/bin/python scripts/nl2sql_ingest_groups.py \
  --url http://localhost:8080 --all --column-limit 300 --sql-example-limit 200
```

Or via the API directly:

```bash
# Named groups
curl -s -X POST http://localhost:8080/ingest/groups \
  -H "Content-Type: application/json" \
  -d '{"group_names": ["<group_name>"]}' | python -m json.tool

# All groups
curl -s -X POST http://localhost:8080/ingest/groups \
  -H "Content-Type: application/json" \
  -d '{}' | python -m json.tool
```

### 4. Verify the result

A successful response looks like:

```json
{
  "inserted": 4,
  "updated": 1,
  "skipped": 12,
  "source": "member_profile",
  "failure_count": 0,
  "failed_groups": []
}
```

- `inserted + updated > 0` → cache was invalidated automatically.
- `skipped` = chunks whose `schema_version` hash has not changed (no re-embed needed).
- `failure_count > 0` → inspect `failed_groups[].reason` and fix the entity JSON or rag_schema files.

Check status again after ingestion to confirm the group shows `is_current: true`:

```bash
curl -s http://localhost:8080/ingest/groups/status | python -m json.tool
```

### 5. Smoke-test retrieval

Confirm the newly ingested group is retrievable:

```bash
curl -s -X POST http://localhost:8080/query \
  -H "Content-Type: application/json" \
  -d '{"query": "<natural language question about the ingested domain>", "top_k": 5}' \
  | python -m json.tool
```

## Notes

- `EMBEDDING_DIMENSION` is fixed at DDL bootstrap. Changing it requires dropping and recreating `nl2sql_embeddings`.
- Ingest is idempotent — re-running only updates chunks whose `schema_version` hash changed.
- Successful ingest automatically bumps the DB cache epoch and clears in-memory caches.
- `make sync-schema` regenerates `rag_schema/` from the live MySQL schema before ingesting.
