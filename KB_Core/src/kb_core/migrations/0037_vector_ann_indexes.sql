-- HNSW indexes for the hybrid search kNN arm.
--
-- These are useless on their own, and were deliberately NOT added until now. The
-- `cand` CTE used to compute greatest(own vector, correlated max over its chunks) in
-- a single pass over `nodes`, with no ORDER BY/LIMIT on a distance expression — so
-- there was nothing for an ANN index to answer. Verified before this change by
-- running the identical shape against `entities`, which has had
-- entities_embedding_hnsw since migration 0019: still a Seq Scan, even with
-- enable_seqscan = off. The query had to be reshaped into two independently
-- ANN-ordered legs first (knowledge_base.py, `cand`); these indexes serve those legs.
--
-- vector_cosine_ops matches the `<=>` operator the search uses. Both columns are
-- vector(1024) — comfortably under pgvector's 2000-dim indexing ceiling.
--
-- Exact search stays correct without these; they only change how it is reached. At
-- personal scale (hundreds of notes, a few thousand chunk vectors) Postgres will often still
-- choose a sequential scan because the tables are tiny, and that is the right call —
-- the point of this migration is that the choice now EXISTS. It starts paying at a
-- few thousand notes.
--
-- Build cost is real (HNSW is the slow-build/fast-probe end of the tradeoff) but
-- trivial at this size. If these are ever rebuilt on a large corpus, raise
-- maintenance_work_mem first or the build spills and takes far longer.
CREATE INDEX IF NOT EXISTS nodes_embedding_hnsw
    ON nodes USING hnsw (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS node_chunks_embedding_hnsw
    ON node_chunks USING hnsw (embedding vector_cosine_ops);
