-- 0034_entity_kind.sql — structural kind axis + event/role plumbing.
-- kind is a small closed STRUCTURAL axis (like concepts.kind); open-vocab classes remain
-- the domain taxonomy. NULL = not yet derived.
ALTER TABLE entities ADD COLUMN kind text
    CHECK (kind IN ('person','organization','place','event','project',
                    'artifact','concept','system','other'));
CREATE INDEX entities_kind ON entities (kind) WHERE kind IS NOT NULL;

-- When the note states when an event happened, the anchor carries it.
ALTER TABLE entities ADD COLUMN occurred_at timestamptz;
ALTER TABLE entity_candidates ADD COLUMN occurred_at timestamptz;

-- Role qualifier on a mention (how the entity relates to the memory).
ALTER TABLE entity_candidates ADD COLUMN role text;
ALTER TABLE entity_links ADD COLUMN role text;
