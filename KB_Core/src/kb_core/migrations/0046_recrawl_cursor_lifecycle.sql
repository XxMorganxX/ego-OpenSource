-- Close the lifecycle gaps around the 0045 dependency cursors.
--
-- `recrawl_cursor_adoption_pending` identifies the exact pre-cursor population. A plain
-- `analyzed_* IS NULL` predicate cannot do that: nodes enriched after 0045 also start NULL,
-- and adopting them on a later daemon restart can swallow a vocabulary change they never saw.
--
-- `attempted_*` records a failed run under a specific vocabulary. The analyzed cursor remains
-- untouched (the node did not converge), while the attempted cursor prevents the same poisoned
-- node from being selected every tick. A later vocabulary hash differs and makes it due again.
ALTER TABLE nodes ADD COLUMN recrawl_cursor_adoption_pending boolean NOT NULL DEFAULT false;
ALTER TABLE nodes ADD COLUMN attempted_class_hash text;
ALTER TABLE nodes ADD COLUMN attempted_kind_hash  text;
ALTER TABLE nodes ADD COLUMN last_gate_analyzed_at timestamptz;

-- Before this migration every recrawl ran the gate, so the old general clock is the exact
-- historical gate clock. From here onward only an executed gate pass advances this column.
UPDATE nodes SET last_gate_analyzed_at = last_analyzed_at
WHERE last_analyzed_at IS NOT NULL;

UPDATE nodes
SET recrawl_cursor_adoption_pending = true
WHERE last_analyzed_at IS NOT NULL
  AND analyzed_class_hash IS NULL
  AND analyzed_kind_hash IS NULL;
