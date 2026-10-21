-- Fuzzy name matching for entity resolution (fuzzy-match rung): pg_trgm similarity
-- catches typos ('Vandelaly' -> 'Vandelay') where ILIKE substring finds nothing.
-- The trgm GIN index keeps similarity scans fast; at current scale it's principle
-- more than necessity.
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX entities_name_trgm_idx ON entities USING gin (canonical_name gin_trgm_ops);
