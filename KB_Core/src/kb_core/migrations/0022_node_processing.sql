-- Per-node processing state: one current-state row per (node, task), plus durable audit
-- fields (last_done_at, run_count) that survive re-queue. The unified, queryable surface for
-- "what work does this node need / when was it last done". Non-exclusive by construction: a
-- node may have an 'enrichment', a 'recrawl', and (Phase 3) an 'embedding' row at once.
CREATE TABLE node_processing (
    node_id      uuid NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    task         text NOT NULL,            -- 'enrichment' | 'recrawl' | 'embedding'
    status       text NOT NULL DEFAULT 'pending',  -- pending | processing | done | failed
    queued_at    timestamptz NOT NULL DEFAULT now(),
    started_at   timestamptz,
    last_done_at timestamptz,              -- survives re-queue: "when was this last done"
    run_count    integer NOT NULL DEFAULT 0,
    last_error   text,
    attempts     integer NOT NULL DEFAULT 0,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (node_id, task)
);

-- Worker claim path: find pending work fast, per task.
CREATE INDEX node_processing_pending ON node_processing (task, queued_at) WHERE status = 'pending';

-- Enrichment projection: nodes.enrichment_status stays the authoritative writer (the live
-- enrichment daemon is untouched); this trigger mirrors it into a task='enrichment' row. A
-- trigger (not app code) because enrichment_status has 8+ writers; this catches every one.
CREATE OR REPLACE FUNCTION kb_project_enrichment() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO node_processing
        (node_id, task, status, queued_at, updated_at, started_at, last_done_at, run_count)
    VALUES
        (NEW.id, 'enrichment', NEW.enrichment_status, now(), now(),
         CASE WHEN NEW.enrichment_status = 'processing' THEN now() END,
         CASE WHEN NEW.enrichment_status = 'done'       THEN now() END,
         CASE WHEN NEW.enrichment_status = 'done'       THEN 1 ELSE 0 END)
    ON CONFLICT (node_id, task) DO UPDATE SET
        status       = EXCLUDED.status,
        updated_at   = now(),
        started_at   = CASE WHEN EXCLUDED.status = 'processing' THEN now()
                            ELSE node_processing.started_at END,
        last_done_at = CASE WHEN EXCLUDED.status = 'done' THEN now()
                            ELSE node_processing.last_done_at END,
        run_count    = node_processing.run_count
                       + CASE WHEN EXCLUDED.status = 'done' THEN 1 ELSE 0 END;
    RETURN NULL;
END;
$$;

-- 'none' is the pre-enrichment default: skip it so brand-new nodes get no row until flagged.
-- Split into INSERT + UPDATE triggers because TG_OP is invalid in a WHEN clause and OLD is
-- undefined for INSERT rows. INSERT fires only when a node is created with a non-'none' status.
-- UPDATE fires only when enrichment_status actually transitions (OLD <> NEW guard).
CREATE TRIGGER nodes_project_enrichment_insert
    AFTER INSERT ON nodes
    FOR EACH ROW
    WHEN ( NEW.enrichment_status <> 'none' )
    EXECUTE FUNCTION kb_project_enrichment();

CREATE TRIGGER nodes_project_enrichment_update
    AFTER UPDATE OF enrichment_status ON nodes
    FOR EACH ROW
    WHEN ( NEW.enrichment_status <> 'none'
           AND OLD.enrichment_status IS DISTINCT FROM NEW.enrichment_status )
    EXECUTE FUNCTION kb_project_enrichment();

-- Live UI updates: a node_processing change pushes a 'kb_change' refetch keyed on node_id,
-- the same mechanism the dashboard already uses for nodes/ingest_log (0008_change_notify.sql).
CREATE OR REPLACE FUNCTION kb_notify_node_processing() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE rec record;
BEGIN
    rec := CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
    PERFORM pg_notify('kb_change',
        json_build_object('table', 'node_processing', 'op', TG_OP, 'id', rec.node_id)::text);
    RETURN NULL;
END;
$$;
CREATE TRIGGER node_processing_notify
    AFTER INSERT OR UPDATE OR DELETE ON node_processing
    FOR EACH ROW EXECUTE FUNCTION kb_notify_node_processing();

-- Backfill so audit history is populated on day one, not blank.
-- enrichment: one row per node already considered (status <> 'none').
INSERT INTO node_processing (node_id, task, status, queued_at, last_done_at, run_count, updated_at)
SELECT id, 'enrichment', enrichment_status, ingested_at, enriched_at,
       CASE WHEN enriched_at IS NOT NULL THEN 1 ELSE 0 END, now()
FROM nodes WHERE enrichment_status <> 'none';

-- recrawl: one 'done' row per node already analyzed at least once.
INSERT INTO node_processing (node_id, task, status, queued_at, last_done_at, run_count, updated_at)
SELECT id, 'recrawl', 'done', last_analyzed_at, last_analyzed_at, 1, now()
FROM nodes WHERE last_analyzed_at IS NOT NULL
ON CONFLICT (node_id, task) DO NOTHING;
