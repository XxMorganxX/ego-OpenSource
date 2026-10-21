"""Mode A, served through the contract.

This strategy must behave identically to KnowledgeBase.search(); the proof is in
test_equivalence.py. These tests cover the contract surface it adds on top.
"""

import pytest

from kb_core.domain import NodePatch, NodeRecord

from kb_retrieval.contract import FILTER_NAMES, Query
from kb_retrieval.registry import get_strategy


def _note(kb, ext, content, **kw):
    return kb.upsert(NodeRecord(source="agent", external_id=ext, type="note",
                                content=content, slug=ext, **kw))


def _vec(hot):
    v = [0.0] * 1024
    v[hot] = 1.0
    return v


def test_hybrid_is_registered_under_its_name(kb):
    s = get_strategy("hybrid", kb=kb)
    assert s.name == "hybrid"


def test_fts_only_when_no_embedding_is_supplied(kb):
    _note(kb, "a", "postgres vector search")
    res = get_strategy("hybrid", kb=kb).search(Query(text="postgres"))
    assert len(res.rows) == 1
    assert res.rows[0].similarity is None
    assert res.confidence["level"] == "keyword"
    assert res.cost.llm_calls == 0


def test_hybrid_reports_similarity_and_a_strong_confidence(kb):
    a = _note(kb, "a", "alpha content")
    kb.set_embedding(a.id, _vec(0), embedded_hash="h")
    res = get_strategy("hybrid", kb=kb).search(
        Query(text="alpha", embedding=_vec(0)))
    assert res.rows[0].similarity > 0.99
    assert res.confidence["level"] == "strong"


def test_every_declared_filter_is_reported_as_applied(kb):
    # doc_kind is not a NodeRecord field; set it post-write via NodePatch.
    kb.update(_note(kb, "a", "budget review").id, NodePatch(doc_kind="meeting notes"))
    res = get_strategy("hybrid", kb=kb).search(
        Query(text="budget", kind="meeting notes"))
    assert res.applied_filters == {"kind": "meeting notes"}
    assert res.ignored_filters == []


def test_hybrid_ignores_nothing_because_it_honours_every_filter(kb):
    s = get_strategy("hybrid", kb=kb)
    assert s.capabilities().filters == FILTER_NAMES


def test_anchor_entity_is_reported_as_ignored_not_silently_dropped(kb, make_entity):
    """HybridStrategy has no crawl and nothing to seed with an entity anchor --
    unlike ClusterCrawlStrategy (RetrievalStrategy's rule: every filter the query
    SETS must appear in applied_filters or ignored_filters), it never applied
    anchor_entity AND hardcoded ignored_filters=[], so the facet vanished with no
    disclosure at all. This is the same defect the cluster-crawl anchor_entity fix
    closed, on the arm that fix didn't touch. server.py forwards anchor_entity to
    both strategies unconditionally, so this is reachable from search_graph(
    strategy='hybrid', anchor_entity=...) directly."""
    ent_id = make_entity("Pgvector")
    s = get_strategy("hybrid", kb=kb)
    res = s.search(Query(text="postgres", anchor_entity=ent_id))
    assert res.ignored_filters == ["anchor_entity@hybrid"]
    assert "anchor_entity" not in res.applied_filters
    unset = s.search(Query(text="postgres"))
    assert unset.ignored_filters == []


def test_a_faceted_query_reports_scoped_confidence(kb):
    """A hard facet selected the rows, so cosine is the wrong yardstick."""
    a = _note(kb, "a", "unrelated words")
    kb.update(a.id, NodePatch(doc_kind="runbook"))
    kb.set_embedding(a.id, _vec(700), embedded_hash="h")
    res = get_strategy("hybrid", kb=kb).search(
        Query(text="unrelated", kind="runbook", embedding=_vec(0)))
    assert res.confidence["level"] == "scoped"


def test_empty_result_is_not_an_error(kb):
    res = get_strategy("hybrid", kb=kb).search(Query(text="nothingmatchesthis"))
    assert res.rows == []
    assert res.confidence["level"] == "none"


def test_tokens_returned_is_populated(kb):
    _note(kb, "a", "some content here")
    res = get_strategy("hybrid", kb=kb).search(Query(text="content"))
    assert res.cost.tokens_returned > 0


def test_fts_only_rank_is_raw_ts_rank_and_score_is_the_decayed_value(kb):
    """Row is a lossless port of SearchResult. In the FTS-only branch (no query
    embedding -- the branch that runs in production whenever the embedder is
    unreachable, see KB_MCP's search_memories), `rank` must stay the raw ts_rank value,
    not silently become RRF-space (1/(60+ordinal)) just because hybrid.py fuses
    every branch through rrf(). `score` is the separate importance/recency-decayed
    value in both branches.

    ts_rank is a pure function of content_tsv/query -- it does not depend on
    access_count/last_accessed_at, so comparing it across the two independent
    calls below needs no snapshot/restore (unlike the equivalence proof, which
    compares the RECENCY-sensitive `score`/decayed ranking across live calls).
    """
    _note(kb, "a", "postgres vector search retrieval")
    legacy = kb.search("postgres vector", limit=5)
    got = get_strategy("hybrid", kb=kb).search(Query(text="postgres vector", limit=5)).rows
    assert len(legacy) == len(got) == 1
    assert got[0].rank == legacy[0].rank
    assert got[0].rank != legacy[0].score  # sanity: raw ts_rank != the decayed score
    assert got[0].score > 0.0
    assert got[0].score == pytest.approx(legacy[0].score, rel=1e-6)


def test_flat_pool_returns_the_full_pool_not_the_page(kb):
    """The crawl needs the flat arm's pool at HYBRID_ARM_POOL breadth -- as hop 0 and
    as L_flat -- while search() slices it to q.limit. If flat_pool truncated, the
    crawl's non-negotiable third seed would silently shrink to the caller's limit."""
    for i in range(5):
        _note(kb, f"n{i}", "postgres tuning notes")
    s = get_strategy("hybrid", kb=kb)
    with kb.read_snapshot() as conn:
        ordered, fused, fts, knn, fts_q = s.flat_pool(Query(text="postgres", limit=2), conn)
    assert len(ordered) == 5
    assert set(ordered) == set(fused)
    assert len(fts) == 5 and knn == []
    assert fts_q == "postgres"


def test_flat_pool_returns_the_rewritten_fts_query(kb):
    """The OR-fallback may rewrite the query, and hydrate()'s ts_headline must be
    given what was ACTUALLY searched or the snippet highlights the wrong terms.
    Returning it is why flat_pool's tuple has five members rather than four."""
    _note(kb, "a", "postgres alpha")
    _note(kb, "b", "vector beta")
    s = get_strategy("hybrid", kb=kb)
    with kb.read_snapshot() as conn:
        _o, _f, _fts, _knn, fts_q = s.flat_pool(Query(text="postgres vector"), conn)
    assert fts_q == "postgres OR vector"
