-- Entity lifecycle gets a third state. The 0009 `active` boolean could only say
-- discovered-but-unproven (false) vs established (true); it had no way to record a human
-- decision to REMOVE a class permanently. A recurring hallucination (a small model that
-- emits the same junk category across many memories) sails through the recurrence gate,
-- activates, and — because activation was monotonic and there was no delete — could never
-- be taken back out. Replace the boolean with a status that adds a `blocked` tombstone:
--   inactive — discovered, not yet recurred past the threshold (was active=false)
--   active   — established / offered as a category          (was active=true)
--   blocked  — human-removed; never offered, never revived, never re-staged
ALTER TABLE entities ADD COLUMN status text NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'inactive', 'blocked'));
UPDATE entities SET status = CASE WHEN active THEN 'active' ELSE 'inactive' END;

ALTER TABLE entities DROP COLUMN active;
DROP INDEX IF EXISTS entities_type_active;

-- The hot read is "active classes, ranked" (entity_class_names) — a composite covers the
-- (entity_type, status) filter and still serves the existing entity_type-only lookups.
CREATE INDEX entities_type_status ON entities (entity_type, status);
