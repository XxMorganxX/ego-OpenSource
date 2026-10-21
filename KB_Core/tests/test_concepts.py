"""Pure clustering helpers: no DB, no network, no Docker.

`hub_share=None` on several build_graph() calls below disables the hub cap
(tests/test_concept_graph.py owns that behavior) — these fixtures deliberately construct
high-support "hub" entities to drive Leiden/propagation mechanics that predate and are
orthogonal to the cap, which would otherwise strip those hub entities out of the graph
entirely before these tests get to exercise the thing they're actually testing."""
import random

import pytest

from kb_core.concepts import (ENTITY, MEMORY, attach_entities, build_graph,
                              detect_communities, enforce_constraints,
                              match_concepts, mention_edge_weight,
                              resolve_separations)
from kb_core.concepts import _seeded_bipartition


def _two_projects():
    """Two disjoint 4-memory clusters, each glued by 2 shared entities."""
    mention = []
    for m in ("a1", "a2", "a3", "a4"):
        mention += [{"memory_id": m, "entity_id": "eA1", "weight": 0.9, "support": 4},
                    {"memory_id": m, "entity_id": "eA2", "weight": 0.9, "support": 4}]
    for m in ("b1", "b2", "b3", "b4"):
        mention += [{"memory_id": m, "entity_id": "eB1", "weight": 0.9, "support": 4},
                    {"memory_id": m, "entity_id": "eB2", "weight": 0.9, "support": 4}]
    return mention


def test_mention_edge_weight_damps_ubiquitous_entities():
    assert mention_edge_weight(0.8, 1) == pytest.approx(0.8)      # support 1: full confidence
    assert mention_edge_weight(0.8, 3) == pytest.approx(0.4)      # log2(4) = 2
    assert mention_edge_weight(None, 1) == pytest.approx(0.5)     # missing confidence -> 0.5
    assert mention_edge_weight(0.8, 15) == pytest.approx(0.2)     # log2(16) = 4


def test_build_graph_decays_mention_edges_by_age_factor():
    rows = [{"memory_id": "m", "entity_id": "e", "weight": 1.0, "support": 1,
             "age_factor": 0.5}]
    g = build_graph(rows, [], hub_share=None)
    assert abs(g.es[0]["weight"] - mention_edge_weight(1.0, 1) * 0.5) < 1e-9
    rows[0].pop("age_factor")                       # hand-built rows: no decay
    g = build_graph(rows, [], hub_share=None)
    assert abs(g.es[0]["weight"] - mention_edge_weight(1.0, 1)) < 1e-9


def test_attach_entities_decays_capped_edges():
    """The hub-cap roster path must decay the same way as the graph path: a hub whose
    pull on a community comes only from an old mention loses that roster slot."""
    g = build_graph([], [{"src": "m1", "dst": "m2", "sim": 1.0}])
    comms = [
        {"key": "c0", "level": 0, "parent_key": None, "members": [("m1", MEMORY, 1.0)]},
        {"key": "c1", "level": 0, "parent_key": None, "members": [("m2", MEMORY, 1.0)]},
    ]
    capped = [
        {"memory_id": "m1", "entity_id": "hub", "weight": 1.0, "support": 1,
         "age_factor": 1.0},
        {"memory_id": "m2", "entity_id": "hub", "weight": 1.0, "support": 1,
         "age_factor": 0.1},
    ]
    out = attach_entities(g, comms, min_share=0.25, capped_edges=capped)
    rosters = [{m[0] for m in c["members"]} for c in out]
    assert "hub" in rosters[0]      # fresh mention: share ~0.91 >= 0.25
    assert "hub" not in rosters[1]  # decayed mention: share ~0.09 < 0.25


def test_build_graph_vertices_kinds_and_knn_dedup():
    g = build_graph(
        # support 3 -> mention weight 0.45, so 0.9 below can only be the knn edge
        [{"memory_id": "m1", "entity_id": "e1", "weight": 0.9, "support": 3}],
        [{"src": "m1", "dst": "m2", "sim": 0.7}, {"src": "m2", "dst": "m1", "sim": 0.9}])
    assert g.vcount() == 3 and g.ecount() == 2                    # knn pair deduped
    kinds = dict(zip(g.vs["name"], g.vs["kind"]))
    assert kinds == {"m1": MEMORY, "e1": ENTITY, "m2": MEMORY}
    knn_edge = [e for e in g.es if e["weight"] == pytest.approx(0.9)]
    assert len(knn_edge) == 1                                     # max sim of the pair kept


def test_build_graph_is_canonical_regardless_of_row_order():
    """`seed` pins Leiden's RNG but NOT the vertex->index mapping, and Leiden's local
    moving visits vertices by index — so identical data arriving in a different order
    used to yield a different partition. Neither concept_mention_edges nor
    memory_knn_edges has an ORDER BY, and Postgres guarantees no order without one, so
    a plan change or VACUUM was enough to churn every concept id. Measured on a real
    corpus before this was fixed: shuffling the same rows moved the community count by
    one — the same magnitude as the knobs we tune deliberately."""
    mention = _two_projects()
    knn = [{"src": "a1", "dst": "b1", "sim": 0.6}, {"src": "a2", "dst": "a3", "sim": 0.8}]

    plain = build_graph(mention, knn, hub_share=None)
    for seed in (1, 2, 3):
        shuffled_m, shuffled_k = list(mention), list(knn)
        random.Random(seed).shuffle(shuffled_m)
        random.Random(seed + 100).shuffle(shuffled_k)
        other = build_graph(shuffled_m, shuffled_k, hub_share=None)

        assert list(other.vs["name"]) == list(plain.vs["name"])
        assert sorted((other.vs[e.source]["name"], other.vs[e.target]["name"],
                       round(float(e["weight"]), 9)) for e in other.es) == sorted(
                      (plain.vs[e.source]["name"], plain.vs[e.target]["name"],
                       round(float(e["weight"]), 9)) for e in plain.es)
        assert _partition(detect_communities(other, min_memories=3)) == _partition(
            detect_communities(plain, min_memories=3))


def _partition(comms):
    return sorted(tuple(sorted(n for n, kind, _ in c["members"] if kind == MEMORY))
                  for c in comms)


def test_detect_communities_separates_two_projects():
    comms = detect_communities(build_graph(_two_projects(), []), min_memories=3)
    assert len(comms) == 2
    for c in comms:
        mems = {nid for nid, kind, _ in c["members"] if kind == MEMORY}
        ents = {nid for nid, kind, _ in c["members"] if kind == ENTITY}
        assert len(mems) == 4 and len(ents) == 2                  # entities travel with memories
        assert mems <= {"a1", "a2", "a3", "a4"} or mems <= {"b1", "b2", "b3", "b4"}
        assert all(0.0 <= s <= 1.0 for _, _, s in c["members"])
        assert c["level"] == 0 and c["parent_key"] is None


def test_min_memories_drops_noise_communities():
    tiny = [{"memory_id": "x1", "entity_id": "eX", "weight": 0.9, "support": 1}]
    comms = detect_communities(build_graph(_two_projects() + tiny, []), min_memories=3)
    assert len(comms) == 2                                        # 1-memory cluster dropped


def _bridged_subcliques():
    """Two 3-memory subcliques, each glued by its own entity, plus one 'shared' entity
    spanning all 6 memories (support=6, so its mention_edge_weight is heavily damped).
    That shared entity is the bridge: embedded in a larger graph (see _fillers) it's
    enough to fuse the two subcliques into a single level-0 community, but the
    induced-subgraph re-cluster -- with a much smaller total edge weight -- peels them
    back apart."""
    mention = []
    all_mems = []
    for s in range(2):
        mems = [f"t{s}_{i}" for i in range(3)]
        all_mems += mems
        for m in mems:
            mention.append({"memory_id": m, "entity_id": f"t{s}_local", "weight": 0.9, "support": 3})
    for m in all_mems:
        mention.append({"memory_id": m, "entity_id": "shared", "weight": 0.9, "support": 6})
    return mention


def _fillers(n=3):
    """`n` unrelated 2-memory pairs. Each falls below min_memories=3 so none ever
    surfaces as a community on its own -- they exist purely to inflate the whole graph's
    total edge weight, which is what makes Leiden's top-level pass prefer fusing the two
    subcliques above via the shared entity."""
    mention = []
    for g in range(n):
        for m in (f"f{g}_0", f"f{g}_1"):
            mention.append({"memory_id": m, "entity_id": f"f{g}e1", "weight": 0.9, "support": 2})
            mention.append({"memory_id": m, "entity_id": f"f{g}e2", "weight": 0.9, "support": 2})
    return mention


def test_detect_communities_splits_dense_subcommunity_into_level1_children():
    comms = detect_communities(
        build_graph(_bridged_subcliques() + _fillers(), [], hub_share=None),
        min_memories=3)

    parent_mems = {f"t{s}_{i}" for s in range(2) for i in range(3)}
    parent = next(c for c in comms if c["level"] == 0
                  and {n for n, k, _ in c["members"] if k == MEMORY} == parent_mems)
    assert parent["parent_key"] is None                           # parent keeps full membership

    children = [c for c in comms if c["level"] == 1 and c["parent_key"] == parent["key"]]
    assert len(children) > 1                                      # a single child is just the parent again

    child_mem_sets = [{n for n, k, _ in c["members"] if k == MEMORY} for c in children]
    assert all(s <= parent_mems for s in child_mem_sets)           # children are sub-partitions of the parent
    for i, s1 in enumerate(child_mem_sets):
        for s2 in child_mem_sets[i + 1:]:
            assert s1.isdisjoint(s2)
    assert set.union(*child_mem_sets) == parent_mems               # children partition the parent cleanly


def test_detect_communities_leaves_uniform_dense_clique_unsplit():
    """A single 7-memory clique glued uniformly by two shared entities has no internal
    sub-structure for Leiden to find: re-clustering its induced subgraph reproduces the
    same single blob, so no level=1 children are ever emitted."""
    mems = [f"m{i}" for i in range(7)]
    mention = []
    for m in mems:
        mention.append({"memory_id": m, "entity_id": "e1", "weight": 0.9, "support": 7})
        mention.append({"memory_id": m, "entity_id": "e2", "weight": 0.9, "support": 7})

    comms = detect_communities(build_graph(mention, [], hub_share=None), min_memories=3)

    assert len(comms) == 1
    assert comms[0]["level"] == 0 and comms[0]["parent_key"] is None
    assert {n for n, k, _ in comms[0]["members"] if k == MEMORY} == set(mems)
    assert not any(c["level"] == 1 for c in comms)                 # no split -- one dense clique, no sub-groups


def test_split_cohesion_gate_suppresses_all_weak_children():
    """With the bar above every child's cohesion (the bridged subcliques measure
    ~0.71 and ~0.58 — cross-weight via the shared entity), no children are emitted:
    a split that only produces loose fragments is no split at all."""
    comms = detect_communities(
        build_graph(_bridged_subcliques() + _fillers(), [], hub_share=None),
        min_memories=3, split_cohesion=0.9)
    assert not any(c["level"] == 1 for c in comms)


def test_split_cohesion_gate_drops_weak_child_keeps_strong():
    """A weak child dies alone — its crisp sibling still surfaces. At 0.65 only
    the subclique that captured the shared entity (~0.71) clears the bar; the
    other (~0.58) is dropped without taking the survivor down with it."""
    comms = detect_communities(
        build_graph(_bridged_subcliques() + _fillers(), [], hub_share=None),
        min_memories=3, split_cohesion=0.65)
    children = [c for c in comms if c["level"] == 1]
    assert len(children) == 1
    child_mems = {n for n, k, _ in children[0]["members"] if k == MEMORY}
    assert child_mems == {"t0_0", "t0_1", "t0_2"}


def test_empty_graph_yields_no_communities():
    assert detect_communities(build_graph([], [])) == []


def test_match_concepts_carries_ids_one_to_one():
    comms = detect_communities(build_graph(_two_projects(), []), min_memories=3)
    a = next(c for c in comms
             if "a1" in {n for n, k, _ in c["members"] if k == MEMORY})
    previous = [{"id": "prev-A", "memory_ids": ["a1", "a2", "a3", "a4"]},
                {"id": "prev-Z", "memory_ids": ["z1", "z2", "z3"]}]
    matched = match_concepts(comms, previous, min_overlap=0.5)
    assert matched == {a["key"]: "prev-A"}                        # B unmatched, Z retires


def _bridged():
    """One dense community glued by a shared entity 'eShared' spanning two 3-memory
    sub-groups (a*, b*) — the Vandelay/Acme shape. eShared's support is high (10)
    so its own damped weight is *weaker* than the tight local eA/eB edges (support=2) --
    the local pull alone isn't enough to glue a*/b* at the top level, but it's exactly
    what lets the induced-subgraph re-cluster in _split_cannot peel them cleanly back
    apart once escalated. Six _fillers() pairs (same mechanism as _bridged_subcliques +
    _fillers) inflate the graph's total edge weight so Leiden's top-level pass still
    prefers fusing the two sub-groups through eShared into a single community at the
    default resolution."""
    mention = []
    for m in ("a1", "a2", "a3"):
        mention += [{"memory_id": m, "entity_id": "eA", "weight": 0.9, "support": 2},
                    {"memory_id": m, "entity_id": "eShared", "weight": 0.9, "support": 10}]
    for m in ("b1", "b2", "b3"):
        mention += [{"memory_id": m, "entity_id": "eB", "weight": 0.9, "support": 2},
                    {"memory_id": m, "entity_id": "eShared", "weight": 0.9, "support": 10}]
    return mention + _fillers(6)


def _hub_heavy():
    """Original (pre-retune) `_bridged()` shape: two 3-memory groups on eA/eB at
    support=3 (weight 0.45), glued by 'eShared' at support=2 (weight 0.5678) -- the
    shared hub's per-edge weight *exceeds* the locals'. Used to pin the degree-
    normalized propagation fix in `_seeded_bipartition`: with a hub heavier than the
    local anchors, raw-weight propagation lets the hub, once absorbed into one side,
    drag the opposite side's memories across too."""
    mention = []
    for m in ("a1", "a2", "a3"):
        mention += [{"memory_id": m, "entity_id": "eA", "weight": 0.9, "support": 3},
                    {"memory_id": m, "entity_id": "eShared", "weight": 0.9, "support": 2}]
    for m in ("b1", "b2", "b3"):
        mention += [{"memory_id": m, "entity_id": "eB", "weight": 0.9, "support": 3},
                    {"memory_id": m, "entity_id": "eShared", "weight": 0.9, "support": 2}]
    return mention


def test_cannot_link_splits_a_glued_community():
    g = build_graph(_bridged(), [], hub_share=None)
    # bar > 1.0 disables the sub-pass: this test wants the glued single community
    comms = detect_communities(g, min_memories=3, split_cohesion=2.0)
    assert len(comms) == 1                              # glued into one without the rule
    out = enforce_constraints(g, comms, [
        {"kind": "cannot_link", "left_id": "eA", "right_id": "eB"}], min_memories=3)
    homes = [{n for n, k, _ in c["members"]} for c in out]
    assert len(out) == 2
    assert not any({"eA", "eB"} <= h for h in homes)    # anchors separated
    a_home = next(h for h in homes if "eA" in h)
    assert {"a1", "a2", "a3"} <= a_home                 # memories follow their anchor


def test_must_link_unions_two_communities():
    g = build_graph(_two_projects(), [])
    comms = detect_communities(g, min_memories=3)
    assert len(comms) == 2
    out = enforce_constraints(g, comms, [
        {"kind": "must_link", "left_id": "eA1", "right_id": "eB1"}], min_memories=3)
    assert len(out) == 1
    ids = {n for n, k, _ in out[0]["members"]}
    assert {"eA1", "eB1", "a1", "b1"} <= ids


def test_constraints_with_absent_anchors_are_inert():
    g = build_graph(_two_projects(), [])
    comms = detect_communities(g, min_memories=3)
    out = enforce_constraints(g, comms, [
        {"kind": "cannot_link", "left_id": "ghost1", "right_id": "ghost2"},
        {"kind": "must_link", "left_id": "eA1", "right_id": "ghostX"}], min_memories=3)
    assert len(out) == 2                                # nothing changed


def test_seeded_bipartition_splits_around_anchors():
    g = build_graph(_bridged(), [], hub_share=None)
    va = g.vs.find(name="eA").index
    vb = g.vs.find(name="eB").index
    side_a, side_b = _seeded_bipartition(g, list(range(g.vcount())), va, vb)
    names_a = {g.vs[v]["name"] for v in side_a}
    names_b = {g.vs[v]["name"] for v in side_b}
    assert "eA" in names_a and "eB" in names_b
    assert {"a1", "a2", "a3"} <= names_a and {"b1", "b2", "b3"} <= names_b


def test_seeded_bipartition_resists_hub_domination():
    """Pins the degree-normalized propagation: with a hub HEAVIER than the local
    anchors, raw-weight propagation drags one side across (regression seen in
    development); normalization must keep each group with its own anchor."""
    g = build_graph(_hub_heavy(), [])
    va = g.vs.find(name="eA").index
    vb = g.vs.find(name="eB").index
    side_a, side_b = _seeded_bipartition(g, list(range(g.vcount())), va, vb)
    names_a = {g.vs[v]["name"] for v in side_a}
    names_b = {g.vs[v]["name"] for v in side_b}
    assert {"a1", "a2", "a3"} <= names_a and {"b1", "b2", "b3"} <= names_b


def _shared_person():
    """_two_projects plus one person mentioned on both sides (1 a-memory, 2 b-)."""
    mention = _two_projects()
    for m in ("a1", "b1", "b2"):
        mention.append({"memory_id": m, "entity_id": "eShared",
                        "weight": 0.9, "support": 3})
    return mention


def test_attach_entities_grants_cross_community_membership():
    g = build_graph(_shared_person(), [])
    comms = detect_communities(g, min_memories=3)
    assert len(comms) == 2
    homes_before = sum(1 for c in comms
                       if any(n == "eShared" for n, _, _ in c["members"]))
    assert homes_before == 1                         # the partition picks one side
    out = attach_entities(g, comms)
    for c in out:
        m = next((m for m in c["members"] if m[0] == "eShared"), None)
        assert m is not None and m[1] == ENTITY      # now a member of both
    shares = sorted(m[2] for c in out for m in c["members"] if m[0] == "eShared")
    assert 0 < shares[0] <= shares[1] <= 1           # scores are attachment shares


def test_attach_entities_respects_threshold_and_exclusions():
    g = build_graph(_shared_person(), [])
    comms = detect_communities(g, min_memories=3)

    def homes(result):
        return sum(1 for c in result
                   if any(n == "eShared" for n, _, _ in c["members"]))

    assert homes(attach_entities(g, comms, min_share=0.9)) == 1   # bar too high
    assert homes(attach_entities(g, comms, exclude={"eShared"})) == 1


def _capped_hub(memories=("a1", "a2", "a3", "a4", "b1", "b2", "b3", "b4")):
    """Mention edges for one entity over `memories` — support high enough that the hub
    cap strips it from the clustering graph (8 memories -> cap 4)."""
    return [{"memory_id": m, "entity_id": "eHub", "weight": 0.9,
             "support": len(memories)} for m in memories]


def test_attach_entities_readmits_hub_capped_entity():
    """The cap is a CLUSTERING exclusion, not a roster exclusion: a hub entity must not
    influence the partition, but must still appear on the concepts its memories formed.
    Without this, the most connective entity in a KB belongs to no concept at all."""
    mention = _two_projects() + _capped_hub()
    g = build_graph(mention, [])
    assert "eHub" not in set(g.vs["name"])           # capped out of the graph
    comms = detect_communities(g, min_memories=3)
    assert len(comms) == 2
    assert not any(n == "eHub" for c in comms for n, _, _ in c["members"])
    assert not any(n == "eHub" for c in attach_entities(g, comms)
                   for n, _, _ in c["members"])      # default unchanged

    out = attach_entities(g, comms, capped_edges=_capped_hub())

    for c in out:
        m = next((m for m in c["members"] if m[0] == "eHub"), None)
        assert m is not None and m[1] == ENTITY      # on both concepts
        assert m[2] == pytest.approx(0.5)            # 4 of its 8 memories per side


def test_attach_entities_gates_capped_entities_like_any_other():
    """Re-admission runs through the same share gate and cannot_link exclusion — a
    diffuse hub earns no home, so re-attaching cannot smuggle a stopword everywhere."""
    lopsided = _capped_hub(("a1", "a2", "a3", "a4", "b1"))   # 4/5 one side, 1/5 the other
    g = build_graph(_two_projects() + lopsided, [])
    comms = detect_communities(g, min_memories=3)

    def homes(result):
        return sum(1 for c in result if any(n == "eHub" for n, _, _ in c["members"]))

    assert homes(attach_entities(g, comms, capped_edges=lopsided)) == 1      # 0.2 < 0.25
    assert homes(attach_entities(g, comms, capped_edges=lopsided,
                                 min_share=0.9)) == 0                        # bar too high
    assert homes(attach_entities(g, comms, capped_edges=lopsided,
                                 exclude={"eHub"})) == 0                     # anchor excluded


def test_resolve_separations_prefers_exclusive_memory_anchors():
    rules = [{"id": 1, "note": "unrelated",
              "left_members": ["shared", "m1", "m2"],
              "right_members": ["shared", "n1"]}]
    assert resolve_separations(rules) == [
        {"kind": "cannot_link", "left_id": "m1", "right_id": "n1",
         "note": "unrelated"}]


def test_resolve_separations_nested_child_falls_back_to_top_member():
    # the un-nesting case: the child is fully inside the parent, so it has no
    # exclusive member — it anchors on its own strongest member instead
    rules = [{"id": 1, "note": None,
              "left_members": ["p1", "c1", "c2"],       # parent, strongest-first
              "right_members": ["c1", "c2"]}]           # child ⊂ parent
    assert resolve_separations(rules) == [
        {"kind": "cannot_link", "left_id": "p1", "right_id": "c1", "note": None}]


def test_resolve_separations_inert_on_identical_or_empty_sides():
    assert resolve_separations([
        {"id": 1, "note": None, "left_members": ["m"], "right_members": ["m"]},
        {"id": 2, "note": None, "left_members": [], "right_members": ["m"]},
        {"id": 3, "note": None, "left_members": ["m"], "right_members": []}]) == []


def test_split_drops_twin_child_and_keeps_parts_top_level():
    g = build_graph(_bridged(), [], hub_share=None)
    # bar > 1.0 disables the sub-pass: the child under test is hand-built below
    comms = detect_communities(g, min_memories=3, split_cohesion=2.0)
    assert len(comms) == 1
    parent_key = comms[0]["key"]
    b_side = [m for m in comms[0]["members"]
              if m[0] in {"b1", "b2", "b3", "eB", "eShared"}]
    child = {"key": f"{parent_key}.0", "level": 1, "parent_key": parent_key,
             "members": b_side}
    out = enforce_constraints(g, comms + [child], [
        {"kind": "cannot_link", "left_id": "eA", "right_id": "eB"}], min_memories=3)
    # the child's memory set equals the b-side split part, so the twin is dropped
    # and both parts stand as siblings at the parent's level
    assert len(out) == 2
    assert all(c["level"] == 0 and c["parent_key"] is None for c in out)


def test_fixup_drops_near_twin_children_but_keeps_sub_clusters():
    from kb_core.concepts import _fix_children_of_split
    part = {"key": "c0.x0", "level": 0, "parent_key": None,
            "members": [(f"m{i}", MEMORY, 1.0) for i in range(10)]}
    near_twin = {"key": "c0.0", "level": 1, "parent_key": "c0",
                 "members": [(f"m{i}", MEMORY, 1.0) for i in range(9)]}   # J = 0.9
    sub = {"key": "c0.1", "level": 1, "parent_key": "c0",
           "members": [(f"m{i}", MEMORY, 1.0) for i in range(5)]}         # J = 0.5
    out = _fix_children_of_split([part, near_twin, sub], {"c0": [part]})
    assert [c["key"] for c in out] == ["c0.x0", "c0.1"]  # near-twin dropped
    assert out[1]["parent_key"] == "c0.x0"               # sub-cluster re-parented


def test_split_reparents_smaller_children_to_their_part():
    g = build_graph(_bridged(), [], hub_share=None)
    comms = detect_communities(g, min_memories=3)
    parent_key = comms[0]["key"]
    child = {"key": f"{parent_key}.0", "level": 1, "parent_key": parent_key,
             "members": [("b1", MEMORY, 1.0), ("b2", MEMORY, 1.0)]}
    out = enforce_constraints(g, comms + [child], [
        {"kind": "cannot_link", "left_id": "eA", "right_id": "eB"}],
        min_memories=2)
    kid = next(c for c in out if c["level"] == 1)
    b_part = next(c for c in out if c["level"] == 0
                  and any(m[0] == "b1" for m in c["members"]))
    assert kid["parent_key"] == b_part["key"]


def test_cannot_link_falls_back_to_bipartition_when_escalation_disabled():
    """Drives the _split_cannot -> _seeded_bipartition integration seam (vertex ids,
    anchors, and min_memories filtering threaded through the fallback branch)."""
    g = build_graph(_bridged(), [], hub_share=None)
    comms = detect_communities(g, min_memories=3)
    out = enforce_constraints(g, comms, [
        {"kind": "cannot_link", "left_id": "eA", "right_id": "eB"}],
        min_memories=3, max_escalations=0)
    homes = [{n for n, k, _ in c["members"]} for c in out]
    assert not any({"eA", "eB"} <= h for h in homes)
    a_home = next(h for h in homes if "eA" in h)
    assert {"a1", "a2", "a3"} <= a_home


def test_build_graph_prefers_decayed_knn_weight_falls_back_to_sim():
    g = build_graph([], [{"src": "a", "dst": "b", "sim": 1.0, "weight": 0.25}])
    assert abs(g.es[0]["weight"] - 0.25) < 1e-9
    g = build_graph([], [{"src": "a", "dst": "b", "sim": 0.9}])   # no weight key
    assert abs(g.es[0]["weight"] - 0.9) < 1e-9
