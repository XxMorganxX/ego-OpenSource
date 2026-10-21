"""Which query vector reaches which consumer.

Every equivalence/agreement test elsewhere feeds ONE vector to both sides, so a
swapped field would stay green there. These feed two DISTINCT vectors -- RETRIEVAL
(the instructed query vector) and LABEL (the bare one) -- and assert each consumer
receives the right one."""
from kb_core.domain import NodeRecord
from kb_retrieval.contract import Query
from kb_retrieval.crawl.entry import entry_step
from kb_retrieval.registry import get_strategy


def _unit(i):
    v = [0.0] * 1024
    v[i] = 1.0
    return v


RETRIEVAL, LABEL = _unit(3), _unit(7)


def _note(kb, ext, content):
    return kb.upsert(NodeRecord(source="agent", external_id=ext, type="note",
                                content=content, slug=ext))


def _spy(monkeypatch, kb, name, seen, vec_at):
    """Record the vector argument of kb.<name>: `vec_at` is a positional index or
    the keyword name."""
    real = getattr(kb, name)

    def spy(*a, **kw):
        seen.setdefault(name, []).append(kw[vec_at] if isinstance(vec_at, str) else a[vec_at])
        return real(*a, **kw)

    monkeypatch.setattr(kb, name, spy)


def test_entry_step_routes_the_label_vector(kb, monkeypatch):
    seen = {}
    _spy(monkeypatch, kb, "concept_candidates", seen, 0)
    _spy(monkeypatch, kb, "find_entities", seen, "embedding")
    with kb.read_snapshot() as conn:
        entry_step(kb, Query(text="anything", embedding=RETRIEVAL, label_embedding=LABEL), conn)
    assert seen["concept_candidates"] == [LABEL]
    assert seen["find_entities"] == [LABEL]


def test_entry_step_without_a_label_vector_uses_the_retrieval_vector(kb, monkeypatch):
    seen = {}
    _spy(monkeypatch, kb, "concept_candidates", seen, 0)
    with kb.read_snapshot() as conn:
        entry_step(kb, Query(text="anything", embedding=RETRIEVAL), conn)
    assert seen["concept_candidates"] == [RETRIEVAL]


def test_hybrid_ranks_only_on_the_retrieval_vector(kb, monkeypatch):
    seen = {}
    _spy(monkeypatch, kb, "knn_candidates", seen, 0)
    _spy(monkeypatch, kb, "concept_candidates", seen, 0)
    get_strategy("hybrid", kb=kb).search(
        Query(text="anything", embedding=RETRIEVAL, label_embedding=LABEL))
    assert seen["knn_candidates"] == [RETRIEVAL]
    assert "concept_candidates" not in seen


def test_crawl_ranks_and_offers_on_retrieval_and_routes_on_label(kb, monkeypatch, make_entity):
    # Same reachable hop-2 path as test_bench_knobs.py::test_bridge_fanout_is_threaded:
    # the name anchor seeds an entity, so hop 2 calls bridge_frontier.
    m = _note(kb, "m", "unrelated marmalade lorem")
    kb.set_embedding(m.id, RETRIEVAL, embedded_hash="hm")
    make_entity("Zeta", mentions=[m.id])
    seen = {}
    for name, at in (("knn_candidates", 0), ("similarity_for", 1), ("bridge_frontier", 1),
                     ("concept_candidates", 0), ("find_entities", "embedding")):
        _spy(monkeypatch, kb, name, seen, at)
    get_strategy("cluster-crawl", kb=kb, judge="none").search(
        Query(text="Zeta", embedding=RETRIEVAL, label_embedding=LABEL, limit=20))
    for name in ("knn_candidates", "similarity_for", "bridge_frontier"):
        assert seen.get(name), f"{name} never called -- the corpus no longer reaches it"
        assert all(v == RETRIEVAL for v in seen[name]), name
    for name in ("concept_candidates", "find_entities"):
        assert seen.get(name) and all(v == LABEL for v in seen[name]), name
