-- All memory -> entity links live here, OUT of the generic `edges` table. An entity node is a
-- class (entity_type='class') or an instance; link_type names the relationship. This keeps
-- `edges` as a pure memory<->memory overlay.
CREATE TABLE entity_links (
    memory_id   uuid        NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    entity_id   uuid        NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    link_type   text        NOT NULL,            -- 'belongs_to' (class) | 'mentions' (instance)
    weight      real,                            -- confidence carried from the candidate
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (memory_id, entity_id, link_type)
);
CREATE INDEX entity_links_entity ON entity_links (entity_id, link_type);  -- "memories about X"
CREATE INDEX entity_links_memory ON entity_links (memory_id, link_type);  -- "entities of memory M"

-- Relocate any existing entity links from `edges`, then leave `edges` memory<->memory only.
INSERT INTO entity_links (memory_id, entity_id, link_type, weight, created_at, updated_at)
SELECT source_id, target_id, relation_type, weight, created_at, updated_at
FROM edges WHERE relation_type IN ('belongs_to', 'mentions');
DELETE FROM edges WHERE relation_type IN ('belongs_to', 'mentions');
