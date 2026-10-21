-- Live retrieval telemetry: one row per production search call (search_graph /
-- search_memories through KB_MCP). Written fire-and-forget by
-- KnowledgeBase.log_retrieval -- a failed write must never break a search -- and read
-- by the dashboard's /api/retrieval/telemetry endpoints. Unbounded retention by
-- design (single user, low volume); revisit if it ever matters.
CREATE TABLE retrieval_log (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at               timestamptz NOT NULL DEFAULT now(),
    tool             text NOT NULL,
    strategy         text,
    judge            text,
    crawl_mode       text,
    query            text NOT NULL,
    latency_ms       integer NOT NULL,
    confidence_level text,
    best_similarity  real,
    results          integer NOT NULL,
    llm_calls        integer NOT NULL DEFAULT 0,
    embedder_ok      boolean NOT NULL DEFAULT true
);

CREATE INDEX retrieval_log_at_idx ON retrieval_log (at DESC);

-- Same live-feed pattern as 0008: append-only table, NOTIFY on every committed insert
-- so the Retrieval page refreshes over the existing WebSocket without polling.
CREATE TRIGGER retrieval_log_notify_change
    AFTER INSERT ON retrieval_log
    FOR EACH ROW EXECUTE FUNCTION kb_notify_change();
