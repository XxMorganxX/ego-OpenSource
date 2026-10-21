"""Schema tests for the concept-curation overlay (migration 0026)."""
import pytest
from kb_core.domain import NodeRecord


def _mem(kb, eid):
    return kb.upsert(NodeRecord(source="agent", external_id=eid, type="note",
                                content="c", slug=eid, title=eid))


def test_curation_tables_exist_and_concepts_gained_columns(kb, pool):
    with pool.connection() as conn:
        for t in ("concept_constraints", "concept_member_overrides"):
            assert conn.execute("SELECT to_regclass(%s) AS r",
                                (f"public.{t}",)).fetchone()["r"] is not None
        c = conn.execute("INSERT INTO concepts (name) VALUES ('x') "
                         "RETURNING label_locked, status").fetchone()
        assert c["label_locked"] is False and c["status"] == "active"
        conn.execute("UPDATE concepts SET status = 'dismissed' WHERE name = 'x'")


def test_constraint_rows_cascade_with_nodes(kb, pool):
    m = _mem(kb, "m1")
    n = _mem(kb, "m2")
    with pool.connection() as conn:
        conn.execute("INSERT INTO concept_constraints (kind, left_id, right_id, note) "
                     "VALUES ('cannot_link', %s, %s, 'they differ')",
                     (min(str(m.id), str(n.id)), max(str(m.id), str(n.id))))
        conn.execute("DELETE FROM nodes WHERE id = %s", (m.id,))
        left = conn.execute("SELECT count(*) AS n FROM concept_constraints").fetchone()["n"]
    assert left == 0


def test_duplicate_constraint_pair_rejected_by_unique(kb, pool):
    m, n = _mem(kb, "m1"), _mem(kb, "m2")
    lo, hi = min(str(m.id), str(n.id)), max(str(m.id), str(n.id))
    with pool.connection() as conn:
        conn.execute("INSERT INTO concept_constraints (kind, left_id, right_id) "
                     "VALUES ('must_link', %s, %s)", (lo, hi))
        with pytest.raises(Exception):
            conn.execute("INSERT INTO concept_constraints (kind, left_id, right_id) "
                         "VALUES ('must_link', %s, %s)", (lo, hi))


def test_constraint_pair_unique_index_rejects_opposite_kind(kb, pool):
    """migration 0027: concept_constraints_pair is a pair-level unique index — a pair
    can hold at most one rule regardless of kind, so an opposite-kind row for the
    same pair must also be rejected (not just a same-kind duplicate)."""
    m, n = _mem(kb, "m1"), _mem(kb, "m2")
    lo, hi = min(str(m.id), str(n.id)), max(str(m.id), str(n.id))
    with pool.connection() as conn:
        conn.execute("INSERT INTO concept_constraints (kind, left_id, right_id) "
                     "VALUES ('must_link', %s, %s)", (lo, hi))
        with pytest.raises(Exception):
            conn.execute("INSERT INTO concept_constraints (kind, left_id, right_id) "
                         "VALUES ('cannot_link', %s, %s)", (lo, hi))
