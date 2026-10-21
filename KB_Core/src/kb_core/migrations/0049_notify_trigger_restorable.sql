-- nodes_notify_update (0030) did not survive a backup restore. pg_restore replays
-- pg_get_triggerdef() with search_path = '', and `OLD.path IS DISTINCT FROM NEW.path` then has
-- to find ltree's `=` operator, which lives in `public`: "operator does not exist:
-- public.ltree = public.ltree". The CREATE TRIGGER failed, so every restored database had
-- no live-change NOTIFY for node updates. IS DISTINCT FROM has no OPERATOR(schema.op)
-- spelling, so compare the path's text form instead. text `=` is in pg_catalog, which is
-- always searched, and ltree's text output is canonical, so the comparison is unchanged.
-- Every other condition is copied from 0030 (a WHEN clause can't be altered in place).
DROP TRIGGER IF EXISTS nodes_notify_update ON nodes;

CREATE TRIGGER nodes_notify_update
    AFTER UPDATE ON nodes
    FOR EACH ROW
    WHEN (
        OLD.content_hash         IS DISTINCT FROM NEW.content_hash
        OR OLD.title             IS DISTINCT FROM NEW.title
        OR OLD.path::text        IS DISTINCT FROM NEW.path::text
        OR OLD.parent_id         IS DISTINCT FROM NEW.parent_id
        OR OLD.slug              IS DISTINCT FROM NEW.slug
        OR OLD.type              IS DISTINCT FROM NEW.type
        OR OLD.tags              IS DISTINCT FROM NEW.tags
        OR OLD.summary           IS DISTINCT FROM NEW.summary
        OR OLD.uri               IS DISTINCT FROM NEW.uri
        OR OLD.enrichment_status IS DISTINCT FROM NEW.enrichment_status
        OR OLD.importance        IS DISTINCT FROM NEW.importance
        OR OLD.analysis_excluded IS DISTINCT FROM NEW.analysis_excluded
        OR OLD.doc_kind          IS DISTINCT FROM NEW.doc_kind
    )
    EXECUTE FUNCTION kb_notify_change();
