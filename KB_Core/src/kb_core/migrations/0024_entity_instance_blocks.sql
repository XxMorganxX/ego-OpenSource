-- "Never promote this instance name again." Written by the meaningfulness judge (origin
-- 'judge') when a recurring candidate is deemed a code symbol, and by the denoise backfill /
-- a human (origin 'human'). Keyed by normalized_name alone — instance identity is name-only
-- (category is a link), so a block holds across every category the name was staged under.
-- Delete a row to let the name be reconsidered on a later sweep.
CREATE TABLE entity_instance_blocks (
    normalized_name  text PRIMARY KEY,
    reason           text,
    origin           text NOT NULL DEFAULT 'judge' CHECK (origin IN ('judge', 'human')),
    created_at       timestamptz NOT NULL DEFAULT now()
);
