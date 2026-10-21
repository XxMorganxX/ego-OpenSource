-- A memory the analysis 'entity_gate' pass judges no longer chronologically relevant — a passed
-- event, a long-overdue one-off task, a "current" status now plainly outdated — is RETIRED (dead):
-- frozen as-is. Distinct from a noise memory: a dead memory's past influence on the entity system
-- is KEPT (never retracted), because it mattered once. Going forward it casts no new votes/links
-- and is never crawled again (re-analysis skips it), until a human revives it.
--
-- dead_at NULL = alive; a non-null dead_at = retired (and when it happened); dead_reason records
-- why. Revive sets dead_at back to NULL. Noise (entity_eligible) and dead are orthogonal axes; a
-- node's display lifecycle derives as dead > noise > active.
ALTER TABLE nodes ADD COLUMN dead_at timestamptz;
ALTER TABLE nodes ADD COLUMN dead_reason text;
