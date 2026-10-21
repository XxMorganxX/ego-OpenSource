-- Turn grouping: the MCP tool calls one agent made for ONE user request share a
-- turn_id -- the id of the turn's first call (its opener), so the opener's turn_id is
-- its own id. Two expressions of one rule:
--   * incremental, at write time (KnowledgeBase.log_trace): inherit the turn of the
--     latest call in the same session whose end is within TURN_GAP_S before this
--     call's start, else open a turn;
--   * batch, here (mcp_trace_rebuild_turns): the same rule over every row, used by
--     this migration to backfill history and by `kb-turns-rebuild` after the
--     constant is retuned.
-- The gap runs from the previous call's END (`at` is written after the call
-- completes) to the next call's START (`at - latency_ms`). Rows with no session (the
-- rows carried over from the older retrieval_log) each stand alone.
ALTER TABLE mcp_trace ADD COLUMN turn_id bigint REFERENCES mcp_trace(id);

CREATE OR REPLACE FUNCTION mcp_trace_rebuild_turns(gap_seconds integer) RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
    moved integer := 0;
    alone integer := 0;
BEGIN
    -- Reads continue; concurrent log_trace writes wait for the rebuild (milliseconds
    -- at today's size; log_trace has a 2 s lock_timeout and tracing is advisory).
    -- This EXCLUSIVE lock and the ROW EXCLUSIVE lock log_trace takes BEFORE its turn
    -- lookup together make the rebuild race-free: a write can no longer read a
    -- pre-rebuild turn_id and land it afterwards. EXCLUSIVE alone would not do it --
    -- it lets plain SELECTs through, so a lookup could still read stale ids and only
    -- its INSERT would wait. Inside this migration's own transaction the lock is a
    -- no-op: ADD COLUMN already holds ACCESS EXCLUSIVE.
    LOCK TABLE mcp_trace IN EXCLUSIVE MODE;
    WITH ordered AS (
        SELECT id, session_id, at, latency_ms,
               lag(at) OVER (PARTITION BY session_id ORDER BY at, id) AS prev_end
        FROM mcp_trace
        WHERE session_id IS NOT NULL
    ), flagged AS (
        SELECT id, session_id, at,
               CASE WHEN prev_end IS NULL
                      OR (at - latency_ms * interval '1 millisecond') - prev_end
                         > gap_seconds * interval '1 second'
                    THEN 1 ELSE 0 END AS opens
        FROM ordered
    ), islands AS (
        SELECT id, session_id, at,
               sum(opens) OVER (PARTITION BY session_id ORDER BY at, id) AS island
        FROM flagged
    ), assigned AS (
        -- first_value, not min(id): an id/at inversion from a pre-lock race must
        -- never pick a non-opener.
        SELECT id,
               first_value(id) OVER (PARTITION BY session_id, island ORDER BY at, id)
                   AS turn_id
        FROM islands
    )
    UPDATE mcp_trace m SET turn_id = a.turn_id
    FROM assigned a
    WHERE m.id = a.id AND m.turn_id IS DISTINCT FROM a.turn_id;
    GET DIAGNOSTICS moved = ROW_COUNT;

    UPDATE mcp_trace SET turn_id = id
    WHERE session_id IS NULL AND turn_id IS DISTINCT FROM id;
    GET DIAGNOSTICS alone = ROW_COUNT;

    RETURN moved + alone;
END;
$$;

-- Backfill history at the gap in force on the day this shipped. Later retuning goes
-- through `kb-turns-rebuild`, which passes knowledge_base.TURN_GAP_S.
SELECT mcp_trace_rebuild_turns(90);

ALTER TABLE mcp_trace ALTER COLUMN turn_id SET NOT NULL;
CREATE INDEX mcp_trace_turn_idx ON mcp_trace (turn_id);

-- Request-level adjudication: same shape and rules as retrieval_judgment (0039),
-- keyed by the turn's opener. Append-only; readers take the latest verdict (across
-- judges), newest first.
-- A CHECK cannot see another row, so "turn_id is an opener" is enforced by
-- KnowledgeBase.add_turn_judgment.
CREATE TABLE turn_judgment (
    id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    turn_id   bigint NOT NULL REFERENCES mcp_trace(id) ON DELETE CASCADE,
    verdict   text NOT NULL CHECK (verdict IN ('good', 'partial', 'bad')),
    judged_by text NOT NULL,
    notes     text,
    at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX turn_judgment_turn_idx ON turn_judgment (turn_id);
