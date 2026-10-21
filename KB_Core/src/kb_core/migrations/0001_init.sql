CREATE EXTENSION IF NOT EXISTS ltree;
-- pgvector is deferred to Phase 3 (added in a later migration with the embedding column).

CREATE TABLE schema_migrations (
    version    text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE nodes (
    id                uuid PRIMARY KEY,
    source            text NOT NULL,
    external_id       text NOT NULL,
    type              text NOT NULL,
    parent_id         uuid REFERENCES nodes(id),
    slug              text NOT NULL,
    path              ltree NOT NULL,
    title             text,
    content           text NOT NULL,
    summary           text,
    uri               text,
    tags              text[] NOT NULL DEFAULT '{}',
    source_created_at timestamptz,
    source_updated_at timestamptz,
    ingested_at       timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    content_hash      text NOT NULL,
    content_tsv       tsvector GENERATED ALWAYS AS (
        to_tsvector('english', coalesce(title, '') || ' ' || content)
    ) STORED,
    -- UNIQUE(parent_id, slug): unique filename within a directory. Root nodes have
    -- parent_id = NULL (treated as distinct), so root uniqueness is enforced by
    -- UNIQUE(path) instead, since a root node's path equals its slug.
    UNIQUE (parent_id, slug),
    UNIQUE (path)
);

CREATE INDEX nodes_path_gist_idx ON nodes USING gist (path);
CREATE INDEX nodes_content_tsv_idx ON nodes USING gin (content_tsv);
CREATE INDEX nodes_tags_idx ON nodes USING gin (tags);
CREATE INDEX nodes_source_idx ON nodes (source);
CREATE INDEX nodes_type_idx ON nodes (type);
CREATE INDEX nodes_parent_idx ON nodes (parent_id);
