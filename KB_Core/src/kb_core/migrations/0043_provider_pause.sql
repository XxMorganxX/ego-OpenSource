-- Provider outage pauses: while a row's `until` is in the future, the enrichment
-- lane does not claim work whose passes route to that provider. Written by
-- KnowledgeBase.pause_provider (worker threads hitting ProviderUnavailable), read
-- by the ingest daemon's enrich tick, the healthcheck, and /api/ingest/status.
CREATE TABLE provider_pause (
    provider   text        PRIMARY KEY,
    until      timestamptz NOT NULL,
    reason     text        NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
