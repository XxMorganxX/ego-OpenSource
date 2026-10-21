"""Frontier construction -- menus only.

These tests call the hop functions directly with explicit visited sets, because that
is where the three guards' behaviour is observable. strategy.py's own tests cover
the wiring; this file covers what the guards actually do.
"""

from datetime import datetime, timezone
from uuid import UUID

from kb_core.domain import NodePatch, NodeRecord

from kb_retrieval.contract import Query
from kb_retrieval.crawl import frontier as frontier_mod
from kb_retrieval.crawl.frontier import (BRIDGE_FANOUT, FrontierItem, hop1_frontier,
                                         hop2_frontier)
from kb_retrieval.crawl.judge import HOP_JUDGES, HopDecision
from kb_retrieval.registry import get_strategy


def _note(kb, ext, content, **kw):
    return kb.upsert(NodeRecord(source="agent", external_id=ext, type="note",
                                content=content, slug=ext, **kw))


def _grad_vec(weight):
    """Cosine to _grad_vec(1.0) is strictly increasing in `weight`. A one-hot vector
    only ever gives cosine 0 or 1, which cannot express the gradient bridge_frontier
    orders its per-entity fan-out by."""
    v = [0.0] * 1024
    v[0] = weight
    v[1] = 1.0 - weight
    return v


def test_hop1_offers_memories_and_harvests_entities_separately(kb, make_concept,
                                                               make_entity):
    """An entity is not a memory, so entity members are never offered to a judge.
    They come back as hop 2's bridge seeds instead."""
    m0 = _note(kb, "m0", "alpha")
    m1 = _note(kb, "m1", "beta")
    eid = make_entity("Widget", mentions=[m0.id])
    cid = make_concept("C", [(m0.id, "memory", 1.0), (m1.id, "memory", 0.5),
                             (eid, "entity", 0.8)])
    q = Query(text="alpha")
    with kb.read_snapshot() as conn:
        items, bridges = hop1_frontier(kb, [cid], q, conn=conn,
                                       visited_nodes=set(), visited_pairs=set())
    assert [it.node_id for it in items] == [m0.id, m1.id]
    assert [it.idx for it in items] == [0, 1]
    assert all(it.family == "concept" for it in items)
    assert bridges == [eid]
    assert eid not in {it.node_id for it in items}


def test_hop1_offers_in_the_primitives_ordinal_order(kb, make_concept):
    """Pins the CONTRACT -- the frontier's order is the primitive's server-side
    ordinal -- rather than proving the implementation could not have re-derived it
    (the two coincide whenever the SQL already returns ORDER BY ordinal, so no
    fixture can separate them). What is structurally guaranteed is elsewhere:
    menu_score never reaches rank.py, which test_crawl_rank.py covers."""
    notes = [_note(kb, f"n{i}", "x") for i in range(4)]
    cid = make_concept("C", [(n.id, "memory", 0.5) for n in notes])   # all tied
    q = Query(text="x")
    with kb.read_snapshot() as conn:
        out = kb.concept_frontier([cid], under=None, source=None, type=None,
                                  tags=None, kind=None, since=None, until=None,
                                  include_dead=True, conn=conn)
        items, _ = hop1_frontier(kb, [cid], q, conn=conn,
                                 visited_nodes=set(), visited_pairs=set())
    expected = [m["id"] for m in sorted(out["memories"], key=lambda m: m["ordinal"])]
    assert [it.node_id for it in items] == expected


def test_hop1_frontier_item_similarity_matches_similarity_for(kb, make_concept):
    """Nothing reads FrontierItem.similarity today --
    DeterministicHopJudge ignores it -- and no test asserted it anywhere: it could
    return {} for every item and the whole suite would still pass. The (deleted) LLM judge
    rendered this field, and frontier.py's own docstring calls it "the one ruler, the
    same one that later enters ranking" -- load-bearing, not decorative. Pins it
    directly against a SEPARATE similarity_for() call over the same ids and the same
    query embedding, with two members deliberately at DIFFERENT cosines so a
    constant or a swapped value cannot pass by accident."""
    hi = _note(kb, "hi", "alpha")
    kb.set_embedding(hi.id, _grad_vec(1.0), embedded_hash="h1")
    lo = _note(kb, "lo", "beta")
    kb.set_embedding(lo.id, _grad_vec(0.5), embedded_hash="h2")
    cid = make_concept("C", [(hi.id, "memory", 0.5), (lo.id, "memory", 0.5)])
    q = Query(text="alpha", embedding=_grad_vec(1.0))
    with kb.read_snapshot() as conn:
        expected = kb.similarity_for([hi.id, lo.id], q.embedding, conn=conn)
        items, _ = hop1_frontier(kb, [cid], q, conn=conn,
                                 visited_nodes=set(), visited_pairs=set())
    assert {it.node_id for it in items} == {hi.id, lo.id}
    for it in items:
        assert it.similarity == expected[it.node_id]
    assert expected[hi.id] != expected[lo.id]   # rules out a constant/placeholder


def test_hop1_never_offers_a_node_already_in_the_flat_pool(kb, make_concept):
    """strategy.py seeds visited_nodes WITH THE FLAT POOL. L_admit is crawl-rows-only
    by design: letting a flat row also collect an admission vote would
    let judgment promote a row the query had already ranked."""
    flat = _note(kb, "flat", "x")
    fresh = _note(kb, "fresh", "x")
    cid = make_concept("C", [(flat.id, "memory", 1.0), (fresh.id, "memory", 0.9)])
    with kb.read_snapshot() as conn:
        items, _ = hop1_frontier(kb, [cid], Query(text="x"), conn=conn,
                                 visited_nodes={flat.id}, visited_pairs=set())
    assert [it.node_id for it in items] == [fresh.id]


def test_hop2_collapses_one_memory_reached_by_two_entities_but_keeps_both_parents(
        kb, make_entity):
    """LOAD-BEARING for L_corrob. One item per node -- a judge must not see the same
    title at two indices -- but ALL the parents that reached it are kept. If the
    collapse discarded the second parent, every corroboration count would be exactly
    1 and L_corrob would be a constant: present in the fusion, measuring nothing."""
    shared = _note(kb, "shared", "x")
    kb.set_embedding(shared.id, _grad_vec(1.0), embedded_hash="h")
    e1 = make_entity("EntOne", mentions=[shared.id])
    e2 = make_entity("EntTwo", mentions=[shared.id])
    q = Query(text="x", embedding=_grad_vec(1.0))
    with kb.read_snapshot() as conn:
        items, _capped = hop2_frontier(kb, [e1, e2], q, conn=conn,
                                       visited_nodes=set(), visited_pairs=set(),
                                       visited_bridges=set())
    assert len(items) == 1
    assert items[0].node_id == shared.id
    assert set(items[0].parents) == {e1, e2}


def test_truncation_does_not_burn_candidates_no_judge_ever_saw(kb, make_concept,
                                                               monkeypatch):
    """Marking happens AFTER the FRONTIER_OFFERED cut. A candidate truncated away has
    not been expanded, and burning it would deny a later hop a legitimate second
    route to the same memory."""
    monkeypatch.setattr(frontier_mod, "FRONTIER_OFFERED", 2)
    notes = [_note(kb, f"n{i}", "x") for i in range(4)]
    cid = make_concept("C", [(n.id, "memory", 1.0 - i * 0.1)
                             for i, n in enumerate(notes)])
    visited = set()
    with kb.read_snapshot() as conn:
        items, _ = hop1_frontier(kb, [cid], Query(text="x"), conn=conn,
                                 visited_nodes=visited, visited_pairs=set())
    assert len(items) == 2
    assert visited == {it.node_id for it in items}      # exactly the two offered


def test_hop2_returns_nothing_without_an_embedding(kb, make_entity):
    """bridge_frontier orders by cosine, so with no query vector there is nothing to
    order by. Returning early is not a silent skip -- strategy.py has already
    recorded degraded["embedder"]."""
    m = _note(kb, "m", "x")
    eid = make_entity("Widget", mentions=[m.id])
    with kb.read_snapshot() as conn:
        items, capped = hop2_frontier(kb, [eid], Query(text="x"), conn=conn,
                                      visited_nodes=set(), visited_pairs=set(),
                                      visited_bridges=set())
    assert items == [] and capped == []


def test_hop2_bridge_capped_reports_every_entity_including_a_zero_take(kb,
                                                                       make_entity):
    """The design: nothing is refused, it is capped and SHOWN. An entity whose every
    mention was filtered out still appears, with taken=0 -- and it is the only case
    that needs the entity_profile() fallback, since name and support otherwise come
    back on the rows."""
    kept = _note(kb, "kept", "x")
    kb.set_embedding(kept.id, _grad_vec(1.0), embedded_hash="h1")
    unembedded = _note(kb, "unembedded", "x")     # no vector -> filtered out
    e_hit = make_entity("Hit", mentions=[kept.id])
    e_miss = make_entity("Miss", mentions=[unembedded.id])
    q = Query(text="x", embedding=_grad_vec(1.0))
    with kb.read_snapshot() as conn:
        items, capped = hop2_frontier(kb, [e_hit, e_miss], q, conn=conn,
                                      visited_nodes=set(), visited_pairs=set(),
                                      visited_bridges=set())
    by_name = {c["name"]: c for c in capped}
    assert set(by_name) == {"Hit", "Miss"}
    assert by_name["Hit"] == {"name": "Hit", "support": 1, "taken": 1, "of": 1}
    assert by_name["Miss"] == {"name": "Miss", "support": 1, "taken": 0, "of": 1}
    assert [it.node_id for it in items] == [kept.id]


def test_hop2_marks_bridges_visited_so_one_entity_is_never_traversed_twice(kb,
                                                                           make_entity):
    """visited_bridges is keyed on entity id: "a mid-degree instance cannot be
    traversed on both hops". Without it a hub entity harvested at hop 1
    and also named by the entry step would fan out twice."""
    m = _note(kb, "m", "x")
    kb.set_embedding(m.id, _grad_vec(1.0), embedded_hash="h")
    eid = make_entity("Widget", mentions=[m.id])
    q = Query(text="x", embedding=_grad_vec(1.0))
    visited_bridges = set()
    with kb.read_snapshot() as conn:
        first, _ = hop2_frontier(kb, [eid], q, conn=conn, visited_nodes=set(),
                                 visited_pairs=set(), visited_bridges=visited_bridges)
        second, capped = hop2_frontier(kb, [eid], q, conn=conn, visited_nodes=set(),
                                       visited_pairs=set(),
                                       visited_bridges=visited_bridges)
    assert len(first) == 1
    assert visited_bridges == {eid}
    assert second == [] and capped == []
    assert BRIDGE_FANOUT == 8


def test_offer_skips_a_row_whose_pair_is_visited_even_with_visited_nodes_empty(
        kb, make_concept):
    """visited_pairs is REDUNDANT TODAY: _mark_offered always adds a node to
    visited_nodes in the same call it adds that node's pairs, so in production the
    guard's first `or` clause always fires before the second could ever matter.
    That makes the second clause reachable but never exercised by any test that only
    seeds visited_nodes. Seeding visited_pairs directly, with visited_nodes EMPTY, is
    the only way to observe the clause doing anything at all -- it is kept as
    defence-in-depth for a later hop that relaxes visited_nodes (frontier.py's own
    docstring on _mark_offered)."""
    m0 = _note(kb, "m0", "alpha")
    m1 = _note(kb, "m1", "beta")
    cid = make_concept("C", [(m0.id, "memory", 1.0), (m1.id, "memory", 0.9)])
    with kb.read_snapshot() as conn:
        items, _ = hop1_frontier(
            kb, [cid], Query(text="alpha"), conn=conn,
            visited_nodes=set(), visited_pairs={(cid, m0.id, "concept")})
    assert [it.node_id for it in items] == [m1.id]


# ---- render metadata -------------------------------------------------------
# FrontierItem gains title/doc_kind/event_at so an LLM judge can render a row.
# Fetched UNCONDITIONALLY by _offer: the design's "same pipeline with decide()
# replaced by a rule" is falsified by a frontier whose CONTENTS differ by mode.


def test_offer_carries_title_doc_kind_and_event_at_for_every_offered_row(
        kb, make_concept):
    """The three fields frontier.py's own docstring reserved. Pinned against the
    values written through the real write path, with the two members carrying
    DIFFERENT values for each field, so a constant, a swapped pair, or a
    first-row-only fetch cannot pass by accident."""
    when = datetime(2026, 7, 14, 9, 30, tzinfo=timezone.utc)
    m0 = _note(kb, "m0", "alpha", title="Keystone retro", event_at=when)
    kb.update(m0.id, NodePatch(doc_kind="meeting notes"))
    m1 = _note(kb, "m1", "beta", title="Stall postmortem")     # no event_at
    kb.update(m1.id, NodePatch(doc_kind="implementation plan"))
    cid = make_concept("C", [(m0.id, "memory", 1.0), (m1.id, "memory", 0.5)])

    with kb.read_snapshot() as conn:
        items, _ = hop1_frontier(kb, [cid], Query(text="alpha"), conn=conn,
                                 visited_nodes=set(), visited_pairs=set())

    by_id = {it.node_id: it for it in items}
    assert by_id[m0.id].title == "Keystone retro"
    assert by_id[m0.id].doc_kind == "meeting notes"
    assert by_id[m0.id].event_at == when
    assert by_id[m1.id].title == "Stall postmortem"
    assert by_id[m1.id].doc_kind == "implementation plan"
    assert by_id[m1.id].event_at is None


def test_offer_fetches_render_metadata_even_with_no_query_embedding(kb, make_concept):
    """Made machine-checkable: `similarity` IS gated on q.embedding (there is
    nothing to compare against without a query vector); the render metadata is NOT.
    Copying the similarity gate onto the node_meta call is the single most likely
    'simplification' here -- it looks like a free saving and it silently makes the
    frontier mode-dependent, because the embedder-down path is the one that most
    often runs deterministic."""
    m0 = _note(kb, "m0", "alpha", title="Keystone retro")
    cid = make_concept("C", [(m0.id, "memory", 1.0)])

    with kb.read_snapshot() as conn:
        items, _ = hop1_frontier(kb, [cid], Query(text="alpha"), conn=conn,
                                 visited_nodes=set(), visited_pairs=set())

    assert items[0].similarity is None          # no query vector: nothing to compare
    assert items[0].title == "Keystone retro"   # ...but the row still renders


def test_a_frontier_item_defaults_the_three_new_fields(kb):
    """Defaulted, per frontier.py's own docstring ("gains three fields WITH
    DEFAULTS"). Every existing construction site and every test fake builds a
    FrontierItem positionally through the first six fields."""
    it = FrontierItem(idx=0, node_id=UUID(int=1), family="concept",
                      parents=(UUID(int=2),), menu_score=1.0, similarity=0.5)
    assert (it.title, it.doc_kind, it.event_at) == (None, None, None)


def test_the_offered_frontier_is_identical_under_a_deterministic_and_an_llm_judge(
        kb, make_concept, monkeypatch):
    """THE DESIGN'S IDENTITY CLAIM, end to end: "the deterministic crawl is the same
    pipeline with decide() replaced by a rule". The two judges below differ in
    EXACTLY ONE attribute -- uses_llm -- and admit identically, so any difference in
    what they were OFFERED is a difference the mode itself caused.

    Compared by `==` (FrontierItem is a frozen dataclass, so every field including
    the three new ones participates) AND by repr(), which catches a float that
    changed value without changing equality class and any field a future dataclass
    edit adds without a test.

    The fixture is deliberately tiny and the crawl-only notes deliberately
    UNEMBEDDED: knn_candidates has no similarity floor, so any embedded note lands
    in the flat pool, and the flat pool seeds visited_nodes. Keeping them out of it
    makes the hop-1 frontier stable set-wise across the two searches, so
    record_access's importance bump (a write on the read path) cannot move a row in
    or out between them."""
    class _Recorder:
        name = "recorder"
        uses_llm = False
        seen: list = []

        # `admitted` is keyword-only with a default and is IGNORED here. It is
        # declared so that strategy.py's `decide(q, frontier, hop, admitted=(...))`
        # call cannot break these two fakes with a TypeError.
        def decide(self, q, frontier, hop, *, admitted=()):
            type(self).seen.append(list(frontier))
            return HopDecision(admit=list(range(len(frontier))), stop=True, note="rec")

    class _RecorderLLM(_Recorder):
        name = "recorder-llm"
        uses_llm = True
        seen: list = []

    flat = _note(kb, "flat", "postgres tuning notes", title="Flat hit")
    kb.set_embedding(flat.id, _grad_vec(1.0), embedded_hash="hf")
    crawl = []
    for i in range(3):
        n = _note(kb, f"c{i}", "unrelated marmalade lorem", title=f"Crawl only {i}",
                  event_at=datetime(2026, 7, 10 + i, tzinfo=timezone.utc))
        kb.update(n.id, NodePatch(doc_kind="journal entry"))
        crawl.append(n)
    make_concept("Postgres", [(n.id, "memory", 0.9 - i * 0.1)
                              for i, n in enumerate(crawl)],
                 embedding=_grad_vec(1.0))

    monkeypatch.setitem(HOP_JUDGES, "rec", _Recorder)
    monkeypatch.setitem(HOP_JUDGES, "rec-llm", _RecorderLLM)
    monkeypatch.setattr(_Recorder, "seen", [])
    monkeypatch.setattr(_RecorderLLM, "seen", [])
    q = Query(text="postgres", embedding=_grad_vec(1.0))

    get_strategy("cluster-crawl", kb=kb, judge="rec").search(q)
    get_strategy("cluster-crawl", kb=kb, judge="rec-llm").search(q)

    assert len(_Recorder.seen) == 1 and len(_RecorderLLM.seen) == 1
    assert _Recorder.seen[0] == _RecorderLLM.seen[0]
    assert repr(_Recorder.seen[0]) == repr(_RecorderLLM.seen[0])
    # Pin the discriminator: an all-None metadata frontier would compare equal to
    # another all-None one and this test would pass while proving nothing.
    assert [it.title for it in _Recorder.seen[0]] == ["Crawl only 0", "Crawl only 1",
                                                      "Crawl only 2"]
