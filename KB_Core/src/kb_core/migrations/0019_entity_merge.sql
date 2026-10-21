-- Phase 3 (entities): a derived semantic-identity layer. Each entity carries a vector built
-- from its name + mention context; near-duplicate same-kind pairs are surfaced as reviewable
-- merge suggestions (never auto-merged).

CREATE EXTENSION IF NOT EXISTS vector;

ALTER TABLE entities
    ADD COLUMN IF NOT EXISTS embedding      vector(1024),
    ADD COLUMN IF NOT EXISTS embed_dirty    boolean NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS embed_text_hash text;

-- KNN over the (incrementally growing) entity set; cosine distance, no train step.
CREATE INDEX IF NOT EXISTS entities_embedding_hnsw
    ON entities USING hnsw (embedding vector_cosine_ops);

-- Pending/resolved merge suggestions. A pair is stored canonical-ordered (min,max by uuid text)
-- so it dedups regardless of which side was discovered first.
CREATE TABLE IF NOT EXISTS entity_merge_candidates (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    left_id             uuid NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    right_id            uuid NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    kind                text NOT NULL,                 -- 'instance' | 'class'
    distance            real NOT NULL,
    judge_same          boolean NOT NULL,
    judge_reason        text,
    suggested_target_id uuid REFERENCES nodes(id) ON DELETE CASCADE,
    status              text NOT NULL DEFAULT 'pending', -- pending | approved | rejected
    created_at          timestamptz NOT NULL DEFAULT now(),
    resolved_at         timestamptz,
    UNIQUE (left_id, right_id)
);
CREATE INDEX IF NOT EXISTS entity_merge_candidates_pending
    ON entity_merge_candidates (kind) WHERE status = 'pending';

-- "Never suggest again." A judge 'not same' (origin='judge') or a human/agent reject
-- (origin='human') lands here; the scan skips any pair present.
CREATE TABLE IF NOT EXISTS entity_merge_blocks (
    left_id    uuid NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    right_id   uuid NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    origin     text NOT NULL,                          -- 'judge' | 'human'
    reason     text,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (left_id, right_id)
);
