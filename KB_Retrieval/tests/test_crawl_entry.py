"""The entry step: A[:3] label-cosine  u  C_damped[:2] evidence-vote  u  N[:2] name.

Every test here runs inside a read_snapshot(), because that is how strategy.py calls
entry_step -- and both primitives it uses needed a conn= parameter added for that to
be honest.
"""

from kb_core.domain import NodeRecord

from kb_retrieval.contract import Query
from kb_retrieval.crawl.entry import entry_step


def _note(kb, ext, content, **kw):
    return kb.upsert(NodeRecord(source="agent", external_id=ext, type="note",
                                content=content, slug=ext, **kw))


def _vec(hot):
    v = [0.0] * 1024
    v[hot] = 1.0
    return v


def _seeds(kb, q):
    with kb.read_snapshot() as conn:
        return entry_step(kb, q, conn)


def test_a_pre_resolved_anchor_seeds_without_any_embedding(kb, make_entity):
    """anchor_entity needs no vector, so it is honoured even with the embedder down
    -- and it is added FIRST so an explicitly supplied anchor keeps its own `via`
    tag if a name rung would otherwise re-add the same id."""
    eid = make_entity("Widget")
    seeds = _seeds(kb, Query(text="unrelated words", anchor_entity=eid))
    assert [(s.kind, s.id, s.via) for s in seeds] == [("entity", eid, "anchor_entity")]


def test_a_label_only_concept_does_not_raise_on_the_vote_sort(kb, make_concept):
    """REGRESSION. concept_candidates returns label_rank and vote_rank INDEPENDENTLY,
    and either may be None. A concept with an embedding but no member in the query's
    ANN top-25 has vote_rank None -- the common case, not an edge case -- and sorting
    the raw list by that column raises TypeError comparing None to int. Each family
    must be filtered to its own members before it is sorted."""
    cid = make_concept("Near", [], embedding=_vec(0))
    seeds = _seeds(kb, Query(text="anything", embedding=_vec(0)))
    assert [(s.kind, s.id, s.via) for s in seeds] == [("concept", cid, "label-cosine")]


def test_a_vote_only_concept_is_seeded_too(kb, make_concept):
    """The mirror image: a concept with members but no embedding has label_rank None
    and must still be reachable through family C."""
    m = _note(kb, "m", "x")
    kb.set_embedding(m.id, _vec(0), embedded_hash="h")
    cid = make_concept("Voted", [(m.id, "memory", 1.0)])   # no embedding
    seeds = _seeds(kb, Query(text="anything", embedding=_vec(0)))
    assert [(s.kind, s.id, s.via) for s in seeds] == [("concept", cid, "evidence-vote")]


def test_a_concept_winning_both_families_yields_one_seed_keeping_the_first_via(kb,
                                                                               make_concept):
    """A[:3] u C[:2] is a UNION, not a concatenation -- the measured gain comes from
    the union. A concept that wins both must not occupy two of the <=7 anchor slots,
    and the surviving `via` is the earlier (label) family so the trace stays stable."""
    m = _note(kb, "m", "x")
    kb.set_embedding(m.id, _vec(0), embedded_hash="h")
    cid = make_concept("Both", [(m.id, "memory", 1.0)], embedding=_vec(0))
    seeds = _seeds(kb, Query(text="anything", embedding=_vec(0)))
    assert [(s.kind, s.id, s.via) for s in seeds] == [("concept", cid, "label-cosine")]


def test_only_the_precise_name_rungs_become_seeds(kb, make_entity):
    """find_entities has four rungs. Only `name` and `fuzzy-name` are entry points.
    `description` and `memory-vote` resolve an entity from CONTENT similarity --
    which is the judgment this crawl exists to ROUTE around rather than perform --
    and memory-vote in particular re-derives the same evidence signal family C
    already carries, so admitting it would double-count one measurement as two
    families."""
    m = _note(kb, "m", "quarterly planning")
    kb.set_embedding(m.id, _vec(0), embedded_hash="h")
    make_entity("Zebra", mentions=[m.id])   # reachable ONLY via memory-vote
    named = make_entity("Quarterly")        # reachable via the name rung

    with kb.read_snapshot() as conn:
        hits = kb.find_entities("Quarterly", embedding=_vec(0), conn=conn)
        seeds = entry_step(kb, Query(text="Quarterly", embedding=_vec(0)), conn)
    # find_entities really does surface the memory-vote entity -- so the filter is
    # doing work, not passing a set that was already empty.
    assert "memory-vote" in {h["via"] for h in hits}
    assert [s.id for s in seeds] == [named]
    assert seeds[0].via == "name"


def test_entry_never_returns_a_memory_id(kb, make_concept, make_entity):
    """The concept layer is the ROUTER, never the reachable set. Memories
    enter the pool through hop 0 and through the frontiers, never as an entry seed."""
    m = _note(kb, "m", "widget report")
    kb.set_embedding(m.id, _vec(0), embedded_hash="h")
    make_concept("C", [(m.id, "memory", 1.0)], embedding=_vec(0))
    make_entity("Widget", mentions=[m.id])
    seeds = _seeds(kb, Query(text="Widget", embedding=_vec(0)))
    assert seeds
    assert {s.kind for s in seeds} <= {"concept", "entity"}
    assert m.id not in {s.id for s in seeds}
