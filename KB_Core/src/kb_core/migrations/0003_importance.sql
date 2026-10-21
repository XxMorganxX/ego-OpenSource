ALTER TABLE nodes
    ADD COLUMN importance           real        NOT NULL DEFAULT 1.0,
    ADD COLUMN importance_modifiers jsonb       NOT NULL DEFAULT '{}',
    ADD COLUMN access_count         integer     NOT NULL DEFAULT 0,
    ADD COLUMN last_accessed_at     timestamptz;
