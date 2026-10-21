-- Derived concept layer (consolidation). A concept is a Leiden community discovered over
-- the memory<->entity mention graph + memory-embedding kNN edges, labeled by the LLM.
-- Concepts are DERIVED and DISPOSABLE: every consolidation run recomputes the set, carries
-- ids for communities that overlap a previous concept, and RETIRES (never deletes) the
-- rest. Deliberately NOT nodes — no ingestion/enrichment state, no ltree path, no
-- reidentify exposure; the layer can be dropped and rebuilt at any time. Membership is
-- soft (a score, not a filing decision); the tree in `nodes` stays the only hierarchy of
-- record for memories.

CREATE TABLE concept_runs (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    status      text NOT NULL DEFAULT 'running'
                CHECK (status IN ('running', 'done', 'failed')),
    params      jsonb NOT NULL DEFAULT '{}'::jsonb,   -- knobs the run used (audit/repro)
    stats       jsonb NOT NULL DEFAULT '{}'::jsonb    -- counts incl. discarded communities
);

CREATE TABLE concepts (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name          text NOT NULL,
    description   text,
    kind          text NOT NULL DEFAULT 'other'
                  CHECK (kind IN ('project', 'topic', 'area', 'event', 'other')),
    level         int  NOT NULL DEFAULT 0,             -- 0 top-level, 1 sub-community
    parent_id     uuid REFERENCES concepts(id) ON DELETE SET NULL,
    status        text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retired')),
    embedding     vector(1024),                        -- of name+description (read path later)
    member_count  int NOT NULL DEFAULT 0,
    last_run_id   bigint REFERENCES concept_runs(id),
    first_seen_at timestamptz NOT NULL DEFAULT now(),  -- survives id carry-over across runs
    updated_at    timestamptz NOT NULL DEFAULT now(),
    retired_at    timestamptz
);
CREATE INDEX concepts_active ON concepts (status) WHERE status = 'active';

-- Soft, scored membership: memories AND entities of the community, replaced every run.
CREATE TABLE concept_members (
    concept_id  uuid NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    node_id     uuid NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    member_kind text NOT NULL CHECK (member_kind IN ('memory', 'entity')),
    score       real NOT NULL,             -- fraction of the member's edge weight inside
    PRIMARY KEY (concept_id, node_id)
);
CREATE INDEX concept_members_node ON concept_members (node_id);
