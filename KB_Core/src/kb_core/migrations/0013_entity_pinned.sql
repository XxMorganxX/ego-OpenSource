-- A class can be pinned by a human curator. `pinned` protects the class's active status from
-- automatic deactivation by reconcile_classes — manual curation outranks the vote-based sweep,
-- mirroring description_locked (protects the description) and the blocked tombstone (protects a
-- removal). The three flags are orthogonal.
ALTER TABLE entities ADD COLUMN pinned boolean NOT NULL DEFAULT false;
