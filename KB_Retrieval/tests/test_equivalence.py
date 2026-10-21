"""THE GATE.

HybridStrategy must return what KnowledgeBase.search() returns. Until this passes,
the hybrid column of the 2x2 means nothing -- and it gates cluster-crawl too,
because both strategies fuse with the same rrf().

Method precedent: migration 0037 proved its rewrite by comparing top-20 SETS across
every golden query. This file does the synthetic-corpus half in CI; the live
golden half is scripts/prove_equivalence.py.
"""

import random

from kb_core.domain import NodePatch, NodeRecord

from kb_retrieval.contract import Query
from kb_retrieval.registry import get_strategy

WORDS = ["postgres", "vector", "retrieval", "budget", "meeting", "agent",
         "schema", "index", "latency", "concept", "entity", "eval"]


def _seeded_corpus(kb, n=60, seed=7):
    rnd = random.Random(seed)
    for i in range(n):
        body = " ".join(rnd.choice(WORDS) for _ in range(rnd.randint(8, 40)))
        # NodeRecord has no doc_kind field (domain.py) -- it lives on Node/NodePatch,
        # so it is set post-write via update(), matching test_hybrid.py and
        # KB_Core's tests/test_search_precision.py.
        node = kb.upsert(NodeRecord(
            source="agent", external_id=f"n{i}", type="note", slug=f"n{i}",
            content=body, title=" ".join(rnd.sample(WORDS, 3)),
            parent_path=rnd.choice(["", "proj", "proj.sub", "ops"])))
        kind = rnd.choice([None, "meeting notes", "runbook"])
        if kind is not None:
            kb.update(node.id, NodePatch(doc_kind=kind))
        if rnd.random() < 0.8:
            v = [0.0] * 1024
            for _ in range(6):
                v[rnd.randrange(1024)] = rnd.random()
            kb.set_embedding(node.id, v, embedded_hash=f"h{i}")


def _vec(seed):
    rnd = random.Random(seed)
    v = [0.0] * 1024
    for _ in range(6):
        v[rnd.randrange(1024)] = rnd.random()
    return v


def _snapshot_access(pool):
    """Read access_count/last_accessed_at for every node, matching the observer-
    effect guard in evals/retrieval/run.py's run() (access-count snapshot/restore) -- reads bump those columns,
    which feed the importance/retrieval-recency multiplier that ranking consumes.
    Indexed by id so _restore_access can look up just the rows it needs."""
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT id, access_count, last_accessed_at FROM nodes").fetchall()
    return {r["id"]: (r["access_count"], r["last_accessed_at"]) for r in rows}


def _restore_access(pool, snapshot, ids):
    """Restore ONLY `ids` to their snapshotted values. record_access touches at
    most `limit` rows per call, so restoring every row in the table on every
    _compare (as this helper used to, with an unscoped executemany over the whole
    snapshot) is write amplification -- an UPDATE for every row in the corpus when
    only ~20 changed. The same fix lives in scripts/prove_equivalence.py, which
    hits this for real against your own corpus."""
    rows = [(snapshot[i][0], snapshot[i][1], i) for i in ids if i in snapshot]
    if not rows:
        return
    with pool.connection() as conn:
        conn.cursor().executemany(
            "UPDATE nodes SET access_count = %s, last_accessed_at = %s WHERE id = %s",
            rows)


def _compare(kb, pool, **kwargs):
    """Runs legacy search() and HybridStrategy.search() back to back. Both bump
    access_count/last_accessed_at on the rows they return (record_access), and
    that write measurably perturbs the NEXT HNSW scan -- so without restoring
    between the two calls, the second implementation compared here would be
    reading state the first one's own read had already mutated. Snapshot once
    before the pair, restore between them and again after, so neither call -- nor
    the next _compare in a test's loop -- observes another call's writes.
    """
    limit = kwargs.pop("limit", 20)
    snapshot = _snapshot_access(pool)
    legacy = kb.search(limit=limit, **kwargs)
    legacy_ids = [r.node.id for r in legacy]
    _restore_access(pool, snapshot, legacy_ids)
    strategy = get_strategy("hybrid", kb=kb).search(
        Query(text=kwargs["query"], limit=limit,
              **{k: v for k, v in kwargs.items() if k != "query"}))
    got_ids = [r.id for r in strategy.rows]
    _restore_access(pool, snapshot, got_ids)
    return (legacy_ids, got_ids)


def test_fts_only_matches(kb, pool):
    _seeded_corpus(kb)
    for q in ("postgres", "budget meeting", "vector index latency"):
        legacy, got = _compare(kb, pool, query=q)
        assert set(got) == set(legacy), q


def test_hybrid_matches(kb, pool):
    _seeded_corpus(kb)
    for s in range(5):
        legacy, got = _compare(kb, pool, query="postgres vector", embedding=_vec(s))
        assert set(got) == set(legacy), s


def test_filtered_hybrid_matches(kb, pool):
    _seeded_corpus(kb)
    for kwargs in ({"kind": "runbook"}, {"under": "proj"},
                   {"include_dead": False}, {"type": "note"}):
        legacy, got = _compare(kb, pool, query="postgres", embedding=_vec(1), **kwargs)
        assert set(got) == set(legacy), kwargs


def test_or_fallback_path_matches(kb, pool):
    _seeded_corpus(kb)
    # A bag whose terms never co-occur verbatim -> the OR-fallback fires.
    legacy, got = _compare(kb, pool, query="postgres eval latency concept")
    assert set(got) == set(legacy)


# 12 words, every one absent from WORDS except three: AND matches nothing.
LONG_NL = ("what caused the postgres latency problem and how was the index fixed "
           "last week")


def test_long_keyword_only_or_fallback_path_matches(kb, pool):
    """Keyword-only (no embedding): a long question's AND-miss falls back to OR on both
    paths, and they agree on the ordering."""
    _seeded_corpus(kb)
    legacy, got = _compare(kb, pool, query=LONG_NL)
    assert legacy and got == legacy


def test_long_hybrid_query_keeps_the_short_bag_gate_on_both_paths(kb, pool):
    """With an embedding the gate is unchanged: a >8-word AND-miss is NOT rewritten, so
    the FTS arm is empty and only the vector arm contributes -- on both paths."""
    _seeded_corpus(kb)
    legacy, got = _compare(kb, pool, query=LONG_NL, embedding=_vec(2))
    assert got == legacy
    pool_q = get_strategy("hybrid", kb=kb)
    with kb.read_snapshot() as conn:
        _, _, fts, _, fts_q = pool_q.flat_pool(
            Query(text=LONG_NL, embedding=_vec(2), limit=20), conn)
    assert fts_q == LONG_NL and fts == []


def test_ordering_is_identical_not_merely_the_same_set(kb, pool):
    _seeded_corpus(kb)
    legacy, got = _compare(kb, pool, query="postgres vector", embedding=_vec(3))
    assert got == legacy


def test_hybrid_matches_when_the_label_vector_differs(kb, pool):
    """The flat pool reads only the retrieval vector: a different label vector on the
    Query must not change what search() and HybridStrategy agree on."""
    _seeded_corpus(kb)
    snapshot = _snapshot_access(pool)
    legacy = [r.node.id for r in kb.search("postgres vector", embedding=_vec(3), limit=20)]
    _restore_access(pool, snapshot, legacy)
    got = [r.id for r in get_strategy("hybrid", kb=kb).search(
        Query(text="postgres vector", embedding=_vec(3), label_embedding=_vec(11),
              limit=20)).rows]
    _restore_access(pool, snapshot, got)
    assert legacy and legacy == got
