-- Push capture: durable local staging of webhook events.
CREATE TABLE ingest_inbox (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source       text        NOT NULL,
    external_id  text,                                  -- source's stable id (Principle #3)
    event_type   text        NOT NULL DEFAULT 'upsert', -- 'upsert' | 'delete'
    payload      jsonb       NOT NULL DEFAULT '{}',
    received_at  timestamptz NOT NULL DEFAULT now(),
    processed_at timestamptz,                           -- NULL = still pending
    status       text        NOT NULL DEFAULT 'pending',-- pending | processed | failed
    error        text,
    attempts     int         NOT NULL DEFAULT 0
);
CREATE INDEX ingest_inbox_pending ON ingest_inbox (source, received_at)
    WHERE processed_at IS NULL;

-- Two-phase enrichment state (system-managed; never on NodeRecord).
ALTER TABLE nodes
    ADD COLUMN enrichment_status text NOT NULL DEFAULT 'none',  -- none|pending|processing|done|failed
    ADD COLUMN enriched_at       timestamptz,
    ADD COLUMN enriched_hash     text;                          -- content_hash at last enrichment
CREATE INDEX nodes_enrichment_pending ON nodes (enrichment_status)
    WHERE enrichment_status = 'pending';
