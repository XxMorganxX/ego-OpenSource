-- Runs once, on first cluster initialization (empty kb_pgdata volume).
-- Enables the extensions kb-mcp relies on. The app's own SQL migrations also
-- `CREATE EXTENSION IF NOT EXISTS ltree`, but doing it here makes the cluster
-- ready immediately and pre-creates pgvector for the embedding columns.
CREATE EXTENSION IF NOT EXISTS ltree;
CREATE EXTENSION IF NOT EXISTS vector;
