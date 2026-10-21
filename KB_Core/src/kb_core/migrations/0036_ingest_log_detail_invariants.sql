-- Restore ingest_log.detail invariants (NOT NULL DEFAULT '{}'). A short-lived branch
-- migration (0035, deleted before merge) dropped them on any DB it ran against.
-- Idempotent: repairs a drifted DB, no-op on a correct one.
UPDATE ingest_log SET detail = '{}'::jsonb WHERE detail IS NULL;
ALTER TABLE ingest_log ALTER COLUMN detail SET DEFAULT '{}'::jsonb;
ALTER TABLE ingest_log ALTER COLUMN detail SET NOT NULL;
