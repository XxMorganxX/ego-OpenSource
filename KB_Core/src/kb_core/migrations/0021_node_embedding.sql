-- Phase 3 (write path): a per-node embedding vector + a hash of the text behind it.
-- pgvector is (re)created here so this migration is self-sufficient on an existing DB,
-- even though db/init/01-extensions.sql pre-creates it on fresh containers.
--
-- vector(1024) is qwen3-embedding:0.6b's native dimensionality. This couples to the embed
-- model: OLLAMA_EMBED_DIMENSIONS must stay unset (native 1024) or writes fail the ::vector
-- cast. Changing the dimension is a new migration, not a config flip.
--
-- No ANN index yet: index type (HNSW/IVFFlat) and distance op-class (cosine/L2) couple to the
-- query-time metric, which lands with the retrieval spec (Phase 3 read path).
CREATE EXTENSION IF NOT EXISTS vector;

ALTER TABLE nodes ADD COLUMN embedding vector(1024);
ALTER TABLE nodes ADD COLUMN embedded_hash text;
