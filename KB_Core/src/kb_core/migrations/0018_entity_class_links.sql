-- Phase 2: decouple an instance's CATEGORY from its identity. Previously an instance node was
-- identified by (category, name) and lived at entities.<category>.<name>, so the same real
-- entity fragmented into one node per category (Vandelay as project + org + software + …).
-- Now an instance is ONE canonical node (identity = name only, entity_type='instance', placed
-- flat under entities.instances) and its category memberships are many-to-many LINKS to class
-- nodes. An instance can belong to several classes; a class has many instances.
--
-- This is the instance<->class overlay, kept OUT of `entity_links` (which stays memory->entity)
-- and `edges` (memory<->memory), mirroring how 0010 separated those concerns.
CREATE TABLE IF NOT EXISTS entity_class_links (
    instance_id uuid        NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    class_id    uuid        NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    weight      real,                            -- confidence carried from the candidate
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (instance_id, class_id)
);
CREATE INDEX IF NOT EXISTS entity_class_links_class ON entity_class_links (class_id);       -- "instances of class C"
CREATE INDEX IF NOT EXISTS entity_class_links_instance ON entity_class_links (instance_id); -- "classes of instance X"
