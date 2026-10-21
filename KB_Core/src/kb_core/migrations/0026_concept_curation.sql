-- Human curation overlay for the DERIVED concept layer. Direct edits to concepts would
-- be bulldozed by the next consolidation run, so every override is a durable RULE the
-- pipeline consults each run (mirrors entity_instance_blocks / entity_merge_blocks):
--   * concept_constraints — entity-anchored must_link / cannot_link pairs. `note` keeps
--     the human's own sentence: provenance ("why does this rule exist?") and context fed
--     to the labeler for affected concepts.
--   * concept_member_overrides — pin/remove one node from one concept, re-applied at the
--     end of every apply pass.
--   * concepts.label_locked — a human took over name/kind/description; the labeler is
--     skipped and apply never overwrites them.
--   * concepts.status gains 'dismissed' — matched communities are skipped at persist
--     time (the "never show this cluster again" tombstone; delete = un-dismiss).

CREATE TABLE concept_constraints (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind       text NOT NULL CHECK (kind IN ('must_link', 'cannot_link')),
    left_id    uuid NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    right_id   uuid NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    note       text,
    origin     text NOT NULL DEFAULT 'human' CHECK (origin IN ('human')),
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (left_id <> right_id),
    UNIQUE (kind, left_id, right_id)
);

CREATE TABLE concept_member_overrides (
    concept_id uuid NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    node_id    uuid NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    action     text NOT NULL CHECK (action IN ('pin', 'remove')),
    note       text,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (concept_id, node_id)
);

ALTER TABLE concepts ADD COLUMN label_locked boolean NOT NULL DEFAULT false;

ALTER TABLE concepts DROP CONSTRAINT IF EXISTS concepts_status_check;
ALTER TABLE concepts ADD CONSTRAINT concepts_status_check
    CHECK (status IN ('active', 'retired', 'dismissed'));
