-- Live feed for the dashboard Skills page:
-- an agent's upsert_skill / delete_skill refreshes every open Skills tab.
--
-- A dedicated function, NOT the shared kb_notify_change() (0008): that one builds its
-- payload from rec.id, and `skills` is keyed by `name` with no `id` column, so attaching
-- it would raise inside every skill write (see 0040_mcp_tool_state.sql). The frontend's
-- change feed reads only `table`, so `id` carrying the name is harmless.
--
-- `skills` alone is enough: every upsert updates current_revision/updated_at and every
-- archive updates archived_at, in the same transaction as the revision rows, and NOTIFY
-- is delivered at commit. The body uses only pg_catalog operators, so it survives
-- pg_restore's empty search_path (the 0049 pitfall).
CREATE OR REPLACE FUNCTION skills_notify_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_notify(
        'kb_change',
        json_build_object('table', TG_TABLE_NAME, 'op', TG_OP, 'id', NEW.name)::text
    );
    RETURN NULL;  -- AFTER trigger: the return value is ignored
END;
$$;

CREATE TRIGGER skills_notify_change
    AFTER INSERT OR UPDATE ON skills
    FOR EACH ROW EXECUTE FUNCTION skills_notify_change();
