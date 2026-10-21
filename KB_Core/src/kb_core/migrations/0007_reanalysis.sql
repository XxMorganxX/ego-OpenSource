-- Time-driven re-analysis: when the LLM last analyzed a node. NULL = never analyzed.
-- The re-analysis crawler picks the stalest nodes (NULLs first) so, over time, every node
-- is revisited and re-connected as the entity taxonomy evolves.
ALTER TABLE nodes ADD COLUMN last_analyzed_at timestamptz;
CREATE INDEX nodes_last_analyzed ON nodes (last_analyzed_at ASC NULLS FIRST);
