"""A recrawl cursor is stamped only when the pass it gates actually ran.

Stamping a hash for a pass that did not run would record the node as converged against a
vocabulary it never saw, silently swallowing that vocabulary's change.
"""

from kb_core.domain import NodeRecord


def _note(kb, pool, eid, *, analyzed_days_ago=1):
    n = kb.upsert(NodeRecord(source="agent", external_id=eid, type="note",
                             content="c", slug=eid))
    with pool.connection() as conn:
        conn.execute(
            "UPDATE nodes SET enrichment_status='done', "
            f"last_analyzed_at = now() - interval '{analyzed_days_ago} days' WHERE id=%s",
            (n.id,))
    return n


def test_mark_analyzed_preserves_a_cursor_whose_pass_did_not_run(kb, pool):
    """The invariant, stated as a regression guard: a run that only fired doc_kind must
    leave a previously-stamped class cursor exactly as it was.

    Asserting `is None` on a fresh node cannot catch this — the minimal bug (dropping the
    None-guard) writes NULL into a column that was already NULL.
    """
    a = _note(kb, pool, "a")
    kb.mark_analyzed(a.id, class_hash="CLASS", kind_hash="KIND-1")   # both passes ran

    kb.mark_analyzed(a.id, kind_hash="KIND-2")                       # only doc_kind ran

    with pool.connection() as conn:
        row = conn.execute(
            "SELECT analyzed_class_hash, analyzed_kind_hash FROM nodes WHERE id=%s",
            (a.id,)).fetchone()

    assert row["analyzed_class_hash"] == "CLASS"    # preserved, not clobbered to NULL
    assert row["analyzed_kind_hash"] == "KIND-2"    # advanced


def test_mark_analyzed_preserves_the_kind_cursor_when_only_extraction_ran(kb, pool):
    """The mirror of the class-cursor case: a run where only extract_entities fired must
    leave a previously-stamped doc-kind cursor untouched.

    Without this, dropping the kind_hash None-guard passes every other test in this file.
    """
    a = _note(kb, pool, "a")
    kb.mark_analyzed(a.id, class_hash="CLASS-1", kind_hash="KIND")   # both passes ran

    kb.mark_analyzed(a.id, class_hash="CLASS-2")                     # only extraction ran

    with pool.connection() as conn:
        row = conn.execute(
            "SELECT analyzed_class_hash, analyzed_kind_hash FROM nodes WHERE id=%s",
            (a.id,)).fetchone()

    assert row["analyzed_kind_hash"] == "KIND"      # preserved, not clobbered
    assert row["analyzed_class_hash"] == "CLASS-2"  # advanced


def test_mark_analyzed_advances_the_clock_with_no_hashes(kb, pool):
    a = _note(kb, pool, "a", analyzed_days_ago=99)
    before = kb.get(a.id).last_analyzed_at
    kb.mark_analyzed(a.id)                            # the failure path: clock only
    got = kb.get(a.id)
    assert got.last_analyzed_at > before
    assert got.enrichment_status == "done"            # ingest state still untouched
