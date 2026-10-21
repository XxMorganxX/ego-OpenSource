"""Replay capture: a faithful per-stage record that never changes the search."""

import json

from kb_core.domain import NodeRecord

import kb_retrieval.replay as replay_mod
from kb_retrieval.crawl.frontier import FRONTIER_OFFERED
from kb_retrieval.contract import Query
from kb_retrieval.registry import get_strategy


def _note(kb, ext, content, **kw):
    return kb.upsert(NodeRecord(source="agent", external_id=ext, type="note",
                                content=content, slug=ext, **kw))


def _vec(hot):
    v = [0.0] * 1024
    v[hot % 1024] = 1.0
    return v


def _no_access_writes(kb, monkeypatch):
    # record_access bumps the importance/recency multiplier the FTS arm orders by;
    # comparing two searches needs it neutralized (see test_equivalence._compare).
    monkeypatch.setattr(kb, "_record_access", lambda ids: None)


def _corpus(kb):
    notes = {}
    for i, body in enumerate(["postgres vector index", "postgres budget",
                              "vector latency", "meeting notes"]):
        n = _note(kb, f"n{i}", body)
        kb.set_embedding(n.id, _vec(i), embedded_hash=f"h{i}")
        notes[body] = n
    return notes


def test_hybrid_replay_matches_the_returned_rows(kb, monkeypatch):
    _no_access_writes(kb, monkeypatch)
    _corpus(kb)
    res = get_strategy("hybrid", kb=kb).search(
        Query(text="postgres", embedding=_vec(0), limit=2))
    rep = res.replay
    assert rep["v"] == 1 and rep["limit"] == 2 and "crawl" not in rep
    flat = rep["flat"]
    assert [r["id"] for r in flat["fused"][:2]] == [str(r.id) for r in res.rows]
    assert len(flat["fused"]) >= len(res.rows)          # full pool, not the page
    assert flat["fts_query"] == "postgres" and flat["or_fallback"] is False
    assert flat["embedder"] == "ok"
    assert {r["id"] for r in flat["fts"]} <= {r["id"] for r in flat["fused"]}
    assert all(r["sim"] is not None for r in flat["knn"])
    assert all(isinstance(r["rrf"], float) for r in flat["fused"])


def test_hybrid_replay_records_the_or_fallback_rewrite(kb, monkeypatch):
    _no_access_writes(kb, monkeypatch)
    _corpus(kb)
    rep = get_strategy("hybrid", kb=kb).search(
        Query(text="budget latency", limit=5)).replay
    assert rep["flat"]["or_fallback"] is True
    assert rep["flat"]["fts_query"] == "budget OR latency"


def test_hybrid_replay_without_an_embedding_says_so(kb, monkeypatch):
    _no_access_writes(kb, monkeypatch)
    _corpus(kb)
    flat = get_strategy("hybrid", kb=kb).search(Query(text="postgres")).replay["flat"]
    assert flat["knn"] == [] and flat["embedder"] == "unavailable"


def test_a_replay_failure_never_changes_the_hybrid_search(kb, monkeypatch):
    _no_access_writes(kb, monkeypatch)
    _corpus(kb)
    s = get_strategy("hybrid", kb=kb)
    q = Query(text="postgres", embedding=_vec(0), limit=3)
    before = [r.id for r in s.search(q).rows]

    def boom(**_kw):
        raise RuntimeError("boom")
    monkeypatch.setattr(replay_mod, "flat_stage", boom)
    res = s.search(q)
    assert [r.id for r in res.rows] == before
    assert res.replay is None
    assert res.trace.degraded["replay"] == "RuntimeError: boom"


def test_hybrid_replay_stays_under_the_size_budget(kb, monkeypatch):
    _no_access_writes(kb, monkeypatch)
    for i in range(120):
        n = _note(kb, f"big{i}", f"postgres vector note number {i}")
        kb.set_embedding(n.id, _vec(i), embedded_hash=f"b{i}")
    # Budget is pinned at the DEFAULT limit; arms are max(limit, 50) deep, so a
    # limit=200 replay is legitimately larger.
    rep = get_strategy("hybrid", kb=kb).search(
        Query(text="postgres vector", embedding=_vec(3), limit=20)).replay
    assert len(rep["flat"]["fts"]) == 50 and len(rep["flat"]["knn"]) == 50
    assert len(json.dumps(rep)) < 64 * 1024


def _crawl_seed(kb, make_concept):
    """A flat hit plus an UNEMBEDDED crawl-only note reachable only through the
    concept (an embedded one would land in the flat pool -- knn has no floor)."""
    flat = _note(kb, "flat", "postgres tuning notes")
    kb.set_embedding(flat.id, _vec(0), embedded_hash="hf")
    crawl_only = _note(kb, "crawlonly", "unrelated marmalade lorem")
    cid = make_concept("Postgres", [(crawl_only.id, "memory", 1.0)], embedding=_vec(0))
    return flat, crawl_only, cid


def test_crawl_replay_records_entry_hop_lists_and_final(kb, make_concept, monkeypatch):
    _no_access_writes(kb, monkeypatch)
    flat, crawl_only, cid = _crawl_seed(kb, make_concept)
    res = get_strategy("cluster-crawl", kb=kb).search(
        Query(text="postgres", embedding=_vec(0), limit=5))
    rep = res.replay
    crawl = rep["crawl"]
    assert {"kind": "concept", "id": str(cid), "label": "Postgres"}.items() <= \
        crawl["entry"][0].items()
    hop = crawl["hops"][0]
    assert (hop["offered_count"], hop["admitted_count"]) == (
        res.trace.hops[0]["offered"], res.trace.hops[0]["admitted"])
    offered = {o["id"]: o for o in hop["offered"]}
    assert offered[str(crawl_only.id)]["admitted"] is True
    assert offered[str(crawl_only.id)]["parents"] == [
        {"kind": "concept", "id": str(cid), "label": "Postgres"}]
    assert [r["id"] for r in crawl["final"][:5]] == [str(r.id) for r in res.rows]
    assert crawl["lists"]["admit"] == []           # gated off: deterministic judge
    assert set(crawl["lists"]["flat"]) == {r["id"] for r in rep["flat"]["fused"]}
    assert crawl["stop_reason"] == res.trace.stop_reason
    assert str(crawl_only.id) not in {r["id"] for r in rep["flat"]["fused"]}


def test_crawl_replay_without_entry_seeds_records_an_empty_route(kb, monkeypatch):
    _no_access_writes(kb, monkeypatch)
    n = _note(kb, "solo", "postgres alone")
    kb.set_embedding(n.id, _vec(5), embedded_hash="hs")
    crawl = get_strategy("cluster-crawl", kb=kb).search(
        Query(text="postgres", embedding=_vec(5))).replay["crawl"]
    assert crawl["entry"] == [] and crawl["hops"] == []


def test_a_replay_failure_never_changes_the_crawl(kb, make_concept, monkeypatch):
    _no_access_writes(kb, monkeypatch)
    _crawl_seed(kb, make_concept)
    s = get_strategy("cluster-crawl", kb=kb)
    q = Query(text="postgres", embedding=_vec(0), limit=5)
    before = [r.id for r in s.search(q).rows]

    def boom(**_kw):
        raise ValueError("bad hop")
    monkeypatch.setattr(replay_mod, "hop_record", boom)
    res = s.search(q)
    assert [r.id for r in res.rows] == before
    assert res.replay is None
    assert res.trace.degraded["replay"] == "ValueError: bad hop"


def test_crawl_replay_stays_under_the_size_budget(kb, make_concept, monkeypatch):
    _no_access_writes(kb, monkeypatch)
    members = []
    for i in range(160):
        n = _note(kb, f"c{i}", f"postgres crawl note {i}")
        if i < 60:
            kb.set_embedding(n.id, _vec(i), embedded_hash=f"c{i}")
        members.append((n.id, "memory", 1.0))
    make_concept("Postgres", members, embedding=_vec(0))
    rep = get_strategy("cluster-crawl", kb=kb).search(
        Query(text="postgres crawl", embedding=_vec(0), limit=20)).replay
    hop = rep["crawl"]["hops"][0]
    assert hop["offered_count"] == FRONTIER_OFFERED == len(hop["offered"])  # full menu recorded
    assert len(json.dumps(rep)) < 64 * 1024


def test_capture_replay_equals_search_replay_and_records_no_access(kb, monkeypatch):
    _corpus(kb)
    touched = []
    monkeypatch.setattr(kb, "_record_access", lambda ids: touched.extend(ids))
    s = get_strategy("hybrid", kb=kb)
    q = Query(text="postgres", embedding=_vec(0), limit=2)
    captured, degraded = s.capture_replay(q)
    assert touched == [] and degraded == {}
    assert captured == s.search(q).replay


def test_flat_stage_maps_non_finite_floats_to_none():
    # Postgres jsonb rejects NaN/Infinity; one would drop the whole trace row.
    out = replay_mod.flat_stage(
        ordered=["a"], fused={"a": float("inf")},
        fts=[{"id": "a", "score": float("nan")}],
        knn=[{"id": "a", "similarity": float("-inf")}],
        fts_q="q", query_text="q", embedded=True)
    assert out["fts"][0]["score"] is None
    assert out["knn"][0]["sim"] is None
    assert out["fused"][0]["rrf"] is None
    json.dumps(out, allow_nan=False)
