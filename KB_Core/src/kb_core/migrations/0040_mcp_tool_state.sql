-- Server-side enable/disable for every tool KB_MCP registers, plus the inventory of
-- what those tools ARE. KB_MCP upserts one row per registered tool on startup
-- (KnowledgeBase.publish_tool_inventory) and reads the enabled flags through a TTL
-- cache on every call; the dashboard's /api/mcp/tools endpoints read and flip them.
--
-- The table carries the inventory rather than KB_Core hardcoding a tool list, because
-- the tools live in a different sub-repo and a hardcoded copy would drift the first
-- time one is added. `enabled` defaults true: a newly registered tool is live until
-- deliberately switched off.
--
-- id/UNIQUE(tool) rather than `tool` as PK: the shared kb_notify_change() trigger
-- (0008) builds its payload from rec.id, so a table without an id column raises on
-- every write.
CREATE TABLE mcp_tool_state (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tool       text NOT NULL UNIQUE,
    kind       text NOT NULL CHECK (kind IN ('read', 'write')),
    enabled    boolean NOT NULL DEFAULT true,
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by text
);

-- Live feed: a toggle from one browser tab (or a direct SQL change) refreshes the
-- panel in every other. UPDATE is included because toggling is an update, unlike the
-- append-only tables in 0008/0038.
CREATE TRIGGER mcp_tool_state_notify_change
    AFTER INSERT OR UPDATE ON mcp_tool_state
    FOR EACH ROW EXECUTE FUNCTION kb_notify_change();
