-- Personal skill registry: portable Agent Skills packages, immutable revisions,
-- compare-and-swap writes. Usage: docs/setup/recipes/skill-registry.md.
--
-- Dedicated tables, deliberately NOT rows in `nodes`: a node type would have to be carved
-- out of every node-keyed pass (importance recompute and decay, concept clustering, entity
-- extraction, doc-kind analysis, the dashboard tree, retrieval heat, the vault mirror), and
-- each omission would quietly treat instructions as memories. Here they are excluded by
-- construction and still ride the ordinary kb-backup dump.
--
-- `skills.current_revision` carries no FK to skill_revisions on purpose: a create inserts
-- the skill row first (revision 1) and then its revision inside one transaction, and the
-- write path (kb_core.skills.upsert) is the only writer, serialised per name by an advisory
-- transaction lock.

CREATE TABLE skills (
    name             text PRIMARY KEY,
    current_revision integer NOT NULL CHECK (current_revision >= 1),
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    archived_at      timestamptz,           -- NULL = active; archive keeps every revision
    archived_by      text                   -- the verified principal, NULL on an unauthenticated listener
);

CREATE TABLE skill_revisions (
    name           text NOT NULL REFERENCES skills(name) ON DELETE CASCADE,
    revision       integer NOT NULL CHECK (revision >= 1),
    description    text NOT NULL,           -- derived from SKILL.md frontmatter, never free-standing
    actor          text,                    -- verified principal (Cloudflare Access email); NULL = no principal
    agent_label    text,                    -- optional, self-reported, never trusted for access
    change_summary text NOT NULL,
    package_hash   text NOT NULL,           -- sha256 over (path, file hash) pairs in path order
    created_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (name, revision)
);

CREATE TABLE skill_revision_files (
    name         text NOT NULL,
    revision     integer NOT NULL,
    path         text NOT NULL,             -- package-relative, stored exactly as validated
    content      text NOT NULL,             -- UTF-8 text only in this delivery
    content_hash text NOT NULL,
    PRIMARY KEY (name, revision, path),
    FOREIGN KEY (name, revision) REFERENCES skill_revisions(name, revision) ON DELETE CASCADE
);

-- Lexical discovery: trigram similarity on the name (pg_trgm is installed by 0032);
-- description full-text ranking is computed at query time over this small table.
CREATE INDEX skills_name_trgm_idx ON skills USING gin (name gin_trgm_ops);
