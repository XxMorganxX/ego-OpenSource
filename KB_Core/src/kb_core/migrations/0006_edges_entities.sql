-- Phase 2: typed, directed, weighted relationships over the node set.
-- Generic by design — one overlay, many relation_types. Entity-mention edges and
-- memory<->memory edges are the SAME table, distinguished by relation_type (and by the
-- target node's type). Tree containment is NOT stored here; it lives in parent_id/path.
CREATE TABLE edges (
    source_id     uuid        NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    target_id     uuid        NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    relation_type text        NOT NULL,           -- 'mentions' (entity), 'relates_to', 'cites', …
    weight        real,                            -- optional strength / confidence
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source_id, target_id, relation_type)
);
-- both-direction traversal; relation_type in the key so neighbors() can filter cheaply.
CREATE INDEX edges_source ON edges (source_id, relation_type);
CREATE INDEX edges_target ON edges (target_id, relation_type);

-- Entities: concept nodes (people, topics, orgs, …) learned from memories. An entity IS
-- a node (type='entity', source='entity', placed under entities.<entity_type>.<slug>);
-- this table is its 1:1 typed extension holding entity-specific fields.
CREATE TABLE entities (
    node_id        uuid PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
    entity_type    text NOT NULL,                  -- 'person' | 'topic' | 'org' | 'project' | …
    canonical_name text NOT NULL,                  -- display name ('Avery')
    aliases        text[] NOT NULL DEFAULT '{}',
    description    text
);
CREATE INDEX entities_type ON entities (entity_type);

-- Candidate entity mentions, staged by the (LLM) enricher. A (entity_type,
-- normalized_name) is promoted to a real entity node only once it recurs across >= N
-- distinct memories (the recurrence threshold). UNIQUE makes staging idempotent per memory.
CREATE TABLE entity_candidates (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    memory_id       uuid NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    entity_type     text NOT NULL,
    normalized_name text NOT NULL,                 -- dedup key (lowercased/trimmed)
    raw_name        text NOT NULL,                 -- as seen in the memory
    confidence      real,
    observed_at     timestamptz NOT NULL DEFAULT now(),
    promoted        boolean NOT NULL DEFAULT false,
    UNIQUE (memory_id, entity_type, normalized_name)
);
CREATE INDEX entity_candidates_group ON entity_candidates (entity_type, normalized_name)
    WHERE promoted = false;
