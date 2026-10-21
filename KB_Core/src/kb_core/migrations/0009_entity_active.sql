-- Recurrence gate for entity CLASSES (broad categories). A class discovered by the LLM is
-- now created INACTIVE and only flipped active once it recurs across >= N distinct memories
-- (the same threshold instances earn via promotion). `active` is the flag the discover pass
-- and classifier read: only active classes are offered as "the categories we have".
-- Instances and explicitly-created classes are real on sight, so they're active; existing
-- rows predate the gate and are likewise backfilled active.
ALTER TABLE entities ADD COLUMN active boolean NOT NULL DEFAULT false;
UPDATE entities SET active = true;

-- The hot read is "active classes" (entity_class_names) — a composite covers it and still
-- serves the existing entity_type-only lookups.
CREATE INDEX entities_type_active ON entities (entity_type, active);
