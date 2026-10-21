"""The vocabulary hashes that gate recrawl.

Both are taken over the sorted SET, not the support-ranked order the prompt builders
return. entity_classes_for_prompt ranks by distinct members and doc_kinds_for_prompt by
count(*) DESC, so a rank-ordered hash would churn every time a count drifted while the
vocabulary itself sat still -- which is the whole failure this gate exists to avoid.
"""

from kb_core.domain import NodeRecord
from kb_core.db import MIGRATIONS_DIR


def _class(kb, name, description, *, active=True):
    """Create an entity-class entity. Note upsert_entity_class takes `active=`, not a
    status string, and runs the description through clean_class_description."""
    return kb.upsert_entity_class(name, description=description, active=active)


def _note(kb, pool, eid, kind=None):
    n = kb.upsert(NodeRecord(source="agent", external_id=eid, type="note",
                             content="c", slug=eid))
    if kind is not None:
        with pool.connection() as conn:
            conn.execute("UPDATE nodes SET doc_kind=%s WHERE id=%s", (kind, n.id))
    return n


def test_cursor_columns_exist(pool):
    with pool.connection() as conn:
        cols = {r["column_name"] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'nodes'").fetchall()}
    assert "analyzed_class_hash" in cols
    assert "analyzed_kind_hash" in cols
    assert "attempted_class_hash" in cols
    assert "attempted_kind_hash" in cols
    assert "recrawl_cursor_adoption_pending" in cols
    assert "last_gate_analyzed_at" in cols


def test_0046_migrates_the_legacy_population_and_gate_clock(pool):
    migration = (MIGRATIONS_DIR / "0046_recrawl_cursor_lifecycle.sql").read_text()
    with pool.connection() as conn, conn.transaction():
        conn.execute(
            """CREATE TEMP TABLE nodes (
                   id text PRIMARY KEY,
                   last_analyzed_at timestamptz,
                   analyzed_class_hash text,
                   analyzed_kind_hash text
               ) ON COMMIT DROP""")
        conn.execute(
            """INSERT INTO nodes VALUES
                   ('legacy', '2026-09-01T12:00:00Z', NULL, NULL),
                   ('never', NULL, NULL, NULL),
                   ('cursor', '2026-09-02T12:00:00Z', 'class-v1', 'kind-v1')""")

        conn.execute(migration)

        rows = {r["id"]: r for r in conn.execute(
            "SELECT * FROM nodes ORDER BY id").fetchall()}
    assert rows["legacy"]["recrawl_cursor_adoption_pending"] is True
    assert rows["legacy"]["last_gate_analyzed_at"] == rows["legacy"]["last_analyzed_at"]
    assert rows["never"]["recrawl_cursor_adoption_pending"] is False
    assert rows["never"]["last_gate_analyzed_at"] is None
    assert rows["cursor"]["recrawl_cursor_adoption_pending"] is False
    assert rows["cursor"]["last_gate_analyzed_at"] == rows["cursor"]["last_analyzed_at"]


def test_vocab_digest_is_order_independent():
    """The load-bearing property: the digest is over the SET, not the sequence.

    Delete the sorted() in _vocab_digest and this test must fail.
    """
    from kb_core.knowledge_base import _vocab_digest

    rows = [("project", "a body of work"), ("tool", "a thing you use"),
            ("pattern", "a recurring shape")]
    assert _vocab_digest(rows) == _vocab_digest(list(reversed(rows)))
    assert _vocab_digest(rows) == _vocab_digest([rows[1], rows[2], rows[0]])


def test_vocab_digest_separates_fields_and_rows():
    """Field and row boundaries must be distinguishable, or two different
    vocabularies collide onto one hash."""
    from kb_core.knowledge_base import _vocab_digest

    assert _vocab_digest([("a", "b")]) != _vocab_digest([("a", ""), ("b", "")])


def test_class_hash_changes_when_a_class_activates(kb, pool):
    _class(kb, "project", "a body of work")
    before = kb.active_class_hash()
    _class(kb, "pattern", "a recurring shape")
    assert kb.active_class_hash() != before


def test_class_hash_changes_when_a_description_changes(kb, pool):
    _class(kb, "project", "a body of work")
    before = kb.active_class_hash()
    with pool.connection() as conn:
        conn.execute("UPDATE entities SET description='reworded' "
                     "WHERE canonical_name=%s", ("project",))
    assert kb.active_class_hash() != before


def test_class_hash_excludes_inactive_classes(kb, pool):
    _class(kb, "project", "a body of work")
    before = kb.active_class_hash()
    _class(kb, "candidate", "an emerging name", active=False)
    assert kb.active_class_hash() == before


def test_kind_hash_ignores_rank_drift_but_sees_membership(kb, pool):
    for i in range(3):
        _note(kb, pool, f"a{i}", kind="meeting notes")
    for i in range(3):
        _note(kb, pool, f"b{i}", kind="implementation plan")
    before = kb.doc_kind_vocab_hash()

    _note(kb, pool, "a9", kind="meeting notes")          # rank drift only, same set
    assert kb.doc_kind_vocab_hash() == before

    for i in range(3):
        _note(kb, pool, f"c{i}", kind="design spec")     # new member
    assert kb.doc_kind_vocab_hash() != before


def test_hashes_are_sha256_hex(kb):
    for h in (kb.active_class_hash(), kb.doc_kind_vocab_hash()):
        assert len(h) == 64 and all(c in "0123456789abcdef" for c in h)
