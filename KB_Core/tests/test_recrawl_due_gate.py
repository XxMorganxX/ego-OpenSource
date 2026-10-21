"""Recrawl is due only when an input that can change its answer has moved."""

from kb_core.domain import NodeRecord


def _note(kb, pool, eid, *, analyzed_days_ago=1, legacy=True):
    n = kb.upsert(NodeRecord(source="agent", external_id=eid, type="note",
                             content="c", slug=eid))
    with pool.connection() as conn:
        conn.execute(
            "UPDATE nodes SET enrichment_status='done', "
            f"last_analyzed_at = now() - interval '{analyzed_days_ago} days', "
            f"last_gate_analyzed_at = now() - interval '{analyzed_days_ago} days', "
            "recrawl_cursor_adoption_pending=%s WHERE id=%s",
            (legacy, n.id))
    return n


def _hashes(kb):
    return {"class_hash": kb.active_class_hash(), "kind_hash": kb.doc_kind_vocab_hash()}


def _adopt(kb):
    """Stamp every analyzed node with the current vocabulary — the settled state."""
    return kb.adopt_vocabulary_cursors(**_hashes(kb))


def test_idle_when_no_vocabulary_moved(kb, pool):
    _note(kb, pool, "a")
    _adopt(kb)
    assert kb.due_for_reanalysis(["agent"], limit=10, **_hashes(kb)) == []


def test_unrelated_new_node_does_not_make_the_corpus_due(kb, pool):
    """The regression this whole change exists for: one new node anywhere used to
    re-queue everything, and promotion's own entity nodes kept that watermark hot."""
    _note(kb, pool, "a")
    _adopt(kb)
    kb.upsert(NodeRecord(source="agent", external_id="b", type="note",
                         content="c", slug="b"))
    assert kb.due_for_reanalysis(["agent"], limit=10, **_hashes(kb)) == []


def test_class_activation_makes_nodes_due_for_extraction_only(kb, pool):
    a = _note(kb, pool, "a")
    _adopt(kb)
    kb.upsert_entity_class("pattern", description="a recurring shape", active=True)

    due = kb.due_for_reanalysis(["agent"], limit=10, **_hashes(kb))

    assert [n.id for n, _ in due] == [a.id]
    (_, reasons), = due
    assert reasons.due_class is True
    assert reasons.due_kind is False
    assert reasons.due_gate is False


def test_kind_vocabulary_move_makes_nodes_due_for_doc_kind_only(kb, pool):
    a = _note(kb, pool, "a")
    _adopt(kb)
    for i in range(3):                       # a new doc-kind member enters the vocabulary
        n = kb.upsert(NodeRecord(source="agent", external_id=f"k{i}", type="note",
                                 content="c", slug=f"k{i}"))
        with pool.connection() as conn:
            conn.execute("UPDATE nodes SET doc_kind='design spec' WHERE id=%s", (n.id,))

    due = {n.id: r for n, r in
           kb.due_for_reanalysis(["agent"], limit=10, **_hashes(kb))}

    assert a.id in due
    assert due[a.id].due_kind is True
    assert due[a.id].due_class is False
    assert due[a.id].due_gate is False


def test_gate_clock_fires_independently_of_both_hashes(kb, pool):
    a = _note(kb, pool, "a", analyzed_days_ago=31)
    _adopt(kb)

    due = kb.due_for_reanalysis(["agent"], limit=10, **_hashes(kb))

    assert [n.id for n, _ in due] == [a.id]
    (_, reasons), = due
    assert reasons.due_gate is True
    assert reasons.due_class is False
    assert reasons.due_kind is False


def test_gate_clock_does_not_fire_inside_the_interval(kb, pool):
    _note(kb, pool, "a", analyzed_days_ago=29)
    _adopt(kb)
    assert kb.due_for_reanalysis(["agent"], limit=10, **_hashes(kb)) == []


def test_class_only_completion_does_not_postpone_the_gate(kb, pool):
    a = _note(kb, pool, "a", analyzed_days_ago=31)
    _adopt(kb)

    kb.mark_analyzed(a.id, class_hash=kb.active_class_hash())

    due = dict((n.id, reasons) for n, reasons in
               kb.due_for_reanalysis(["agent"], limit=10, **_hashes(kb)))
    assert due[a.id].due_gate is True
    assert due[a.id].due_class is False


def test_class_only_failure_does_not_postpone_the_gate(kb, pool):
    a = _note(kb, pool, "a", analyzed_days_ago=31)
    _adopt(kb)

    kb.mark_analysis_failed(a.id, class_hash=kb.active_class_hash())

    due = dict((n.id, reasons) for n, reasons in
               kb.due_for_reanalysis(["agent"], limit=10, **_hashes(kb)))
    assert due[a.id].due_gate is True


def test_gate_failure_advances_only_the_gate_retry_clock(kb, pool):
    a = _note(kb, pool, "a", analyzed_days_ago=31)
    _adopt(kb)

    kb.mark_analysis_failed(a.id, gate=True)

    assert kb.due_for_reanalysis(["agent"], limit=10, **_hashes(kb)) == []


def test_never_analyzed_node_is_due(kb, pool):
    n = kb.upsert(NodeRecord(source="agent", external_id="a", type="note",
                             content="c", slug="a"))
    with pool.connection() as conn:
        conn.execute("UPDATE nodes SET enrichment_status='done' WHERE id=%s", (n.id,))
    due = kb.due_for_reanalysis(["agent"], limit=10, **_hashes(kb))
    assert [x.id for x, _ in due] == [n.id]


def test_existing_crawlable_guards_are_preserved(kb, pool):
    unenriched = kb.upsert(NodeRecord(source="agent", external_id="u", type="note",
                                      content="c", slug="u"))
    retired = _note(kb, pool, "r")
    with pool.connection() as conn:
        conn.execute("UPDATE nodes SET dead_at = now() WHERE id=%s", (retired.id,))
    live = _note(kb, pool, "live", analyzed_days_ago=99)

    due_ids = {n.id for n, _ in kb.due_for_reanalysis(["agent"], limit=10, **_hashes(kb))}

    assert live.id in due_ids
    assert unenriched.id not in due_ids      # not first-pass-enriched
    assert retired.id not in due_ids         # retired memories are frozen


def test_adopt_leaves_never_analyzed_nodes_alone(kb, pool):
    fresh = kb.upsert(NodeRecord(source="agent", external_id="f", type="note",
                                 content="c", slug="f"))
    with pool.connection() as conn:
        conn.execute("UPDATE nodes SET enrichment_status='done' WHERE id=%s", (fresh.id,))
    _adopt(kb)
    with pool.connection() as conn:
        row = conn.execute("SELECT analyzed_class_hash FROM nodes WHERE id=%s",
                           (fresh.id,)).fetchone()
    assert row["analyzed_class_hash"] is None


def test_adopt_is_idempotent(kb, pool):
    _note(kb, pool, "a")
    with pool.connection() as conn:
        conn.execute("UPDATE nodes SET recrawl_cursor_adoption_pending=true")
    assert _adopt(kb) == 1
    assert _adopt(kb) == 0


def test_adopt_ignores_nodes_enriched_after_the_migration(kb, pool):
    fresh = _note(kb, pool, "fresh", legacy=False)

    assert _adopt(kb) == 0
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT analyzed_class_hash, analyzed_kind_hash FROM nodes WHERE id=%s",
            (fresh.id,)).fetchone()
    assert row["analyzed_class_hash"] is None
    assert row["analyzed_kind_hash"] is None


def test_adopt_never_overwrites_a_valid_cursor(kb, pool):
    a = _note(kb, pool, "a")
    with pool.connection() as conn:
        conn.execute(
            "UPDATE nodes SET analyzed_class_hash='VALID', "
            "recrawl_cursor_adoption_pending=true WHERE id=%s", (a.id,))

    _adopt(kb)

    with pool.connection() as conn:
        row = conn.execute(
            "SELECT analyzed_class_hash, analyzed_kind_hash FROM nodes WHERE id=%s",
            (a.id,)).fetchone()
    assert row["analyzed_class_hash"] == "VALID"
    assert row["analyzed_kind_hash"] == kb.doc_kind_vocab_hash()


def test_failed_kind_hash_waits_for_the_next_kind_vocabulary_move(kb, pool):
    a = _note(kb, pool, "a")
    kb.mark_analyzed(a.id, gate=True, **_hashes(kb))
    for i in range(3):
        n = kb.upsert(NodeRecord(source="agent", external_id=f"design-{i}", type="note",
                                 content="c", slug=f"design_{i}"))
        with pool.connection() as conn:
            conn.execute("UPDATE nodes SET doc_kind='design spec' WHERE id=%s", (n.id,))
    failed_hash = kb.doc_kind_vocab_hash()
    kb.mark_analysis_failed(a.id, kind_hash=failed_hash)

    assert kb.due_for_reanalysis(["agent"], limit=10, **_hashes(kb)) == []

    for i in range(3):
        n = kb.upsert(NodeRecord(source="agent", external_id=f"research-{i}", type="note",
                                 content="c", slug=f"research_{i}"))
        with pool.connection() as conn:
            conn.execute("UPDATE nodes SET doc_kind='user research' WHERE id=%s", (n.id,))
    due = dict((n.id, reasons) for n, reasons in
               kb.due_for_reanalysis(["agent"], limit=10, **_hashes(kb)))
    assert due[a.id].due_kind is True


def test_reenabling_entity_participation_invalidates_extraction_cursor(kb, pool):
    a = _note(kb, pool, "a")
    hashes = _hashes(kb)
    kb.mark_analyzed(a.id, gate=True, **hashes)
    kb.mark_analysis_failed(a.id, class_hash=hashes["class_hash"])

    kb.set_entity_eligible(a.id, False, reason="noise")
    kb.set_entity_eligible(a.id, True)

    with pool.connection() as conn:
        row = conn.execute(
            "SELECT analyzed_class_hash, attempted_class_hash FROM nodes WHERE id=%s",
            (a.id,)).fetchone()
    assert row["analyzed_class_hash"] is None
    assert row["attempted_class_hash"] is None
    due = dict((n.id, reasons) for n, reasons in
               kb.due_for_reanalysis(["agent"], limit=10, **hashes))
    assert due[a.id].due_class is True


def test_recrawl_path_never_reads_ingest_log():
    import ast
    import inspect
    import textwrap

    from kb_core.analysis import reanalysis
    from kb_core.knowledge_base import KnowledgeBase

    def body_src(fn):
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn))).body[0]
        stmts = tree.body[1:] if ast.get_docstring(tree) else tree.body
        return "\n".join(ast.unparse(s) for s in stmts)

    for fn in (KnowledgeBase.due_for_reanalysis, KnowledgeBase.crawlable_node,
               KnowledgeBase.due_for_backfill, KnowledgeBase.mark_analyzed,
               KnowledgeBase.mark_analysis_failed):
        assert "ingest_log" not in body_src(fn), fn.__name__
    assert "ingest_log" not in inspect.getsource(reanalysis)
