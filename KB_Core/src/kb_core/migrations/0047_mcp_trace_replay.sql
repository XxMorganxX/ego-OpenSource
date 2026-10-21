-- Per-stage retrieval replay for the dashboard's Live traces replay view: the arm
-- lists, the fused order, and (search_graph) the crawl's entry seeds, per-hop offered
-- candidates and the four fusion lists -- everything the search already held in
-- memory, recorded so the replay shows what ACTUALLY happened on that call rather
-- than a re-run against today's corpus. Versioned jsonb ({"v": 1, ...}); NULL on
-- rows traced before capture, which the read path serves as a partial replay.
ALTER TABLE mcp_trace ADD COLUMN replay jsonb;
