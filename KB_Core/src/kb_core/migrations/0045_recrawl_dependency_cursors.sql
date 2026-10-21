-- Per-node recrawl dependency cursors (dependency-gated recrawl).
--
-- Recrawl used to be gated on ONE corpus-wide watermark -- max(at) over ingest_log's
-- node_created/node_updated -- which is uncorrelated to the node being tested, so a
-- single new node anywhere re-queued every note. Worse, nearly all of the
-- watermark-advancing events were entity nodes minted by promotion,
-- which recrawl's own extract_entities pass feeds: the crawl manufactured the events
-- that made it due, and never idled.
--
-- These two columns replace it. Each recrawl pass depends on exactly one vocabulary,
-- and a node is due for that pass only when its stored hash differs from the current
-- one. NULL means "never analyzed under a known vocabulary" -> due.
--
-- Deliberately NOT backfilled here: the hash is defined in Python (knowledge_base.py
-- _vocab_digest) and duplicating that serialization in SQL would give two sources of
-- truth that can silently drift. The re-analysis daemon adopts the current vocabulary
-- for already-analyzed nodes once at boot instead (adopt_vocabulary_cursors).
ALTER TABLE nodes ADD COLUMN analyzed_class_hash text;
ALTER TABLE nodes ADD COLUMN analyzed_kind_hash  text;
