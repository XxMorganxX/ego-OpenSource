-- A memory whose content is essentially NOISE — random/mashed characters, accidental input,
-- test/placeholder filler, an empty fragment: nothing a reader could extract — is excluded from
-- the entity system. It casts no votes, creates/links no entities, and any influence it already
-- cast is retracted (see set_entity_eligible). It stays a normal, searchable memory; only its
-- participation in entities is suppressed.
--
-- This is a NOISE gate, NOT a richness/length one: a brief but genuine note (a one-line to-do, a
-- name, a single fact) is low-signal yet not noise, so it keeps participating. The verdict is set
-- by the analysis 'entity_gate' pass and defaults to eligible (fail-open — a missing verdict never
-- silently drops a real memory). `entity_skip_reason` records why, for the dashboard/audit.
ALTER TABLE nodes ADD COLUMN entity_eligible boolean NOT NULL DEFAULT true;
ALTER TABLE nodes ADD COLUMN entity_skip_reason text;
