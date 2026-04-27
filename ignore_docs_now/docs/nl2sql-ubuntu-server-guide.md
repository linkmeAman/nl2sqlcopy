# Ubuntu Server Guide For NL To SQL Context Retrieval

## Goal

This guide sets up a practical retrieval layer for the workspace documents on Ubuntu so an NL-to-SQL service can fetch schema and business context before generating SQL.

The current working server path is:

```text
/var/www/py-workspace/nl2sql
```

### Source files (inputs — do not edit by hand)

- `docs/mysql_schema_export.txt` — DDL truth (19 k lines, 692 tables + 373 views)
- `docs/database-table-relations.md` — relationship truth (62 documented joins)
- `docs/vector-db-schema-business-context.md` — business semantics
- `docs/nl2sql-corpus.jsonl` — hand-curated domain rows (13 rows)
- `docs/nl2sql-columns.jsonl` — hand-curated exact-column rows (8 rows)

### Generated corpus files (outputs — regenerate after schema changes)

These are produced by `scripts/nl2sql_build_corpus.py` and live in `docs/generated/`:

- `docs/generated/nl2sql_schema_tables.jsonl` — ~695 rows, one per table / chunk
- `docs/generated/nl2sql_schema_views.jsonl` — ~392 rows, one per view / chunk
- `docs/generated/nl2sql_relationships.jsonl` — 62 rows, one per documented join
- `docs/generated/nl2sql_business_rules.jsonl` — 21 rows, business context paragraphs
- `docs/generated/nl2sql_generated_manifest.json` — manifest listing all four files

**Index the generated manifest instead of the individual JSONL files directly.** The manifest is the single source of truth for which corpus files exist.

## Recommended Shape

One practical setup is:

- Docker Engine on Ubuntu
- Qdrant as the vector database
- A Python ingestion script using `fastembed`
- A retrieval script for debugging context hits
- A temporary Gemini bridge script that receives retrieved context and returns SQL text

Official references used for this guide:

- Docker Engine on Ubuntu: https://docs.docker.com/installation/ubuntulinux/
- Qdrant quickstart: https://qdrant.tech/documentation/quickstart/
- Qdrant Python client: https://python-client.qdrant.tech/

## 1. Build The Corpus (Run On Local Machine First)

Before copying files to the server, generate the corpus from the schema export.

```bash
cd /path/to/webportal
python scripts/nl2sql_build_corpus.py
```

Expected output:

```json
{"tables": 692, "views": 373, "relationships": 62, "output_dir": "docs/generated"}
```

Then audit coverage to confirm 100% schema coverage:

```bash
python scripts/nl2sql_audit_corpus.py
```

Expected output (abbreviated):

```json
{
  "tables_complete": true,
  "views_complete": true,
  "relationships_complete": true
}
```

If any `*_complete` field is `false`, fix the schema export or re-run the builder before proceeding.

## 1a. Copy The Workspace Docs And Scripts To The Server

After the corpus is built locally, copy everything to the server:

```bash
scp -r /path/to/webportal/docs developer@your-server:/var/www/py-workspace/nl2sql/
scp /path/to/webportal/scripts/nl2sql_build_corpus.py developer@your-server:/var/www/py-workspace/nl2sql/scripts/
scp /path/to/webportal/scripts/nl2sql_audit_corpus.py developer@your-server:/var/www/py-workspace/nl2sql/scripts/
scp /path/to/webportal/scripts/nl2sql_ingest_qdrant.py developer@your-server:/var/www/py-workspace/nl2sql/scripts/
scp /path/to/webportal/scripts/nl2sql_query_qdrant.py developer@your-server:/var/www/py-workspace/nl2sql/scripts/
scp /path/to/webportal/scripts/nl2sql_generate_gemini.py developer@your-server:/var/www/py-workspace/nl2sql/scripts/
scp /path/to/webportal/scripts/nl2sql_validate_sql.py developer@your-server:/var/www/py-workspace/nl2sql/scripts/
```

On the server, your files should end up like:

```text
/var/www/py-workspace/nl2sql/docs/database-table-relations.md
/var/www/py-workspace/nl2sql/docs/mysql_schema_export.txt
/var/www/py-workspace/nl2sql/docs/vector-db-schema-business-context.md
/var/www/py-workspace/nl2sql/docs/nl2sql-corpus.jsonl
/var/www/py-workspace/nl2sql/docs/nl2sql-columns.jsonl
/var/www/py-workspace/nl2sql/docs/generated/nl2sql_schema_tables.jsonl
/var/www/py-workspace/nl2sql/docs/generated/nl2sql_schema_views.jsonl
/var/www/py-workspace/nl2sql/docs/generated/nl2sql_relationships.jsonl
/var/www/py-workspace/nl2sql/docs/generated/nl2sql_business_rules.jsonl
/var/www/py-workspace/nl2sql/docs/generated/nl2sql_generated_manifest.json
/var/www/py-workspace/nl2sql/scripts/nl2sql_build_corpus.py
/var/www/py-workspace/nl2sql/scripts/nl2sql_audit_corpus.py
/var/www/py-workspace/nl2sql/scripts/nl2sql_ingest_qdrant.py
/var/www/py-workspace/nl2sql/scripts/nl2sql_query_qdrant.py
/var/www/py-workspace/nl2sql/scripts/nl2sql_generate_gemini.py
/var/www/py-workspace/nl2sql/scripts/nl2sql_validate_sql.py
```

## 2. Install Docker Engine On Ubuntu

Follow Docker's official Ubuntu instructions. A working path on Ubuntu `22.04` is:

```bash
sudo apt update
sudo apt install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

sudo tee /etc/apt/sources.list.d/docker.sources > /dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF

sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
```

Optional:

```bash
sudo usermod -aG docker $USER
newgrp docker
```

## 3. Run Qdrant

Create persistent storage:

```bash
mkdir -p /var/www/py-workspace/nl2sql/qdrant_storage
```

Run Qdrant:

```bash
docker run -d \
  --name webportal-qdrant \
  --restart unless-stopped \
  -p 6333:6333 \
  -p 6334:6334 \
  -v /var/www/py-workspace/nl2sql/qdrant_storage:/qdrant/storage \
  qdrant/qdrant
```

Check it:

```bash
curl http://localhost:6333
docker ps
```

Qdrant dashboard is normally available at:

```text
http://your-server:6333/dashboard
```

Important: by default this is open to the network. Put it behind a firewall or reverse proxy before exposing it publicly.

## 4. Create A Python Environment

```bash
sudo apt install -y python3 python3-venv python3-pip
mkdir -p /var/www/py-workspace/nl2sql
cd /var/www/py-workspace/nl2sql
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -U qdrant-client fastembed
```

This setup uses `fastembed` directly inside the Python scripts. That avoids relying on `qdrant-client` helper methods that may differ across versions.

## 5. Minimal Collection Design

Create one collection called `webportal_nl2sql_context`.

Suggested payload fields (all set by the ingester):

- `doc_id`
- `title`
- `text`
- `source_file`
- `tags`
- `object_type` — `table`, `view`, `relationship`, or `business_rule`
- `database` — schema name (`pf_TickleRight_9210`, `pf_central`, `pf_messenger`)
- `full_object_name` — `database.object_name`, used for deduplication
- `source_kind` — `schema_export`, `relations_doc`, or `business_context_doc`

Recommended ingestion order (all handled automatically via the manifest):

1. Generated schema tables corpus (`nl2sql_schema_tables.jsonl`) — 695 rows
2. Generated schema views corpus (`nl2sql_schema_views.jsonl`) — 392 rows
3. Generated relationships corpus (`nl2sql_relationships.jsonl`) — 62 rows
4. Generated business-rules corpus (`nl2sql_business_rules.jsonl`) — 21 rows
5. Hand-curated corpus (`nl2sql-corpus.jsonl`) — 13 rows (pass via `--corpus`)
6. Hand-curated columns (`nl2sql-columns.jsonl`) — 8 rows (pass via `--corpus`)

## 6. Ingest The NL To SQL Corpus

### Standard ingest (manifest-driven — recommended)

Pass the generated manifest to ingest all four generated corpus files at once:

```bash
cd /var/www/py-workspace/nl2sql
source .venv/bin/activate
python scripts/nl2sql_ingest_qdrant.py \
  --qdrant-url http://localhost:6333 \
  --collection webportal_nl2sql_context \
  --manifest /var/www/py-workspace/nl2sql/docs/generated/nl2sql_generated_manifest.json \
  --corpus /var/www/py-workspace/nl2sql/docs/nl2sql-corpus.jsonl \
  --corpus /var/www/py-workspace/nl2sql/docs/nl2sql-columns.jsonl
```

Expected output:

```text
Indexed ... rows into collection 'webportal_nl2sql_context' from 6 corpus file(s)
```

The ingester de-duplicates paths so passing `--manifest` and `--corpus` together is safe.

### Rebuild after schema changes

When the MySQL schema changes, regenerate the corpus and re-ingest:

```bash
# Regenerate
python scripts/nl2sql_build_corpus.py

# Audit coverage
python scripts/nl2sql_audit_corpus.py

# Re-ingest (drops and recreates the collection)
python scripts/nl2sql_ingest_qdrant.py \
  --qdrant-url http://localhost:6333 \
  --collection webportal_nl2sql_context \
  --manifest docs/generated/nl2sql_generated_manifest.json \
  --corpus docs/nl2sql-corpus.jsonl \
  --corpus docs/nl2sql-columns.jsonl
```

### Legacy ingest (individual files — no manifest)

If you only want the hand-curated rows without the generated corpus:

```bash
python scripts/nl2sql_ingest_qdrant.py \
  --qdrant-url http://localhost:6333 \
  --collection webportal_nl2sql_context \
  --corpus /var/www/py-workspace/nl2sql/docs/nl2sql-corpus.jsonl \
  --corpus /var/www/py-workspace/nl2sql/docs/nl2sql-columns.jsonl
```

If you see this error:

```text
AttributeError: 'QdrantClient' object has no attribute 'fastembed'
```

your script is still using the old helper call. Replace it with the current `fastembed.TextEmbedding` version from `scripts/nl2sql_ingest_qdrant.py`.

## 7. Test Retrieval

Run:

```bash
cd /var/www/py-workspace/nl2sql
source .venv/bin/activate
python scripts/nl2sql_query_qdrant.py "show active packages expiring this month by branch"
```

This should print ranked context chunks from `webportal_nl2sql_context`.

## 8. Validate Generated SQL Before Execution

Run:

```bash
cd /var/www/py-workspace/nl2sql
source .venv/bin/activate
python scripts/nl2sql_validate_sql.py --sql-file /path/to/query.sql
```

You can also pipe SQL directly:

```bash
cat /path/to/query.sql | python scripts/nl2sql_validate_sql.py
```

The validator checks generated SQL against `docs/mysql_schema_export.txt` and catches invented columns such as `contact_name`, `service_name`, or `branch_name` when the actual view uses `fullname`, `service`, or `branch_name_actual`.

## 9. Temporary Gemini LLM Bridge

For a temporary end-to-end NL-to-SQL path, use `scripts/nl2sql_generate_gemini.py`. It:

1. embeds the incoming question
2. retrieves top context chunks from Qdrant
3. builds a workspace-specific NL-to-SQL prompt
4. sends that prompt to Gemini `generateContent`
5. prints the model output

Set the API key:

```bash
export GEMINI_API_KEY='your-real-key'
```

Run the bridge:

```bash
cd /var/www/py-workspace/nl2sql
source .venv/bin/activate
python scripts/nl2sql_generate_gemini.py \
  "show active packages expiring this month by branch" \
  --qdrant-url http://localhost:6333 \
  --collection webportal_nl2sql_context \
  --show-context
```

You can also pass the key inline:

```bash
python scripts/nl2sql_generate_gemini.py \
  "show teacher schedule for tomorrow" \
  --gemini-api-key 'your-real-key'
```

This script calls:

```text
https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent
```

## 10. Retrieval Pattern For NL To SQL

At query time:

1. Embed the user's question.
2. Search Qdrant for top matching chunks.
3. Send those chunks plus the user question to your SQL-generation model.
4. In the SQL prompt, instruct the model to:
   - prefer documented views when possible
   - avoid inventing foreign keys
   - respect `park = 0` style defaults when appropriate
   - qualify schema names when crossing `pf_TickleRight_9210`, `pf_central`, and `pf_admin`

The current scripts already follow this pattern:

- `scripts/nl2sql_query_qdrant.py` for retrieval only
- `scripts/nl2sql_generate_gemini.py` for retrieval plus Gemini generation
- `scripts/nl2sql_validate_sql.py` for schema validation before execution

## 11. Good Prompt Rules For The SQL Model

Use rules like:

- Use `invoice_invoiceitem_view` for invoice, package, service, member, and branch revenue questions unless raw tables are explicitly needed.
- Use `session_batch_view` or `batch_employee_time_view` for schedule questions.
- Use `attendance_cont_view` for attendance questions.
- Use `contact_followup_view` for CRM and reminder timeline questions.
- Use exact retrieved column names. Do not guess aliases like `contact_name` when the catalog says `fullname`.
- Treat `request.table_name` and `request.row_id` as polymorphic references, not a fixed FK.
- Do not claim FK constraints unless schema text explicitly shows them.

## 12. Production Hardening

- Restrict Qdrant port access with UFW, security groups, or reverse proxy rules.
- Keep the vector DB on private network if possible.
- Version your corpus files so you know which schema snapshot was embedded. The manifest (`nl2sql_generated_manifest.json`) records per-database row counts — commit it alongside the schema export.
- Automate corpus rebuild: whenever `docs/mysql_schema_export.txt` changes, run `nl2sql_build_corpus.py` → `nl2sql_audit_corpus.py` → re-ingest. Fail the pipeline if any `*_complete` audit flag is `false`.
- Do not hardcode the Gemini key in the script. Pass it with `GEMINI_API_KEY` or `--gemini-api-key`.
- Keep `docs/nl2sql-corpus.jsonl` and `docs/nl2sql-columns.jsonl` for hand-curated overrides. They are indexed in addition to the generated corpus, not instead of it.
- Validate generated SQL before execution, especially for view-heavy queries.
- Re-run ingestion whenever:
  - schema export changes
  - major views change
  - docs in `docs/` are updated

## 13. Fast Path

If you want the shortest useful deployment:

1. Copy `docs/` and the NL-to-SQL scripts to Ubuntu
2. Install Docker
3. Run Qdrant
4. Create Python venv
5. Index `docs/nl2sql-corpus.jsonl` and `docs/nl2sql-columns.jsonl`
6. Use `scripts/nl2sql_generate_gemini.py` for temporary generation
7. Run `scripts/nl2sql_validate_sql.py` before execution

That gives you a working first version without indexing the full raw schema export.
