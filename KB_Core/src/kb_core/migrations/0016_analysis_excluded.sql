-- A user-driven, intentional exclusion from ENTITY ANALYSIS (the entity-extraction LLM passes).
-- Orthogonal to entity_eligible (automatic 'noise' gate) and dead_at (automatic 'stale' retirement):
-- this is a human switch. The flag is set ONLY on the node the user toggled (a folder or a single
-- memory); a node is EFFECTIVELY excluded if it OR ANY ANCESTOR carries the flag, resolved on read
-- via the ltree `path`. An excluded node stays a normal, searchable, enriched memory (summary/tags
-- still run) — only its entity passes are suppressed, and it is skipped by the re-analysis crawl.
ALTER TABLE nodes ADD COLUMN analysis_excluded boolean NOT NULL DEFAULT false;
ALTER TABLE nodes ADD COLUMN analysis_excluded_at timestamptz;
