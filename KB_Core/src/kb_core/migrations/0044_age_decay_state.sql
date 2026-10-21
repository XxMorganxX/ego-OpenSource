-- Content-age decay master switch. Single row: the
-- dashboard flips `enabled`, the three analysis reads consult it per call. `id`
-- exists because the shared kb_notify_change() trigger builds its payload from
-- rec.id — a table without it raises on every write (see 0040_mcp_tool_state.sql).
CREATE TABLE age_decay_state (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    enabled boolean NOT NULL DEFAULT true,
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by text
);

INSERT INTO age_decay_state (enabled) VALUES (true);

CREATE TRIGGER age_decay_state_notify_change
    AFTER INSERT OR UPDATE ON age_decay_state
    FOR EACH ROW EXECUTE FUNCTION kb_notify_change();
