-- Make the analysis-exclusion toggle a live, UI-visible change. The nodes update-notify
-- trigger (0008) fires only when a column the dashboard renders changes; analysis_excluded
-- now drives the file-explorer icon color and the graph node color, so flipping it must
-- push a refetch the same way importance/title/tags do. Recreated in full (the WHEN clause
-- can't be altered in place) with analysis_excluded added.
DROP TRIGGER IF EXISTS nodes_notify_update ON nodes;
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
        OR OLD.analysis_excluded IS DISTINCT FROM NEW.analysis_excluded
    )
    EXECUTE FUNCTION kb_notify_change();
