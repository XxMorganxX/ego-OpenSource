CREATE TABLE sync_state (
    source         text PRIMARY KEY,
    last_synced_at timestamptz NOT NULL DEFAULT now(),
    cursor         text
);
