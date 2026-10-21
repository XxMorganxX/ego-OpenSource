-- Observability: an append-only log of node-lifecycle events during ingestion.
-- FK-free on purpose — the log outlives the nodes it references (it survives prune /
-- delete), so it stays a faithful historical record. It relates to nodes logically by
-- node_id, never by a foreign key.
CREATE TABLE ingest_log (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at           timestamptz NOT NULL DEFAULT now(),
    event        text        NOT NULL,   -- node_created | node_updated | enrichment_done
    source       text        NOT NULL,
    external_id  text,
    node_id      uuid        NOT NULL,
    path         text,
    content_hash text,
    detail       jsonb       NOT NULL DEFAULT '{}'
);
-- "newest activity first" is the default view; node_id for per-node history.
CREATE INDEX ingest_log_at   ON ingest_log (at DESC);
CREATE INDEX ingest_log_node ON ingest_log (node_id);
