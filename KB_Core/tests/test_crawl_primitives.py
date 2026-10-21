"""Read primitives the cluster-crawl fuses over.

Same contract as test_search_primitives.py -- KB_Core owns the SQL, KB_Retrieval
owns policy, and every candidate primitive returns an explicit server-side ordinal
so Python never re-derives a Postgres ordering. Split into its own file because the
mode-A primitives and the mode-B primitives answer different questions and the
combined file would run past 900 lines.

Helpers are duplicated from test_search_primitives.py rather than imported: that is
the existing house convention (test_hybrid.py and test_equivalence.py each define
their own), and it keeps either file runnable in isolation.
"""

import math
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from kb_core.domain import NodePatch, NodeRecord
from kb_core.entities import BELONGS_TO, MENTIONS


def _note(kb, ext, content, **kw):
    return kb.upsert(NodeRecord(source="agent", external_id=ext, type="note",
                                content=content, slug=ext, **kw))


def _filters(**kw):
    base = dict(under=None, source=None, type=None, tags=None, kind=None,
                since=None, until=None, include_dead=True)
    base.update(kw)
    return base


def _vec(hot):
    v = [0.0] * 1024
    v[hot] = 1.0
    return v


def _grad_vec(weight):
    """A 2-hot vector whose cosine to _vec(0) is strictly increasing in `weight`.
    _vec's one-hot vectors only ever give cosine 0 or 1, which cannot express the
    rankable gradient bridge_frontier's per-entity cap has to be tested against."""
    v = [0.0] * 1024
    v[0] = weight
    v[1] = 1.0 - weight
    return v


# ---------------------------------------------------------------------------
# similarity_for
# ---------------------------------------------------------------------------

def test_similarity_for_agrees_with_knn_candidates_for_the_same_note_and_vector(kb):
    """similarity_for is the one ruler fusion uses to score BOTH flat and crawled
    rows. If it disagreed with knn_candidates for the same (note, vector) pair, two
    candidate sources would silently be compared on different rulers and fusion
    would be corrupted without ever raising. Pin the agreement."""
    a = _note(kb, "a", "alpha")
    b = _note(kb, "b", "beta")
    kb.set_embedding(a.id, _vec(0), embedded_hash="h1")
    kb.set_embedding(b.id, _vec(1), embedded_hash="h2")
    knn_by_id = {r["id"]: r["similarity"]
                 for r in kb.knn_candidates(_vec(0), pool=50, **_filters())}
    got = kb.similarity_for([a.id, b.id], _vec(0), **_filters())
    assert got[a.id] == pytest.approx(knn_by_id[a.id])
    assert got[b.id] == pytest.approx(knn_by_id[b.id])


def test_similarity_for_scores_a_note_by_its_best_chunk(kb):
    """Mirrors test_knn_candidates_scores_a_note_by_its_best_chunk -- the same
    greatest(whole-note, best-chunk) reduction, and the same answer as that
    primitive for the identical (note, vector) pair."""
    n = _note(kb, "long", "unrelated head text")
    kb.set_embedding(n.id, _vec(700), embedded_hash="h")
    kb.set_chunk_embeddings(n.id, [(0, 10, _vec(0))])
    knn_by_id = {r["id"]: r["similarity"]
                 for r in kb.knn_candidates(_vec(0), pool=50, **_filters())}
    got = kb.similarity_for([n.id], _vec(0), **_filters())
    assert got[n.id] > 0.9
    assert got[n.id] == pytest.approx(knn_by_id[n.id])


def test_similarity_for_has_no_similarity_floor(kb):
    """Mirrors test_knn_candidates_has_no_similarity_floor: a distant vector's id is
    still SCORED (present, with a value), not dropped. A floor was tried and
    lowered recall; see negatives.py."""
    a = _note(kb, "a", "alpha")
    kb.set_embedding(a.id, _vec(500), embedded_hash="h")
    got = kb.similarity_for([a.id], _vec(0), **_filters())
    assert a.id in got
    assert got[a.id] is not None


def test_similarity_for_honours_the_shared_filter_block(kb):
    # NodeRecord has no doc_kind field -- set post-write via update()/NodePatch,
    # same as tests/test_search_precision.py does it.
    a = _note(kb, "keep", "x")
    b = _note(kb, "drop", "x")
    kb.update(a.id, NodePatch(doc_kind="meeting notes"))
    kb.update(b.id, NodePatch(doc_kind="design spec"))
    kb.set_embedding(a.id, _vec(0), embedded_hash="h1")
    kb.set_embedding(b.id, _vec(0), embedded_hash="h2")
    got = kb.similarity_for([a.id, b.id], _vec(0), **_filters(kind="meeting notes"))
    # `b` is OMITTED, not scored None -- a facet-excluded row must not be scored at
    # all, which is the same "never outflank a facet" contract as everywhere else
    # that interpolates SEARCH_FILTERS.
    assert set(got) == {a.id}


def test_similarity_for_omits_unknown_ids_rather_than_scoring_them(kb):
    a = _note(kb, "a", "x")
    kb.set_embedding(a.id, _vec(0), embedded_hash="h")
    got = kb.similarity_for([a.id, uuid4()], _vec(0), **_filters())
    assert set(got) == {a.id}


def test_similarity_for_returns_none_for_an_in_scope_id_with_no_embedding_at_all(kb):
    """Distinguishes 'excluded by filter / unknown' (OMITTED) from 'in scope but
    genuinely unscoreable' (PRESENT, value None). The crawl needs those two signals
    to mean different things: the first says the row should never have been in the
    pool; the second says this ruler has nothing to say about a row that belongs."""
    a = _note(kb, "a", "no vectors here")
    got = kb.similarity_for([a.id], _vec(0), **_filters())
    assert got == {a.id: None}


def test_similarity_for_omits_an_entity_type_id_unless_type_is_named(kb):
    """CORRECTION pinned as a test. SEARCH_FILTERS' scaffolding exclusion
    (SEARCH_SCAFFOLDING_TYPES = ('entity', 'folder')) drops type='entity' rows
    unless the caller passes an explicit `type=`. A caller that mixes entity-kind
    concept_members ids into one similarity_for() call therefore has those ids
    OMITTED -- so `got[entity_id]` raises KeyError, it does NOT return None. The
    original draft's caveat section asserted the opposite about this exact
    scenario; the verifier disproved it by execution."""
    ent = kb.upsert(NodeRecord(source="agent", external_id="ent1", type="entity",
                               content="an entity doc", slug="ent1"))
    omitted = kb.similarity_for([ent.id], _vec(0), **_filters())
    assert ent.id not in omitted
    present = kb.similarity_for([ent.id], _vec(0), **_filters(type="entity"))
    assert present == {ent.id: None}


def test_similarity_for_on_empty_input_is_empty(kb):
    assert kb.similarity_for([], _vec(0), **_filters()) == {}


def test_similarity_for_honours_a_supplied_conn(kb):
    a = _note(kb, "a", "x")
    kb.set_embedding(a.id, _vec(0), embedded_hash="h")
    with kb.read_snapshot() as conn:
        got = kb.similarity_for([a.id], _vec(0), **_filters(), conn=conn)
    assert got[a.id] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# concept_candidates
# ---------------------------------------------------------------------------

def _concept(kb, name, *, members, embedding=None, description=None):
    """One active concept via the real write path. Concepts have no direct create()
    primitive -- apply_concept_run is the only way in, mirroring test_expand.py.
    `members` is [(node_id, member_kind, score), ...]."""
    run = kb.begin_concept_run({})
    kb.apply_concept_run(run, [
        {"key": name, "prev_id": None, "parent_key": None, "level": 0,
         "name": name, "kind": "topic", "description": description,
         "embedding": embedding, "members": members}], [])
    kb.finish_concept_run(run, status="done", stats={})
    return {c["name"]: c for c in kb.active_concepts()}[name]["id"]


def _retire(kb, concept_id):
    run = kb.begin_concept_run({})
    kb.apply_concept_run(run, [], [concept_id])
    kb.finish_concept_run(run, status="done", stats={})


def test_concept_candidates_label_rank_is_dense_ordinals_from_one(kb):
    near = _concept(kb, "Near", members=[], embedding=_vec(0))
    far = _concept(kb, "Far", members=[], embedding=_vec(1))
    rows = {r["concept_id"]: r for r in kb.concept_candidates(_vec(0))}
    assert rows[near]["label_rank"] == 1
    assert rows[far]["label_rank"] == 2
    # No memories exist at all here, so family B never fires -- and BOTH ranks being
    # populated is not something a caller may assume.
    assert rows[near]["vote_rank"] is None and rows[far]["vote_rank"] is None


def test_concept_candidates_with_no_concepts_is_empty(kb):
    assert kb.concept_candidates(_vec(0)) == []


def test_concept_candidates_omits_concepts_with_neither_signal(kb):
    lonely = _concept(kb, "Lonely", members=[])   # no embedding, no memory members
    ids = {r["concept_id"] for r in kb.concept_candidates(_vec(0))}
    assert lonely not in ids


def test_concept_candidates_damps_vote_by_member_count(kb):
    """The /log2(1+member_count) divider measurably improves top-1 on a golden set.
    This test proves the MECHANISM -- a small, tightly-matching concept outranks a big
    one that ties it on raw evidence -- not the golden-set number itself."""
    m_big = _note(kb, "big", "x")
    kb.set_embedding(m_big.id, _vec(0), embedded_hash="hbig")
    m_small = _note(kb, "small", "y")
    kb.set_embedding(m_small.id, _vec(0), embedded_hash="hsmall")
    # Padding: 'entity'-kind members inflate member_count without contributing a
    # vote, because votes only ever join member_kind = 'memory'.
    padding = [_note(kb, f"pad{i}", "z").id for i in range(7)]
    big = _concept(kb, "Big",
                   members=[(m_big.id, "memory", 1.0)]
                           + [(p, "entity", 1.0) for p in padding])
    small = _concept(kb, "Small", members=[(m_small.id, "memory", 1.0)])

    damped = {r["concept_id"]: r for r in kb.concept_candidates(_vec(0))}
    assert damped[small]["vote_rank"] == 1
    assert damped[big]["vote_rank"] == 2
    assert damped[small]["vote_score"] == pytest.approx(1.0, rel=1e-6)
    assert damped[big]["vote_score"] == pytest.approx(1.0 / math.log2(9), rel=1e-6)

    undamped = {r["concept_id"]: r
                for r in kb.concept_candidates(_vec(0), damp=False)}
    assert undamped[small]["vote_score"] == pytest.approx(1.0, rel=1e-6)
    assert undamped[big]["vote_score"] == pytest.approx(1.0, rel=1e-6)


def test_concept_candidates_ignores_stale_members_of_a_retired_concept(kb):
    """CORRECTED. apply_concept_run's retire path flips concepts.status but never
    deletes concept_members, so a retired concept's stale membership
    must not vote -- and critically must not even consume a rank slot in
    `ranked_votes`' row_number() window, which would shift a legitimately active
    concept's vote_rank.

    A version of this test with ONLY the retired concept present passes even with
    the `AND c.status = 'active'` join guard DELETED, because the outer WHERE
    already drops retired rows from the output -- it proves nothing about the inner
    guard. A second, active, co-voting concept is required to observe the rank
    pollution the guard prevents (verified: with the guard removed, `real` gets
    vote_rank 2)."""
    m_ghost = _note(kb, "ghost-mem", "x")
    kb.set_embedding(m_ghost.id, _vec(0), embedded_hash="hg")
    ghost = _concept(kb, "Ghost", members=[(m_ghost.id, "memory", 1.0)])
    _retire(kb, ghost)

    m_real = _note(kb, "real-mem", "y")
    kb.set_embedding(m_real.id, _vec(0), embedded_hash="hr")
    real = _concept(kb, "Real", members=[(m_real.id, "memory", 1.0)])

    rows = {r["concept_id"]: r for r in kb.concept_candidates(_vec(0))}
    assert ghost not in rows
    assert real in rows
    assert rows[real]["vote_rank"] == 1


def test_concept_candidates_honours_the_shared_filter_block(kb):
    keep_mem = _note(kb, "keep", "x")
    kb.update(keep_mem.id, NodePatch(doc_kind="meeting notes"))
    kb.set_embedding(keep_mem.id, _vec(0), embedded_hash="h1")
    drop_mem = _note(kb, "drop", "y")
    kb.update(drop_mem.id, NodePatch(doc_kind="design spec"))
    kb.set_embedding(drop_mem.id, _vec(0), embedded_hash="h2")
    keep_c = _concept(kb, "Keep", members=[(keep_mem.id, "memory", 1.0)])
    drop_c = _concept(kb, "Drop", members=[(drop_mem.id, "memory", 1.0)])

    rows = {r["concept_id"]: r for r in
            kb.concept_candidates(_vec(0), kind="meeting notes")}
    assert keep_c in rows and rows[keep_c]["vote_rank"] == 1
    assert drop_c not in rows   # its sole memory never entered family B's ANN leg


def test_concept_candidates_k_caps_the_ann_leg(kb):
    near = _note(kb, "near", "x")
    kb.set_embedding(near.id, _vec(0), embedded_hash="hn")
    fars = []
    for i in range(1, 5):
        f = _note(kb, f"far{i}", "y")
        kb.set_embedding(f.id, _vec(i), embedded_hash=f"hf{i}")
        fars.append(f)
    near_c = _concept(kb, "NearC", members=[(near.id, "memory", 1.0)])
    far_cs = [_concept(kb, f"FarC{i}", members=[(f.id, "memory", 1.0)])
              for i, f in enumerate(fars, start=1)]

    rows = {r["concept_id"]: r for r in kb.concept_candidates(_vec(0), k=1)}
    # `near` is the unique nearest (distance 0); every `far*` is orthogonal
    # (distance 1) and tied with the others, so none is the unique top match and
    # k=1 unambiguously admits only `near` regardless of how the tie breaks.
    assert rows[near_c]["vote_rank"] == 1
    assert all(fc not in rows for fc in far_cs)


def test_concept_candidates_honours_a_supplied_conn(kb):
    near = _concept(kb, "Near", members=[], embedding=_vec(0))
    with kb.read_snapshot() as conn:
        rows = kb.concept_candidates(_vec(0), conn=conn)
    assert rows[0]["concept_id"] == near


# ---------------------------------------------------------------------------
# last_consolidation
# ---------------------------------------------------------------------------

def test_last_consolidation_is_none_before_any_run(kb):
    """None means "the concept layer has never been built", which a caller must be
    able to tell apart from "the crawl found nothing" -- an empty trace.entry looks
    identical in both cases otherwise."""
    assert kb.last_consolidation() is None


def test_last_consolidation_reports_the_latest_finished_run_only(kb):
    _concept(kb, "One", members=[], embedding=_vec(0))
    first = kb.last_consolidation()
    assert first is not None
    # A run still in flight has finished_at NULL and must not move the timestamp
    # backwards or forwards; a 'failed' run would overstate freshness.
    running = kb.begin_concept_run({})
    assert kb.last_consolidation() == first
    kb.finish_concept_run(running, status="failed", stats={})
    assert kb.last_consolidation() == first


# ---------------------------------------------------------------------------
# concept_frontier
# ---------------------------------------------------------------------------

def test_concept_frontier_returns_dense_ordinals_and_entity_support(kb):
    m0 = _note(kb, "m0", "alpha memory")
    m1 = _note(kb, "m1", "beta memory")
    ent = kb.upsert_entity("Widget", categories=["tool"])
    kb.link_entity(m0.id, ent.node.id, MENTIONS, weight=0.9)
    kb.link_entity(m1.id, ent.node.id, MENTIONS, weight=0.5)
    cid = _concept(kb, "Alpha", members=[
        (m0.id, "memory", 1.0), (m1.id, "memory", 0.5),
        (ent.node.id, "entity", 0.8)])

    out = kb.concept_frontier([cid], **_filters())
    assert [r["ordinal"] for r in out["memories"]] == [1, 2]
    assert out["memories"][0]["id"] == m0.id and out["memories"][0]["cm_score"] == 1.0
    assert out["entities"][0]["entity_id"] == ent.node.id
    assert out["entities"][0]["name"] == "Widget"
    assert out["entities"][0]["support"] == 2   # distinct memories mentioning it
    assert out["entities"][0]["ordinal"] == 1


def test_concept_frontier_ordinal_is_dense_across_multiple_concept_ids(kb):
    """CLOSES A COVERAGE GAP. Every other concept_frontier test passes a
    single-element concept_ids list, but the load-bearing claim -- ONE dense
    row_number() spanning ALL requested concepts, not reset per concept,
    deterministic even when the same node belongs to two of them -- is a claim
    specifically about the multi-id case. One row per (concept_id, node_id)
    membership, because that pair is the concept_members PK."""
    m_shared = _note(kb, "shared", "x")
    m_only_a = _note(kb, "only_a", "x")
    cid_a = _concept(kb, "Alpha", members=[(m_shared.id, "memory", 0.9),
                                           (m_only_a.id, "memory", 0.5)])
    cid_b = _concept(kb, "Beta", members=[(m_shared.id, "memory", 0.9)])

    out = kb.concept_frontier([cid_a, cid_b], **_filters())
    assert [r["ordinal"] for r in out["memories"]] == [1, 2, 3]
    assert [r["id"] for r in out["memories"]].count(m_shared.id) == 2


def test_concept_frontier_on_empty_input_is_empty(kb):
    assert kb.concept_frontier([]) == {"memories": [], "entities": []}


def test_concept_frontier_honours_the_shared_filter_block_on_memories_only(kb):
    m0 = _note(kb, "m0", "budget review")
    m1 = _note(kb, "m1", "budget review")
    kb.update(m0.id, NodePatch(doc_kind="meeting notes"))
    kb.update(m1.id, NodePatch(doc_kind="design spec"))
    ent = kb.upsert_entity("Widget", categories=["tool"])
    kb.link_entity(m0.id, ent.node.id, MENTIONS, weight=0.9)
    cid = _concept(kb, "Alpha", members=[
        (m0.id, "memory", 1.0), (m1.id, "memory", 0.5),
        (ent.node.id, "entity", 0.8)])

    out = kb.concept_frontier([cid], **_filters(kind="meeting notes"))
    assert [r["id"] for r in out["memories"]] == [m0.id]
    assert len(out["entities"]) == 1   # a memory-only facet leaves the entity arm alone


def test_concept_frontier_entity_side_ignores_under_because_entities_live_elsewhere(kb):
    """Entities are nodes but never memories: every entity node is
    type='entity', source='entity' and lives under its own `entities.*` subtree.
    A memory-content `under=` scope therefore
    legitimately empties the memory arm while leaving the entity arm untouched."""
    m0 = _note(kb, "m0", "x")
    ent = kb.upsert_entity("Widget", categories=["tool"])
    kb.link_entity(m0.id, ent.node.id, MENTIONS, weight=0.9)
    cid = _concept(kb, "Alpha", members=[(m0.id, "memory", 1.0),
                                         (ent.node.id, "entity", 0.8)])

    out = kb.concept_frontier([cid], **_filters(under="projects.nowhere"))
    assert out["memories"] == []
    assert len(out["entities"]) == 1


def test_concept_frontier_is_unbounded(kb):
    """No pool/LIMIT on either arm. Active-concept memory membership is small and
    drifts as the corpus grows.
    Nothing is close to a pool-sized cap, so a LIMIT here would be an unrequested
    truncation, not a safety net. 12 is an arbitrary stand-in; the point is that no
    LIMIT clause silently trims."""
    mems = [_note(kb, f"bulk{i}", "x") for i in range(12)]
    cid = _concept(kb, "Bulk", members=[(m.id, "memory", 1.0) for m in mems])
    out = kb.concept_frontier([cid], **_filters())
    assert len(out["memories"]) == 12
    assert [r["ordinal"] for r in out["memories"]] == list(range(1, 13))


def test_concept_frontier_does_not_filter_class_members_itself(kb):
    """Documents the design choice: this primitive trusts concept_members as-is and
    does NOT re-filter entity_type. Safety comes from the emergent fact
    that consolidation never puts classes in an ACTIVE concept's membership,
    not from a redundant SQL guard -- and if that
    invariant ever regresses, the fix belongs in consolidation. A class member that
    IS present is handed back faithfully rather than silently hidden."""
    cls = kb.upsert_entity_class("SomeClass")
    cid = _concept(kb, "Alpha", members=[(cls.node.id, "entity", 1.0)])
    out = kb.concept_frontier([cid], **_filters())
    assert [r["entity_id"] for r in out["entities"]] == [cls.node.id]


def test_concept_frontier_honours_a_supplied_conn(kb):
    m0 = _note(kb, "m0", "x")
    cid = _concept(kb, "Alpha", members=[(m0.id, "memory", 1.0)])
    with kb.read_snapshot() as conn:
        out = kb.concept_frontier([cid], **_filters(), conn=conn)
    assert out["memories"][0]["id"] == m0.id


# ---------------------------------------------------------------------------
# bridge_frontier
# ---------------------------------------------------------------------------

def test_bridge_frontier_caps_per_entity_not_globally(kb):
    """entA's memories are ALL more similar to the query than every one of entB's, so
    a naive global `ORDER BY similarity DESC LIMIT fanout` would starve entB
    completely. PARTITION BY entity_id must not let that happen."""
    from collections import Counter
    ent_a = kb.upsert_entity("EntityA", categories=["tool"])
    ent_b = kb.upsert_entity("EntityB", categories=["tool"])
    a_mems = []
    for i, w in enumerate([0.99, 0.95, 0.9]):
        n = _note(kb, f"a{i}", "x")
        kb.set_embedding(n.id, _grad_vec(w), embedded_hash=f"ha{i}")
        kb.link_entity(n.id, ent_a.node.id, MENTIONS, weight=0.9)
        a_mems.append(n)
    for i, w in enumerate([0.7, 0.6, 0.5]):
        n = _note(kb, f"b{i}", "x")
        kb.set_embedding(n.id, _grad_vec(w), embedded_hash=f"hb{i}")
        kb.link_entity(n.id, ent_b.node.id, MENTIONS, weight=0.9)

    rows = kb.bridge_frontier([ent_a.node.id, ent_b.node.id], _grad_vec(1.0),
                              fanout=2, **_filters())
    by_entity = Counter(r["via_entity_id"] for r in rows)
    assert by_entity[ent_a.node.id] == 2
    assert by_entity[ent_b.node.id] == 2          # not starved despite lower cosine
    assert len(rows) == 4
    assert rows[0]["via_entity_id"] == ent_a.node.id   # global ordinal still by cosine
    assert [r["memory_id"] for r in rows if r["via_entity_id"] == ent_a.node.id] == \
        [a_mems[0].id, a_mems[1].id]              # A's top 2 of 3
    assert all(r["entity_support"] == 3 for r in rows)


def test_bridge_frontier_excludes_class_entities(kb):
    cls = kb.upsert_entity_class("SomeClass")
    m = _note(kb, "m", "x")
    kb.set_embedding(m.id, _grad_vec(1.0), embedded_hash="h")
    kb.link_entity(m.id, cls.node.id, BELONGS_TO)   # the realistic shape
    assert kb.bridge_frontier([cls.node.id], _grad_vec(1.0), fanout=10,
                              **_filters()) == []


def test_bridge_frontier_entity_type_filter_bites_independent_of_link_type(kb):
    """Forces the atypical combination -- a CLASS entity linked via 'mentions' -- to
    prove the entity_type predicate excludes it on its own, not as a side effect of
    link_type. entity_links has no CHECK tying the two; in practice they correlate 100%
    (mentions -> instance, belongs_to -> class) but that is data, not a guarantee."""
    cls = kb.upsert_entity_class("SomeClass")
    m = _note(kb, "m", "x")
    kb.set_embedding(m.id, _grad_vec(1.0), embedded_hash="h")
    kb.link_entity(m.id, cls.node.id, MENTIONS, weight=0.9)
    assert kb.bridge_frontier([cls.node.id], _grad_vec(1.0), fanout=10,
                              **_filters()) == []


def test_bridge_frontier_link_type_filter_bites_independent_of_entity_type(kb):
    """The mirror image: an INSTANCE entity linked via 'belongs_to' proves the
    link_type predicate excludes it on its own."""
    inst = kb.upsert_entity("InstanceButBelongsTo")
    m = _note(kb, "m", "x")
    kb.set_embedding(m.id, _grad_vec(1.0), embedded_hash="h")
    kb.link_entity(m.id, inst.node.id, BELONGS_TO)
    assert kb.bridge_frontier([inst.node.id], _grad_vec(1.0), fanout=10,
                              **_filters()) == []


def test_bridge_frontier_honours_the_shared_filter_block(kb):
    ent = kb.upsert_entity("EntityA", categories=["tool"])
    keep = _note(kb, "keep", "x")
    drop = _note(kb, "drop", "x")
    kb.update(keep.id, NodePatch(doc_kind="meeting notes"))
    kb.update(drop.id, NodePatch(doc_kind="design spec"))
    kb.set_embedding(keep.id, _grad_vec(1.0), embedded_hash="h1")
    kb.set_embedding(drop.id, _grad_vec(0.99), embedded_hash="h2")
    kb.link_entity(keep.id, ent.node.id, MENTIONS, weight=0.9)
    kb.link_entity(drop.id, ent.node.id, MENTIONS, weight=0.9)

    rows = kb.bridge_frontier([ent.node.id], _grad_vec(1.0), fanout=10,
                              **_filters(kind="meeting notes"))
    assert [r["memory_id"] for r in rows] == [keep.id]


def test_bridge_frontier_on_empty_input_is_empty(kb):
    assert kb.bridge_frontier([], _grad_vec(1.0), fanout=5, **_filters()) == []


def test_bridge_frontier_honours_a_supplied_conn(kb):
    ent = kb.upsert_entity("EntityA", categories=["tool"])
    m = _note(kb, "m", "x")
    kb.set_embedding(m.id, _grad_vec(1.0), embedded_hash="h")
    kb.link_entity(m.id, ent.node.id, MENTIONS, weight=0.9)
    with kb.read_snapshot() as conn:
        rows = kb.bridge_frontier([ent.node.id], _grad_vec(1.0), fanout=2,
                                  **_filters(), conn=conn)
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# node_meta
# ---------------------------------------------------------------------------

def _commit_other_note(pool, ext, title):
    """Commit a node on a DIFFERENT connection, so a pinned REPEATABLE READ snapshot
    opened earlier must not see it. Returns the new id. Duplicated from
    test_search_primitives.py per the convention this file's header records.

    `path` gets its own named parameter (`%(p)s`), bound to the same value as `%(e)s`,
    rather than reusing `%(e)s` a third time with a `::ltree` cast -- psycopg maps
    repeated named parameters to ONE server-side placeholder, and Postgres then sees
    that placeholder used as plain text (external_id, slug) AND cast to ltree (path)
    in the same statement, which raises `AmbiguousParameter: inconsistent types
    deduced for parameter $1` (verified live against the test DB, matching
    test_search_primitives.py's `_commit_other_note`). Separate names -> separate
    placeholders -> each independently typed from its own column context."""
    with pool.connection() as other:
        row = other.execute(
            "INSERT INTO nodes (id, source, external_id, type, slug, path, title, "
            "content, content_hash) VALUES (gen_random_uuid(), 'agent', %(e)s, 'note', "
            "%(e)s, %(p)s::ltree, %(t)s, %(c)s, %(h)s) RETURNING id",
            {"e": ext, "p": ext, "t": title, "c": "body", "h": ext * 8}).fetchone()
    return row["id"]


class _ExplodingConn:
    """A connection that fails loudly if it is used at all. The only way to OBSERVE that
    node_meta([]) short-circuits before a round trip: with conn=None an unguarded
    implementation would still return {} and the test would pass vacuously."""

    def execute(self, *args, **kwargs):
        raise AssertionError("node_meta issued a query for an empty id list")


def test_node_meta_round_trips_title_doc_kind_and_event_at(kb):
    """The three fields the frontier renderer needs, and nothing else:
    "[i] title . doc_kind . sim=... . via=... . event_at|undated"."""
    when = datetime(2026, 3, 4, 17, 30, tzinfo=timezone.utc)
    n = _note(kb, "dated", "body text", title="Kickoff", event_at=when)
    kb.update(n.id, NodePatch(doc_kind="meeting notes"))
    assert kb.node_meta([n.id]) == {
        n.id: {"title": "Kickoff", "doc_kind": "meeting notes", "event_at": when}}


def test_node_meta_absent_id_is_absent_not_a_row_of_nones(kb):
    """The distinction the caller depends on. A row that EXISTS with no title renders as
    "[3] (untitled) . ..."; an id with NO row is a concurrent delete or a stale pool member
    and must be droppable. Collapsing the two -- pre-seeding every requested id with None --
    would make a vanished memory render as an untitled one at a live index the judge can
    then admit."""
    untitled = _note(kb, "untitled", "a body and no title")
    missing = uuid4()
    got = kb.node_meta([untitled.id, missing])
    assert set(got) == {untitled.id}
    assert missing not in got
    assert got[untitled.id] == {"title": None, "doc_kind": None, "event_at": None}


def test_node_meta_applies_no_filters_of_its_own(kb):
    """Deliberately UNLIKE every other read primitive in this file. Every id reaching
    node_meta already passed its own arm's SEARCH_FILTERS, so re-filtering here could only
    ever drop a row the frontier has already committed to showing -- leaving a hole at an
    index the judge is about to reference. The contrast is observable on a type='entity'
    node, which SEARCH_FILTERS' scaffolding exclusion drops and node_meta returns.

    THE ENTITY IS EMBEDDED, and that is the whole point. similarity_for's own
    predicate is `embedding IS NOT NULL`, so an UNEMBEDDED entity would be omitted
    whatever the filter block did and this contrast would be vacuous -- it would pass
    with the scaffolding exclusion deleted. The embedded plain note beside it is the
    positive control: same call, same filters, and it IS returned."""
    ent = kb.upsert(NodeRecord(source="agent", external_id="ent1", type="entity",
                               content="an entity doc", slug="ent1", title="Widget"))
    kb.set_embedding(ent.id, _vec(0), embedded_hash="hent")
    plain = _note(kb, "plain", "an ordinary note", title="Plain")
    kb.set_embedding(plain.id, _vec(0), embedded_hash="hplain")

    assert kb.node_meta([ent.id])[ent.id]["title"] == "Widget"      # described...
    scored = kb.similarity_for([ent.id, plain.id], _vec(0), **_filters())
    assert plain.id in scored                                       # control
    assert ent.id not in scored                                     # ...but filtered


def test_node_meta_on_empty_input_returns_empty_without_a_round_trip(kb):
    assert kb.node_meta([], conn=_ExplodingConn()) == {}


def test_node_meta_with_conn_reads_only_the_pinned_snapshot(kb, pool):
    """The crawl renders its frontier inside one read_snapshot(); metadata read outside it
    would describe a different DB state than the ids came from. The STRONG shape -- commit
    on another connection mid-snapshot and assert invisibility -- because merely calling
    with conn= passes even if the method ignores it entirely, which is the actual bug."""
    first = _note(kb, "first", "body", title="First")
    with kb.read_snapshot() as conn:
        # A REAL read first. REPEATABLE READ takes its snapshot at the first
        # non-transaction-control statement, so without this line the snapshot would open
        # AFTER the insert below and both assertions would be vacuous.
        assert set(kb.node_meta([first.id], conn=conn)) == {first.id}
        intruder = _commit_other_note(pool, "intruder", "Intruder")
        pinned = kb.node_meta([first.id, intruder], conn=conn)
        unpinned = kb.node_meta([first.id, intruder])
    assert set(pinned) == {first.id}                     # invisible inside the snapshot
    assert set(unpinned) == {first.id, intruder}         # ...and visible outside it
