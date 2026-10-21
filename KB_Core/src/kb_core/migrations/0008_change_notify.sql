-- Live change feed: NOTIFY on every committed nodes / ingest_log mutation so the
-- dashboard can push updates to the UI over a WebSocket. Cross-process by construction --
-- pg_notify fires at COMMIT regardless of which connection made the change (ingest
-- daemon, dashboard API, or manual SQL). The payload is a tiny {table, op, id}; the UI
-- treats it as a debounced "refetch this view" signal, not a row delta.
CREATE OR REPLACE FUNCTION kb_notify_change() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    rec record;
BEGIN
    rec := CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
    PERFORM pg_notify(
        'kb_change',
        json_build_object('table', TG_TABLE_NAME, 'op', TG_OP, 'id', rec.id)::text
    );
    RETURN NULL;  -- AFTER trigger: the return value is ignored
END;
$$;

-- ingest_log is append-only: every insert is a new lifecycle/observability event.
CREATE TRIGGER ingest_log_notify_change
    AFTER INSERT ON ingest_log
    FOR EACH ROW EXECUTE FUNCTION kb_notify_change();

-- nodes: creations and deletions always matter. Updates fire only when a UI-visible
-- column actually changes -- NOT on the per-read access_count/last_accessed_at bump, which
-- would otherwise push a refetch on every agent retrieval (and risk a refetch->read->notify
-- loop). importance is included so the graph's importance coloring stays live.
CREATE TRIGGER nodes_notify_insert
    AFTER INSERT ON nodes
    FOR EACH ROW EXECUTE FUNCTION kb_notify_change();

CREATE TRIGGER nodes_notify_delete
    AFTER DELETE ON nodes
    FOR EACH ROW EXECUTE FUNCTION kb_notify_change();

CREATE TRIGGER nodes_notify_update
    AFTER UPDATE ON nodes
    FOR EACH ROW
    WHEN (
        OLD.content_hash         IS DISTINCT FROM NEW.content_hash
        OR OLD.title             IS DISTINCT FROM NEW.title
        OR OLD.path              IS DISTINCT FROM NEW.path
        OR OLD.parent_id         IS DISTINCT FROM NEW.parent_id
        OR OLD.slug              IS DISTINCT FROM NEW.slug
        OR OLD.type              IS DISTINCT FROM NEW.type
        OR OLD.tags              IS DISTINCT FROM NEW.tags
        OR OLD.summary           IS DISTINCT FROM NEW.summary
        OR OLD.uri               IS DISTINCT FROM NEW.uri
        OR OLD.enrichment_status IS DISTINCT FROM NEW.enrichment_status
        OR OLD.importance        IS DISTINCT FROM NEW.importance
    )
    EXECUTE FUNCTION kb_notify_change();
