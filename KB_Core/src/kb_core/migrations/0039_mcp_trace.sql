-- Full MCP retrieval tracing: one row per retrieval-shaped tool call through
-- KB_MCP (search_memories, search_graph, find_entity, expand, entity_activity,
-- get_memory), written fire-and-forget by KnowledgeBase.log_trace -- a failed
-- write must never break a tool call. Supersedes 0038's retrieval_log, which
-- recorded that a search happened but not what came back.
-- Unbounded retention by design (single user, low volume; same ruling as 0038).
CREATE TABLE mcp_trace (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at               timestamptz NOT NULL DEFAULT now(),
    request_id       uuid NOT NULL,
    session_id       text,
    caller           text,          -- NULL = real traffic; eval harness stamps 'eval'
    tool             text NOT NULL,
    args             jsonb NOT NULL,
    query            text,
    target_id        uuid,          -- subject of get_memory/expand/entity_activity
    latency_ms       integer NOT NULL,
    results          jsonb,         -- [{id, kind, rank, score, similarity, via}, ...]
    result_count     integer NOT NULL DEFAULT 0,
    confidence_level text,
    best_similarity  real,
    strategy         text,
    judge            text,
    crawl_mode       text,
    trace            jsonb,         -- search_graph crawl trace + applied/ignored filters
    llm_calls        integer NOT NULL DEFAULT 0,
    embedder_ok      boolean NOT NULL DEFAULT true,
    error            text           -- the tool raised; failed retrievals are eval data too
);

CREATE INDEX mcp_trace_at_idx      ON mcp_trace (at DESC);
CREATE INDEX mcp_trace_session_idx ON mcp_trace (session_id, at);
-- results @> '[{"id": "..."}]' is the implicit-metric workhorse (follow-up rate).
CREATE INDEX mcp_trace_results_gin ON mcp_trace USING gin (results jsonb_path_ops);

-- Same live-feed pattern as 0008/0038: NOTIFY on every committed insert so the
-- Retrieval page refreshes over the existing WebSocket without polling.
CREATE TRIGGER mcp_trace_notify_change
    AFTER INSERT ON mcp_trace
    FOR EACH ROW EXECUTE FUNCTION kb_notify_change();

-- Adjudications are append-only: re-judging inserts a new row; readers take the
-- latest per judged_by. Closed verdict set on purpose -- this is a labeling
-- instrument, not an emergent vocabulary.
CREATE TABLE retrieval_judgment (
    id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    trace_id  bigint NOT NULL REFERENCES mcp_trace(id) ON DELETE CASCADE,
    verdict   text NOT NULL CHECK (verdict IN ('good', 'partial', 'bad')),
    judged_by text NOT NULL,        -- 'owner' | 'llm:<model>' | 'script:<name>'
    notes     text,
    at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX retrieval_judgment_trace_idx ON retrieval_judgment (trace_id);

-- Carry the 0038-era telemetry over (args synthesized from the one recorded
-- field; results were never recorded), then retire the superseded table.
INSERT INTO mcp_trace (at, request_id, tool, args, query, latency_ms,
                       result_count, confidence_level, best_similarity,
                       strategy, judge, crawl_mode, llm_calls, embedder_ok)
SELECT at, gen_random_uuid(), tool, jsonb_build_object('query', query), query,
       latency_ms, results, confidence_level, best_similarity,
       strategy, judge, crawl_mode, llm_calls, embedder_ok
FROM retrieval_log;

DROP TABLE retrieval_log;
