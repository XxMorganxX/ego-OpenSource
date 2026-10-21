-- Event time: when a memory's content HAPPENED (meeting date, todo completion, authoring
-- date), as opposed to when its file was created or synced. Source/file timestamps cannot
-- serve this: bulk file operations (a repo reorganisation, a re-checkout, a sync tool rewrite)
-- can flatten a whole vault's ctime/mtime into the ingest window (see the hash-gating note in
-- _persist_node). Written by adapters from source metadata only — frontmatter
-- date:/completed_at:/created:, or a date embedded in the filename — never from file
-- timestamps, and recomputed on every upsert (deterministic from source content, so
-- unlike source_* it is not hash-gated). NULL = the source carries no explicit event
-- signal; read paths fall back with COALESCE(event_at, source_created_at).
ALTER TABLE nodes ADD COLUMN event_at timestamptz;
CREATE INDEX nodes_event_at_idx ON nodes (event_at) WHERE event_at IS NOT NULL;
