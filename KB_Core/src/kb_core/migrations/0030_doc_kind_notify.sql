-- 0029 added nodes.doc_kind, but the nodes_notify_update trigger's change-list (0008, extended
-- by 0017 for analysis_excluded) predates it, so a doc_kind-only update (the classification pass
-- writing a new/changed kind) matches no WHEN condition and fires no NOTIFY -- the dashboard's
-- live feed never refreshes for classifications. Recreate the trigger with
-- OLD.doc_kind IS DISTINCT FROM NEW.doc_kind added to the change conditions; everything else is
-- unchanged from 0017 (the WHEN clause can't be altered in place).
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
        OR OLD.doc_kind          IS DISTINCT FROM NEW.doc_kind
    )
    EXECUTE FUNCTION kb_notify_change();
