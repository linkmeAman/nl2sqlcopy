-- Non-destructive pgvector HNSW migration for nl2sql_embeddings.embedding.
-- This script intentionally does not drop existing IVFFlat indexes. Confirm
-- recall and planner behavior before removing any legacy vector index.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE INDEX IF NOT EXISTS nl2sql_embed_hnsw_idx
    ON nl2sql_embeddings
    USING hnsw (embedding vector_cosine_ops)
    WITH (m=16, ef_construction=64);

-- Verification query:
-- SELECT indexname, indexdef
-- FROM pg_indexes
-- WHERE schemaname = current_schema()
--   AND tablename = 'nl2sql_embeddings'
--   AND indexdef ILIKE '%USING hnsw%'
--   AND indexdef ILIKE '%embedding%';

-- Query-time setting to use with vector lookups in application transactions:
-- BEGIN;
-- SET LOCAL hnsw.ef_search = 40;
-- SELECT content
-- FROM nl2sql_embeddings
-- ORDER BY embedding <=> $1
-- LIMIT $2;
-- COMMIT;
