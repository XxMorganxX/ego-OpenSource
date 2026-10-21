-- Audit log for approved entity merges. Unlike entity_merge_candidates, this table has no FK
-- to the source entity's node (which is tombstoned during the merge), so approved merges are
-- permanently recorded here regardless of CASCADE deletions on the candidates table.
CREATE TABLE IF NOT EXISTS entity_merges (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    survivor_id  uuid REFERENCES nodes(id) ON DELETE SET NULL,  -- the kept entity
    source_name  text NOT NULL,        -- folded entity's canonical_name (its node is gone)
    kind         text NOT NULL,        -- 'instance' | 'class'
    distance     real,                 -- cosine distance at suggestion time
    judge_reason text,
    approved_at  timestamptz NOT NULL DEFAULT now()
);
