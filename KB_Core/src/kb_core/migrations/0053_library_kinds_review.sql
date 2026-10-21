-- Skill and agent library.
--
-- One registry, two kinds of item. A skill is an Agent Skills package; an agent is a
-- Claude Code subagent definition stored as a one-file package (AGENT.md). Names are
-- unique per kind, so every key gains `kind`. The table names stay: renaming them would
-- touch every query, the 0052 trigger and the tests for no behavioural gain.
--
-- Review: every new revision is `pending` until an MCP session that read it in full
-- approves it against its package hash. Only `skills.approved_revision` is served to
-- agents and synced devices (kb_core.skills / kb_core.library_export).

ALTER TABLE skill_revision_files DROP CONSTRAINT skill_revision_files_name_revision_fkey;
ALTER TABLE skill_revisions DROP CONSTRAINT skill_revisions_name_fkey;
ALTER TABLE skill_revision_files DROP CONSTRAINT skill_revision_files_pkey;
ALTER TABLE skill_revisions DROP CONSTRAINT skill_revisions_pkey;
ALTER TABLE skills DROP CONSTRAINT skills_pkey;

ALTER TABLE skills ADD COLUMN kind text NOT NULL DEFAULT 'skill'
    CHECK (kind IN ('skill', 'agent'));
ALTER TABLE skill_revisions ADD COLUMN kind text NOT NULL DEFAULT 'skill';
ALTER TABLE skill_revision_files ADD COLUMN kind text NOT NULL DEFAULT 'skill';

ALTER TABLE skills ADD PRIMARY KEY (kind, name);
ALTER TABLE skill_revisions ADD PRIMARY KEY (kind, name, revision);
ALTER TABLE skill_revision_files ADD PRIMARY KEY (kind, name, revision, path);
-- The FKs carry kind, so a revision row can never disagree with its item's kind; the
-- CHECK on skills.kind therefore covers every table.
ALTER TABLE skill_revisions ADD FOREIGN KEY (kind, name)
    REFERENCES skills (kind, name) ON DELETE CASCADE;
ALTER TABLE skill_revision_files ADD FOREIGN KEY (kind, name, revision)
    REFERENCES skill_revisions (kind, name, revision) ON DELETE CASCADE;

ALTER TABLE skills ADD COLUMN approved_revision integer CHECK (approved_revision >= 1);
ALTER TABLE skill_revisions
    ADD COLUMN review_status text NOT NULL DEFAULT 'pending'
        CHECK (review_status IN ('pending', 'approved', 'rejected', 'superseded')),
    ADD COLUMN reviewed_at    timestamptz,
    ADD COLUMN reviewer       text,     -- verified principal of the reviewing call
    ADD COLUMN reviewer_label text,     -- self-reported, never trusted for access
    ADD COLUMN review_notes   text,
    ADD COLUMN reviewed_hash  text;     -- the package hash the reviewer confirmed

CREATE INDEX skill_revisions_pending_idx ON skill_revisions (kind, name)
    WHERE review_status = 'pending';

-- BACKFILL
-- Every revision that existed before review was introduced. The current one is approved
-- as a legacy import; older ones were never served after being replaced, so they are
-- superseded. The test replays everything after this marker.
UPDATE skill_revisions r
   SET review_status = 'approved', reviewed_at = now(), reviewer_label = 'legacy-import',
       review_notes = 'approved before review existed', reviewed_hash = r.package_hash
  FROM skills s
 WHERE r.kind = s.kind AND r.name = s.name AND r.revision = s.current_revision
   AND r.review_status = 'pending';
UPDATE skill_revisions SET review_status = 'superseded' WHERE review_status = 'pending';
UPDATE skills SET approved_revision = current_revision WHERE approved_revision IS NULL;
