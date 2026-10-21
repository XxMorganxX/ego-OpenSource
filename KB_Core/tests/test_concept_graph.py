# tests/test_concept_graph.py
"""KB reads that feed the consolidation graph."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from kb_core.concepts import (build_graph, detect_communities, hub_capped_entities,
                              mention_edge_weight)
from kb_core.domain import NodeRecord
from kb_core.entities import BELONGS_TO, MENTIONS
from kb_core.knowledge_base import KnowledgeBase
from kb_core.policy import DecayRule, Match, Policy
from kb_core.registry import default_registry
from tests.legacy_policy import LEGACY_EMAIL_RULE


def _mem(eid, content="c", parent_path=""):
    return NodeRecord(source="agent", external_id=eid, type="note", content=content,
                      slug=eid, title=eid, parent_path=parent_path)


def _mem_at(eid, *, days_ago=None, tags=(), parent_path="notes"):
    """A memory with a real event_at `days_ago` days back (None = undated)."""
    eat = None if days_ago is None else (
        datetime.now(timezone.utc) - timedelta(days=days_ago))
    return NodeRecord(source="agent", external_id=eid, type="note", content="c",
                      slug=eid, title=eid, event_at=eat, tags=list(tags),
                      parent_path=parent_path)


def _vec(axis):
    """1024-dim one-hot — cosine sim 1.0 with itself, 0.0 with any other axis."""
    v = [0.0] * 1024
    v[axis] = 1.0
    return v


def test_mention_edges_cover_active_instances_only(kb):
    m1, m2 = kb.upsert(_mem("m1")), kb.upsert(_mem("m2"))
    ent = kb.upsert_entity("Ollama", categories=["tool"])
    dead = kb.upsert_entity("OldThing", categories=["tool"])
    kb.link_entity(m1.id, ent.node.id, MENTIONS, weight=0.9)
    kb.link_entity(m2.id, ent.node.id, MENTIONS, weight=0.6)
    kb.link_entity(m1.id, dead.node.id, MENTIONS, weight=0.9)
    kb.demote_instance(dead.node.id)                       # inactive -> excluded
    rows = kb.concept_mention_edges()
    assert {(r["memory_id"], r["entity_id"]) for r in rows} == {
        (m1.id, ent.node.id), (m2.id, ent.node.id)}
    assert all(r["support"] == 2 for r in rows)            # distinct-memory support


def test_mention_edges_carry_age_factor(kb):
    """Undated memory -> exactly 1.0; a year-old email memory -> ~the email rule's floor."""
    m_old = kb.upsert(_mem_at("m_old", days_ago=365, tags=("email",)))
    m_new = kb.upsert(_mem_at("m_new"))
    ent = kb.upsert_entity("Ollama", categories=["tool"])
    kb.link_entity(m_old.id, ent.node.id, MENTIONS, weight=0.9)
    kb.link_entity(m_new.id, ent.node.id, MENTIONS, weight=0.9)
    f = {r["memory_id"]: float(r["age_factor"]) for r in kb.concept_mention_edges()}
    assert f[m_new.id] == 1.0
    assert abs(f[m_old.id] - LEGACY_EMAIL_RULE.floor) < 0.02


def test_mention_edges_exclude_class_links_and_dead_memories(kb, pool):
    m = kb.upsert(_mem("m1"))
    ent = kb.upsert_entity("Ollama", categories=["tool"])
    kb.link_entity(m.id, ent.node.id, MENTIONS, weight=0.9)
    with pool.connection() as conn:                        # a class link must not appear
        cls = conn.execute(
            "SELECT node_id FROM entities WHERE entity_type = 'class' LIMIT 1").fetchone()
        conn.execute(
            "INSERT INTO entity_links (memory_id, entity_id, link_type) VALUES (%s, %s, %s) "
            "ON CONFLICT DO NOTHING", (m.id, cls["node_id"], BELONGS_TO))
    assert len(kb.concept_mention_edges()) == 1
    with pool.connection() as conn:                        # dead memory -> excluded
        conn.execute("UPDATE nodes SET dead_at = now() WHERE id = %s", (m.id,))
    assert kb.concept_mention_edges() == []


def test_memory_knn_edges_link_similar_memories_only(kb):
    a1, a2 = kb.upsert(_mem("a1")), kb.upsert(_mem("a2"))
    b1 = kb.upsert(_mem("b1"))
    kb.set_embedding(a1.id, _vec(0), embedded_hash="h1")
    kb.set_embedding(a2.id, _vec(0), embedded_hash="h2")   # identical -> sim 1.0
    kb.set_embedding(b1.id, _vec(1), embedded_hash="h3")   # orthogonal -> sim 0.0
    rows = kb.memory_knn_edges(k=4, min_sim=0.5)
    pairs = {tuple(sorted((str(r["src"]), str(r["dst"])))) for r in rows}
    assert pairs == {tuple(sorted((str(a1.id), str(a2.id))))}
    assert all(r["sim"] > 0.99 for r in rows)


def test_mention_edges_respect_inherited_folder_exclusion(kb):
    m = kb.upsert(_mem("m1", parent_path="todos"))
    ent = kb.upsert_entity("Ollama", categories=["tool"])
    kb.link_entity(m.id, ent.node.id, MENTIONS, weight=0.9)
    assert len(kb.concept_mention_edges()) == 1
    kb.set_analysis_excluded(kb.get_by_path("todos").id, True)   # flag the FOLDER only
    assert kb.concept_mention_edges() == []                      # child excluded by inheritance


def test_memory_knn_edges_respect_inherited_folder_exclusion(kb):
    a1 = kb.upsert(_mem("a1"))
    a2 = kb.upsert(_mem("a2", parent_path="todos"))
    kb.set_embedding(a1.id, _vec(0), embedded_hash="h1")
    kb.set_embedding(a2.id, _vec(0), embedded_hash="h2")         # identical -> sim 1.0
    assert len(kb.memory_knn_edges(k=4, min_sim=0.5)) == 2       # one pair, both directions
    kb.set_analysis_excluded(kb.get_by_path("todos").id, True)   # flag the FOLDER only
    assert kb.memory_knn_edges(k=4, min_sim=0.5) == []


def test_memory_knn_edges_drop_entity_ineligible_memories(kb):
    a1, a2 = kb.upsert(_mem("a1")), kb.upsert(_mem("a2"))
    kb.set_embedding(a1.id, _vec(0), embedded_hash="h1")
    kb.set_embedding(a2.id, _vec(0), embedded_hash="h2")
    assert len(kb.memory_knn_edges(k=4, min_sim=0.5)) == 2
    kb.set_entity_eligible(a2.id, False, reason="empty template stub")
    assert kb.memory_knn_edges(k=4, min_sim=0.5) == []           # noise emits no similarity edges


def test_concept_member_view_shapes_prompt_inputs(kb):
    nested = NodeRecord(source="agent", external_id="m1", type="note", slug="m1", title="m1",
                        content="Working on the entity denoising gate today",
                        parent_path="projects.kb_core")
    m = kb.upsert(nested)
    ent = kb.upsert_entity("Ollama", categories=["tool"])
    view = kb.concept_member_view([str(m.id)], [str(ent.node.id)])
    assert view["entities"] == [("Ollama", "tool")]
    (title, snippet, folder), = view["memories"]
    assert title == "m1" and "denoising" in snippet
    assert folder == "projects › kb_core"      # filed-under breadcrumb feeds the labeler


def test_concept_member_view_preserves_caller_order(kb):
    """Order is a caller-supplied ranking (strongest-first), not DB row order — the view
    must re-walk the caller's id lists rather than return whatever order Postgres picks.
    Insert three of each in one order, then query in a scrambled order (with a nonexistent
    id spliced into each list) and assert the output matches the query order exactly."""
    m1 = kb.upsert(_mem("m1", content="alpha memory about caching"))
    m2 = kb.upsert(_mem("m2", content="beta memory about routing"))
    m3 = kb.upsert(_mem("m3", content="gamma memory about indexing"))
    e1 = kb.upsert_entity("Alpha", categories=["tool"])
    e2 = kb.upsert_entity("Beta", categories=["language"])
    e3 = kb.upsert_entity("Gamma", categories=["concept"])

    missing_mem = str(uuid.uuid4())
    missing_ent = str(uuid.uuid4())

    # Scrambled, caller-chosen order (not insertion order, not reverse-insertion order
    # either) with a nonexistent id spliced into the middle of each list.
    mem_ids = [str(m3.id), missing_mem, str(m1.id), str(m2.id)]
    ent_ids = [str(e3.node.id), missing_ent, str(e1.node.id), str(e2.node.id)]

    view = kb.concept_member_view(mem_ids, ent_ids)

    assert view["entities"] == [
        ("Gamma", "concept"), ("Alpha", "tool"), ("Beta", "language")]
    assert view["memories"] == [
        ("m3", "gamma memory about indexing", ""),
        ("m1", "alpha memory about caching", ""),
        ("m2", "beta memory about routing", ""),
    ]


def test_entity_coherence_scores_entities_against_the_corpus_baseline(kb):
    """Coherence = how alike an entity's own memories are; baseline = how alike any two
    memories are. The ratio is what the hub gate judges, so both must come from the same
    memory population the kNN edges use."""
    a1, a2, b1 = kb.upsert(_mem("a1")), kb.upsert(_mem("a2")), kb.upsert(_mem("b1"))
    for node, axis in ((a1, 0), (a2, 0), (b1, 1)):
        kb.set_embedding(node.id, _vec(axis), embedded_hash=f"h{node.slug}")
    tight = kb.upsert_entity("Tight", categories=["tool"])   # both axis-0 memories
    loose = kb.upsert_entity("Loose", categories=["tool"])   # one of each -> orthogonal
    kb.link_entity(a1.id, tight.node.id, MENTIONS, weight=0.9)
    kb.link_entity(a2.id, tight.node.id, MENTIONS, weight=0.9)
    kb.link_entity(a1.id, loose.node.id, MENTIONS, weight=0.9)
    kb.link_entity(b1.id, loose.node.id, MENTIONS, weight=0.9)

    out = kb.entity_coherence()

    assert out["coherence"][str(tight.node.id)] == pytest.approx(1.0, abs=1e-6)
    assert out["coherence"][str(loose.node.id)] == pytest.approx(0.0, abs=1e-6)
    # all three memory pairs: (a1,a2)=1.0, (a1,b1)=0, (a2,b1)=0
    assert out["baseline"] == pytest.approx(1 / 3, abs=1e-6)


def test_entity_coherence_skips_entities_with_nothing_to_measure(kb):
    """One mention = no pair = no score. Absent, not zero — a zero would read as
    'measured and uninformative' and get the entity cut."""
    m = kb.upsert(_mem("m1"))
    kb.set_embedding(m.id, _vec(0), embedded_hash="h1")
    solo = kb.upsert_entity("Solo", categories=["tool"])
    kb.link_entity(m.id, solo.node.id, MENTIONS, weight=0.9)
    assert str(solo.node.id) not in kb.entity_coherence()["coherence"]


def _mention(m, e, support):
    return {"memory_id": m, "entity_id": e, "weight": 0.9, "support": support}


def test_hub_cap_drops_ubiquitous_entity():
    # 10 memories; entity "hub" mentioned in 9 (share 0.9), entity "niche" in 2.
    edges = [_mention(f"m{i}", "hub", 9) for i in range(9)]
    edges += [_mention("m0", "niche", 2), _mention("m1", "niche", 2)]
    g = build_graph(edges, [], hub_share=0.5)
    assert "hub" not in set(g.vs["name"])
    assert "niche" in set(g.vs["name"])


def test_hub_cap_none_disables():
    edges = [_mention(f"m{i}", "hub", 9) for i in range(9)]
    g = build_graph(edges, [], hub_share=None)
    assert "hub" in set(g.vs["name"])


def test_hub_cap_small_graphs_untouched():
    # 3 memories: even a 2/3-share entity survives (cap floor keeps tiny corpora intact)
    edges = [_mention("m0", "e", 2), _mention("m1", "e", 2), _mention("m2", "x", 1)]
    g = build_graph(edges, [], hub_share=0.10)
    assert "e" in set(g.vs["name"])


def _frequency_trap():
    """The case frequency gets backwards: `hub` is mentioned far more often but its
    memories genuinely cohere; `stopword` is mentioned less but its memories are no more
    alike than two drawn at random. Measured shape — a specific, frequently mentioned
    project (lift > 1.5) was cut while a ubiquitous personal name (lift < 1) survived."""
    edges = [_mention(f"m{i}", "hub", 20) for i in range(20)]
    edges += [_mention(f"m{i}", "stopword", 8) for i in range(8)]
    return edges


def test_coherence_gate_drops_the_uninformative_not_the_merely_frequent():
    edges = _frequency_trap()
    # frequency alone (cap = max(4, 0.10*20) = 4) condemns BOTH — including the good one
    assert hub_capped_entities(edges, 0.10) == {"hub", "stopword"}
    # coherence judges what the cap was only ever proxying for
    hubs = hub_capped_entities(edges, 0.10, coherence={"hub": 0.63, "stopword": 0.38},
                               baseline=0.40)
    assert hubs == {"stopword"}


def test_coherence_gate_threshold_is_a_lift_over_the_corpus_baseline():
    edges = _frequency_trap()
    coherence = {"hub": 0.63, "stopword": 0.38}
    # baseline scales with the corpus: the same absolute scores judged against a more
    # alike corpus condemn both, so the gate needs no per-corpus retuning
    assert hub_capped_entities(edges, 0.10, coherence=coherence, baseline=0.70) == {
        "hub", "stopword"}
    # demanding a clear margin over baseline, not merely beating it
    assert hub_capped_entities(edges, 0.10, coherence=coherence, baseline=0.40,
                               min_lift=1.6) == {"hub", "stopword"}


def test_coherence_gate_will_not_cut_on_a_single_sample():
    """An entity mentioned twice has exactly ONE pair, so its 'mean' coherence is a
    single cosine — far too noisy to condemn on. Measured: two of the three entities the
    gate first cut were support-2, judged on one sample each. It also binds only that one
    pair, so keeping it costs nearly nothing. `min_support` is the recurrence half of the
    house gating pattern: coherence decides, but only once there is enough of it."""
    edges = [_mention("m0", "thin", 2), _mention("m1", "thin", 2)]
    edges += [_mention(f"m{i}", "thick", 6) for i in range(6)]
    coherence = {"thin": 0.10, "thick": 0.10}          # both look terrible
    hubs = hub_capped_entities(edges, 0.10, coherence=coherence, baseline=0.40)
    assert hubs == {"thick"}                            # only the one we can trust
    # lowering the bar lets the single-sample judgement through
    assert hub_capped_entities(edges, 0.10, coherence=coherence, baseline=0.40,
                               min_support=2) == {"thin", "thick"}


def test_coherence_gate_keeps_entities_it_cannot_judge():
    """A support-1 entity has no pair to measure, so it has no coherence score. It is
    also structurally inert (a leaf binds nothing), so keeping it costs nothing —
    whereas cutting it would silently drop its concept-roster membership."""
    edges = _frequency_trap() + [_mention("m0", "singleton", 1)]
    hubs = hub_capped_entities(edges, 0.10, coherence={"hub": 0.63, "stopword": 0.38},
                               baseline=0.40)
    assert "singleton" not in hubs


def test_mention_edge_weight_alpha_defaults_to_identity():
    """alpha=1.0 must reproduce the current formula byte-for-byte: 0.9/log2(4) = 0.45."""
    assert mention_edge_weight(0.9, 3) == pytest.approx(0.45)
    assert mention_edge_weight(0.9, 3, alpha=1.0) == pytest.approx(0.45)


def test_mention_edge_weight_alpha_scales_linearly():
    assert mention_edge_weight(0.9, 3, alpha=2.0) == pytest.approx(0.90)
    assert mention_edge_weight(0.9, 3, alpha=0.5) == pytest.approx(0.225)
    assert mention_edge_weight(0.9, 3, alpha=0.0) == 0.0


def test_mention_edge_weight_alpha_applies_after_the_none_default():
    """A missing confidence still defaults to 0.5 before alpha is applied."""
    assert mention_edge_weight(None, 1, alpha=2.0) == pytest.approx(1.0)


def _edge_weight(graph, a, b):
    ia, ib = graph.vs.find(name=a).index, graph.vs.find(name=b).index
    return graph.es[graph.get_eid(ia, ib)]["weight"]


def test_build_graph_entity_alpha_scales_only_mention_edges():
    """alpha is the mixing ratio: entity edges move, similarity edges do not."""
    mention = [{"memory_id": "m0", "entity_id": "e1", "weight": 0.9, "support": 3}]
    knn = [{"src": "m0", "dst": "m1", "sim": 0.8}]
    base = build_graph(mention, knn, hub_share=None)
    scaled = build_graph(mention, knn, hub_share=None, entity_alpha=2.0)
    assert _edge_weight(scaled, "m0", "e1") == pytest.approx(2 * _edge_weight(base, "m0", "e1"))
    assert _edge_weight(scaled, "m0", "m1") == pytest.approx(_edge_weight(base, "m0", "m1"))


def test_build_graph_entity_alpha_default_is_a_no_op():
    mention = [{"memory_id": "m0", "entity_id": "e1", "weight": 0.9, "support": 3}]
    knn = [{"src": "m0", "dst": "m1", "sim": 0.8}]
    a = build_graph(mention, knn, hub_share=None)
    b = build_graph(mention, knn, hub_share=None, entity_alpha=1.0)
    assert a.es["weight"] == b.es["weight"]
    assert a.vs["name"] == b.vs["name"]


def test_uniform_weight_scaling_cannot_change_a_partition():
    """RBConfiguration modularity is scale-invariant, so scaling ALL weights by a constant
    is provably a no-op. This is why an original design's 'normalize total graph weight'
    control was replaced by granularity matching -- recorded so the reasoning survives."""
    mention = [{"memory_id": f"m{i}", "entity_id": "e1", "weight": 0.9, "support": 3}
               for i in range(4)]
    knn = [{"src": "m0", "dst": "m1", "sim": 0.9}, {"src": "m2", "dst": "m3", "sim": 0.9}]
    plain = build_graph(mention, knn, hub_share=None)
    scaled = build_graph(mention, knn, hub_share=None)
    scaled.es["weight"] = [w * 7.5 for w in scaled.es["weight"]]

    def shape(comms):
        return sorted(tuple(sorted(n for n, _k, _s in c["members"])) for c in comms)

    assert shape(detect_communities(plain, min_memories=2)) == \
           shape(detect_communities(scaled, min_memories=2))


def test_memory_knn_edges_decay_weight_by_newer_endpoint(kb):
    """Old<->old pairs fade toward the floor; an old memory keeps a full-weight edge to
    fresh content (newer-endpoint rule, GREATEST(f_src, f_dst)). Selection stays on raw
    cosine — `sim` is still the undecayed similarity."""
    old1 = kb.upsert(_mem_at("old1", days_ago=365, tags=("email",)))
    old2 = kb.upsert(_mem_at("old2", days_ago=365, tags=("email",)))
    fresh = kb.upsert(_mem_at("fresh"))                    # undated -> factor 1.0
    for n, h in ((old1, "h1"), (old2, "h2"), (fresh, "h3")):
        kb.set_embedding(n.id, _vec(0), embedded_hash=h)   # all identical: sim 1.0
    rows = kb.memory_knn_edges(k=4, min_sim=0.5)
    assert all(float(r["sim"]) > 0.99 for r in rows)       # selection basis unchanged
    w = {tuple(sorted((str(r["src"]), str(r["dst"])))): float(r["weight"]) for r in rows}
    oo = tuple(sorted((str(old1.id), str(old2.id))))
    of = tuple(sorted((str(old1.id), str(fresh.id))))
    assert abs(w[oo] - LEGACY_EMAIL_RULE.floor) < 0.02                   # old-old: ~floor
    assert w[of] > 0.97                                    # old-fresh: ~full weight


def test_memory_knn_edges_do_not_decay_untagged_notes(kb):
    """Amendment scope gate: an old DATED note without the email tag keeps full weight."""
    n1 = kb.upsert(_mem_at("n1", days_ago=365))
    n2 = kb.upsert(_mem_at("n2", days_ago=365))
    kb.set_embedding(n1.id, _vec(0), embedded_hash="h1")
    kb.set_embedding(n2.id, _vec(0), embedded_hash="h2")
    rows = kb.memory_knn_edges(k=4, min_sim=0.5)
    assert rows and all(float(r["weight"]) > 0.97 for r in rows)


def test_mention_edges_age_factor_is_one_for_untagged_notes(kb):
    """Amendment scope gate: an old DATED note without the email tag keeps age_factor 1.0."""
    m = kb.upsert(_mem_at("m1", days_ago=365))
    ent = kb.upsert_entity("Ollama", categories=["tool"])
    kb.link_entity(m.id, ent.node.id, MENTIONS, weight=0.9)
    rows = kb.concept_mention_edges()
    assert [float(r["age_factor"]) for r in rows] == [1.0]


def test_path_scoped_rule_decays_notes_under_that_folder(kb, pool):
    journal = DecayRule("journal", Match(path_prefix="notes.journal"), 30.0, 0.5)
    scoped = KnowledgeBase(pool, default_registry(), policy=Policy(decay=(journal,)))
    old_j = scoped.upsert(_mem_at("j_old", days_ago=3000, parent_path="notes.journal"))
    old_n = scoped.upsert(_mem_at("n_old", days_ago=3000))
    ent = scoped.upsert_entity("Ollama", categories=["tool"])
    for m in (old_j, old_n):
        scoped.link_entity(m.id, ent.node.id, MENTIONS, weight=0.9)
    f = {r["memory_id"]: float(r["age_factor"]) for r in scoped.concept_mention_edges()}
    assert abs(f[old_j.id] - 0.5) < 0.01                       # matched: ~floor
    assert f[old_n.id] == 1.0                                  # unmatched folder: never


def test_no_rules_means_nothing_decays(kb, pool):
    plain = KnowledgeBase(pool, default_registry(), policy=Policy())
    m = plain.upsert(_mem_at("e_old", days_ago=3000, tags=("email",)))
    ent = plain.upsert_entity("Ollama", categories=["tool"])
    plain.link_entity(m.id, ent.node.id, MENTIONS, weight=0.9)
    assert float(plain.concept_mention_edges()[0]["age_factor"]) == 1.0
