"""Read primitives that KB_Retrieval fuses over.

KB_Core stays the sole Postgres owner: KB_Retrieval holds policy and no SQL. Every
candidate primitive returns an explicit server-side ordinal so Python never
re-derives a Postgres ordering -- that is what makes the mode-A equivalence proof a
statement about fusion rather than about float ties or ltree collation.
"""

from kb_core.domain import NodePatch, NodeRecord


def _note(kb, ext, content, **kw):
    return kb.upsert(NodeRecord(source="agent", external_id=ext, type="note",
                                content=content, slug=ext, **kw))


def test_order_by_path_uses_postgres_collation_not_python(kb):
    # Discriminating fact (verified live against the test DB):
    #   'Zed.aaa'::ltree < 'alpha.bbb'::ltree  -> TRUE   (ltree's native comparator
    #       is a plain per-label byte/memcmp comparison: 'Z' = 0x5A < 'a' = 0x61)
    #   'Zed.aaa'::text  < 'alpha.bbb'::text   -> FALSE  (Postgres's default,
    #       locale-aware TEXT collation orders 'alpha.bbb' before 'Zed.aaa')
    # so this pair sorts in OPPOSITE directions under ltree-native ordering vs.
    # Postgres text collation. A prior version of this test used "Zed"/"Alpha"
    # (both capitalized): ltree-native, text collation, AND a plain Python str
    # sort all agree there ("Alpha" < "Zed"), so that fixture stayed green even
    # if order_by_path's `ORDER BY path` were changed to `ORDER BY path::text`
    # (or reimplemented client-side) -- exactly the regression this test exists
    # to catch. Do not "simplify" this back to a same-case (or otherwise
    # order-equivalent) pair.
    a = _note(kb, "aaa", "x", parent_path="Zed")
    b = _note(kb, "bbb", "y", parent_path="alpha")
    got = kb.order_by_path([a.id, b.id])
    assert got == [a.id, b.id]


def test_order_by_path_on_empty_input_is_empty(kb):
    assert kb.order_by_path([]) == []


def test_order_by_path_drops_unknown_ids(kb):
    from uuid import uuid4
    a = _note(kb, "aaa", "x")
    assert kb.order_by_path([a.id, uuid4()]) == [a.id]


def test_order_by_path_honours_a_supplied_conn(kb):
    """order_by_path was the only candidate primitive without a conn= parameter --
    it always opened a fresh pooled connection, even when called inside a pinned
    read_snapshot() (HybridStrategy.search() does exactly this). No existing test
    covered the conn= branch of ANY primitive; this is the first."""
    a = _note(kb, "aaa", "x", parent_path="Zed")
    b = _note(kb, "bbb", "y", parent_path="alpha")
    with kb.read_snapshot() as conn:
        got = kb.order_by_path([a.id, b.id], conn=conn)
    assert got == [a.id, b.id]


def test_read_snapshot_sees_one_consistent_view(kb, pool):
    _note(kb, "one", "first")
    with kb.read_snapshot() as conn:
        first = conn.execute("SELECT count(*) AS n FROM nodes").fetchone()["n"]
        # A concurrent committed insert on ANOTHER connection must not be visible
        # inside this snapshot, or two primitives in one query could disagree.
        with pool.connection() as other:
            other.execute(
                "INSERT INTO nodes (id, source, external_id, type, slug, path, content, "
                "content_hash) "
                "VALUES (gen_random_uuid(), 'agent', 'two', 'note', 'two', "
                "'two'::ltree, 'second', 'deadbeef')")
        second = conn.execute("SELECT count(*) AS n FROM nodes").fetchone()["n"]
    assert first == second


def _filters(**kw):
    base = dict(under=None, source=None, type=None, tags=None, kind=None,
                since=None, until=None, include_dead=True)
    base.update(kw)
    return base


def test_fts_candidates_returns_dense_ordinals_from_one(kb):
    _note(kb, "a", "postgres vector search")
    _note(kb, "b", "postgres tuning guide")
    rows = kb.fts_candidates("postgres", pool=50, **_filters())
    assert [r["ordinal"] for r in rows] == [1, 2]
    assert all(r["score"] > 0 for r in rows)


def test_fts_candidates_excludes_scaffolding_unless_type_named(kb):
    _note(kb, "note", "widget report")
    kb.upsert(NodeRecord(source="agent", external_id="fold", type="folder",
                         content="widget", slug="widgetdir"))
    ids = {r["id"] for r in kb.fts_candidates("widget", pool=50, **_filters())}
    assert len(ids) == 1
    typed = kb.fts_candidates("widget", pool=50, **_filters(type="folder"))
    assert len(typed) == 1


def test_fts_candidates_honours_the_shared_filter_block(kb):
    # NodeRecord has no doc_kind field (it's set post-write via update()/NodePatch,
    # same as tests/test_search_precision.py does), so doc_kind is applied here
    # rather than threaded through _note's **kw.
    keep = _note(kb, "keep", "budget review")
    kb.update(keep.id, NodePatch(doc_kind="meeting notes"))
    drop = _note(kb, "drop", "budget review")
    kb.update(drop.id, NodePatch(doc_kind="design spec"))
    rows = kb.fts_candidates("budget", pool=50, **_filters(kind="meeting notes"))
    assert len(rows) == 1


def test_fts_candidates_respects_the_pool_cap(kb):
    for i in range(5):
        _note(kb, f"n{i}", "shared term here")
    assert len(kb.fts_candidates("shared", pool=3, **_filters())) == 3


def test_fts_matches_any_is_true_only_for_an_and_match(kb):
    _note(kb, "a", "alpha beta")
    assert kb.fts_matches_any("alpha beta") is True
    assert kb.fts_matches_any("alpha gamma") is False


def test_record_access_bumps_count_and_sets_last_accessed(kb):
    a = _note(kb, "aaa", "x")
    b = _note(kb, "bbb", "y")
    before = kb.get(a.id)
    assert before.access_count == 0
    assert before.last_accessed_at is None

    kb.record_access([a.id])

    after = kb.get(a.id)
    assert after.access_count == 1
    assert after.last_accessed_at is not None
    # A node not named in the call is untouched.
    other = kb.get(b.id)
    assert other.access_count == 0
    assert other.last_accessed_at is None

    kb.record_access([a.id])

    twice = kb.get(a.id)
    assert twice.access_count == 2
    assert twice.last_accessed_at >= after.last_accessed_at


def test_record_access_on_empty_input_is_a_noop(kb):
    a = _note(kb, "aaa", "x")
    kb.record_access([])
    unchanged = kb.get(a.id)
    assert unchanged.access_count == 0
    assert unchanged.last_accessed_at is None


def _vec(hot):
    v = [0.0] * 1024
    v[hot] = 1.0
    return v


def test_knn_candidates_orders_by_cosine_with_dense_ordinals(kb):
    a = _note(kb, "a", "alpha")
    b = _note(kb, "b", "beta")
    kb.set_embedding(a.id, _vec(0), embedded_hash="h1")
    kb.set_embedding(b.id, _vec(1), embedded_hash="h2")
    rows = kb.knn_candidates(_vec(0), pool=50, **_filters())
    assert rows[0]["id"] == a.id
    assert [r["ordinal"] for r in rows] == [1, 2]
    assert rows[0]["similarity"] > rows[1]["similarity"]


def test_knn_candidates_has_no_similarity_floor(kb):
    """A per-row floor was built and measured harmful to recall. Absence of
    a floor is a deliberate, load-bearing property -- see negatives.py."""
    a = _note(kb, "a", "alpha")
    kb.set_embedding(a.id, _vec(500), embedded_hash="h")
    assert len(kb.knn_candidates(_vec(0), pool=50, **_filters())) == 1


def test_knn_candidates_honours_the_shared_filter_block(kb):
    # NodeRecord has no doc_kind field — it is set post-write via update()/NodePatch,
    # the same way tests/test_search_precision.py does it.
    a = _note(kb, "keep", "x")
    b = _note(kb, "drop", "x")
    kb.update(a.id, NodePatch(doc_kind="meeting notes"))
    kb.update(b.id, NodePatch(doc_kind="design spec"))
    kb.set_embedding(a.id, _vec(0), embedded_hash="h1")
    kb.set_embedding(b.id, _vec(0), embedded_hash="h2")
    rows = kb.knn_candidates(_vec(0), pool=50, **_filters(kind="meeting notes"))
    assert [r["id"] for r in rows] == [a.id]


def test_knn_candidates_is_deterministic_across_ties(kb):
    """Equal (similarity, importance) previously permuted freely and fed unstable
    ranks into RRF -- the cause of non-reproducible eval reruns before 0037."""
    # Proves the OUTPUT is stable across repeated calls, not that the `id` tiebreak
    # is WHY -- a test that forces a tied tuple to relocate to prove that causal
    # claim is out of scope here.
    for i in range(4):
        n = _note(kb, f"n{i}", "same")
        kb.set_embedding(n.id, _vec(0), embedded_hash=f"h{i}")
    first = [r["id"] for r in kb.knn_candidates(_vec(0), pool=50, **_filters())]
    for _ in range(3):
        assert [r["id"] for r in kb.knn_candidates(_vec(0), pool=50, **_filters())] == first


def test_knn_candidates_scores_a_note_by_its_best_chunk(kb):
    n = _note(kb, "long", "unrelated head text")
    kb.set_embedding(n.id, _vec(700), embedded_hash="h")
    kb.set_chunk_embeddings(n.id, [(0, 10, _vec(0))])
    rows = kb.knn_candidates(_vec(0), pool=50, **_filters())
    assert rows[0]["similarity"] > 0.9


def test_hydrate_returns_nodes_and_snippets_keyed_by_id(kb):
    a = _note(kb, "a", "the quick brown fox jumps over the lazy dog")
    got = kb.hydrate([a.id], "brown fox")
    node, snippet = got[a.id]
    assert node.id == a.id
    assert "brown" in snippet.lower()


def test_hydrate_on_empty_input_is_empty(kb):
    assert kb.hydrate([], "anything") == {}


def test_hydrate_omits_unknown_ids_rather_than_raising(kb):
    from uuid import uuid4
    a = _note(kb, "a", "text here")
    got = kb.hydrate([a.id, uuid4()], "text")
    assert set(got) == {a.id}


# ---- conn= branch coverage -------------------------------------------------
# Before this block, order_by_path was the ONLY primitive whose conn= branch any
# test touched. ClusterCrawlStrategy calls every one of these inside a single
# read_snapshot(), so an unpinned read is a silent correctness hole, not a style
# nit. Two of these tests use the STRONG shape -- commit a row on another
# connection mid-snapshot and assert the pinned call cannot see it. A weak shape
# (just calling with conn= and checking the result) would pass even if the method
# ignored conn entirely and opened its own connection, which is precisely the bug.


def _commit_other_note(pool, ext, content):
    """Commit a node on a DIFFERENT connection, so a pinned REPEATABLE READ
    snapshot opened earlier must not see it.

    `path` gets its own named parameter (`%(p)s`), bound to the same value as
    `%(e)s`, rather than reusing `%(e)s` a third time with a `::ltree` cast --
    psycopg maps repeated named parameters to ONE server-side placeholder, and
    Postgres then sees that placeholder used as plain text (external_id, slug)
    AND cast to ltree (path) in the same statement, which raises
    `AmbiguousParameter: inconsistent types deduced for parameter $1` (verified
    live against the test DB). Separate names -> separate placeholders -> each
    independently typed from its own column context.
    """
    with pool.connection() as other:
        other.execute(
            "INSERT INTO nodes (id, source, external_id, type, slug, path, content, "
            "content_hash) VALUES (gen_random_uuid(), 'agent', %(e)s, 'note', %(e)s, "
            "%(p)s::ltree, %(c)s, %(h)s)",
            {"e": ext, "p": ext, "c": content, "h": ext * 8})


def test_fts_candidates_with_conn_reads_only_the_pinned_snapshot(kb, pool):
    _note(kb, "first", "postgres tuning")
    with kb.read_snapshot() as conn:
        # Establish the snapshot with a REAL read before the concurrent write.
        # REPEATABLE READ takes its snapshot at the first non-transaction-control
        # statement, so `SET TRANSACTION ISOLATION LEVEL` alone pins nothing yet --
        # without this line the snapshot would open AFTER the insert and see it.
        # (test_read_snapshot_sees_one_consistent_view above follows the same order
        # for the same reason.)
        assert len(kb.fts_candidates("postgres", pool=50, conn=conn, **_filters())) == 1
        _commit_other_note(pool, "second", "postgres tuning")
        pinned = kb.fts_candidates("postgres", pool=50, conn=conn, **_filters())
        unpinned = kb.fts_candidates("postgres", pool=50, **_filters())
    assert len(pinned) == 1     # the concurrent insert is invisible inside the snapshot
    assert len(unpinned) == 2   # ...and visible outside it -- proving conn= is load-bearing


def test_knn_candidates_honours_a_supplied_conn(kb):
    a = _note(kb, "a", "alpha")
    kb.set_embedding(a.id, _vec(0), embedded_hash="h1")
    with kb.read_snapshot() as conn:
        rows = kb.knn_candidates(_vec(0), pool=50, conn=conn, **_filters())
    assert [r["id"] for r in rows] == [a.id]


def test_hydrate_honours_a_supplied_conn(kb):
    a = _note(kb, "a", "the quick brown fox")
    with kb.read_snapshot() as conn:
        got = kb.hydrate([a.id], "brown", conn=conn)
    assert set(got) == {a.id}


def test_fts_matches_any_honours_a_supplied_conn(kb):
    _note(kb, "a", "alpha beta")
    with kb.read_snapshot() as conn:
        assert kb.fts_matches_any("alpha beta", conn=conn) is True
        assert kb.fts_matches_any("alpha gamma", conn=conn) is False


def test_find_entities_with_conn_reads_only_the_pinned_snapshot(kb, pool):
    """find_entities is the crawl's name-anchor rung and runs INSIDE the crawl's
    read_snapshot(). Without conn= it opened its own connection, so the entity it
    resolved could come from a different DB state than the concept/bridge frontier
    calls in the same search()."""
    kb.upsert_entity("Widget", categories=["tool"])
    with kb.read_snapshot() as conn:
        # Same ordering requirement as the fts test above: a real read first, or the
        # snapshot opens after the write and the assertion below is vacuous.
        assert {r["name"] for r in kb.find_entities("Widget", conn=conn)} == {"Widget"}
        after = kb.upsert_entity("Widgetron", categories=["tool"])
        pinned = kb.find_entities("Widget", conn=conn)
        unpinned = kb.find_entities("Widget")
    # "Widgetron" matches the name rung's ILIKE '%Widget%', so it WOULD appear if
    # this call were reading current state instead of the pinned snapshot.
    # find_entities returns `id` as a str (_entity_profile_dict), hence str(...).
    assert str(after.node.id) not in {r["id"] for r in pinned}
    assert str(after.node.id) in {r["id"] for r in unpinned}
    assert {r["name"] for r in pinned} == {"Widget"}


def test_entity_profile_honours_a_supplied_conn(kb):
    ent = kb.upsert_entity("Widget", categories=["tool"])
    with kb.read_snapshot() as conn:
        got = kb.entity_profile(ent.node.id, conn=conn)
    assert got["name"] == "Widget"
    assert got["mentions"] == 0
