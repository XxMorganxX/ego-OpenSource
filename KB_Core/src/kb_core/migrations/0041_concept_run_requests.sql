-- Manual consolidation trigger. A durable queue of "rebuild the concept layer now"
-- requests: the dashboard inserts one and NOTIFYs; kb-mcp-consolidated claims every
-- unclaimed row on its next wake and serves them with ONE forced pass. Durable rather
-- than a bare NOTIFY for two reasons: a daemon restart would silently swallow the
-- click, and the UI needs state it can read so "Consolidating..." survives a reload.
CREATE TABLE concept_run_requests (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    requested_at timestamptz NOT NULL DEFAULT now(),
    source       text NOT NULL DEFAULT 'dashboard',
    claimed_at   timestamptz,                          -- daemon picked it up
    finished_at  timestamptz,                          -- the serving pass ended
    run_id       bigint REFERENCES concept_runs(id),
    error        text
);
-- The daemon's hot read: "is anything waiting?"
CREATE INDEX concept_run_requests_unclaimed
    ON concept_run_requests (requested_at) WHERE claimed_at IS NULL;

-- Which trigger fired a run. 'churn'/'backstop' come from should_consolidate, 'manual'
-- from a dashboard request, 'cli' from kb-consolidate --apply. concept_runs could not
-- distinguish these before. `phase` is coarse in-flight progress: graph|labeling|applying.
ALTER TABLE concept_runs ADD COLUMN trigger_reason text NOT NULL DEFAULT 'churn';
ALTER TABLE concept_runs ADD COLUMN phase text;

-- Put concept state on the live change feed (0008_change_notify.sql), which until now
-- carried only nodes / ingest_log / node_processing. Every transition -- request queued,
-- run opened, phase stamped, run finished -- becomes a WebSocket push, so the rebuild
-- button needs no polling loop.
CREATE TRIGGER concept_runs_notify
    AFTER INSERT OR UPDATE ON concept_runs
    FOR EACH ROW EXECUTE FUNCTION kb_notify_change();
CREATE TRIGGER concept_run_requests_notify
    AFTER INSERT OR UPDATE ON concept_run_requests
    FOR EACH ROW EXECUTE FUNCTION kb_notify_change();
