-- Concept-pair separation rules: "these two CONCEPTS are unrelated". Anchored on
-- concept ids — unlike concept_constraints, whose left/right are node ids — because the
-- human speaks at concept level ("Garden Volunteers is not related to Globex Billing
-- Migration") and node membership churns run to run. Each consolidation run re-resolves
-- a rule against CURRENT membership (concepts.resolve_separations): one node-anchored
-- cannot_link between each side's strongest exclusive memory member, enforced by the
-- existing split machinery. A rule whose side has no members (retired/emptied concept)
-- is inert for that run, never an error.
CREATE TABLE concept_separations (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    left_id    uuid NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    right_id   uuid NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    note       text,
    origin     text NOT NULL DEFAULT 'human' CHECK (origin IN ('human')),
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (left_id <> right_id),
    UNIQUE (left_id, right_id)
);
