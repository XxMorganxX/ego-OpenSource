-- One rule per pair, regardless of kind: a pair is either must_link, cannot_link, or
-- unconstrained — never both. This closes the race where two concurrent inserts with
-- opposite kinds could each pass a SELECT-based contradiction check and both land.
CREATE UNIQUE INDEX concept_constraints_pair
    ON concept_constraints (left_id, right_id);
