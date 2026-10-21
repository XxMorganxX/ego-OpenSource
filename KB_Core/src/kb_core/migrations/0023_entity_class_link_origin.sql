-- Provenance of an instance's is_a links: 'derived' (from LLM candidate votes — reconcilable) vs
-- 'human' (a manual reparent/add via the dashboard/API). The reconcile sweep never retracts a
-- 'human' link, so an explicit human typing survives re-analysis.
ALTER TABLE entity_class_links ADD COLUMN origin text NOT NULL DEFAULT 'derived'
    CHECK (origin IN ('derived', 'human'));
