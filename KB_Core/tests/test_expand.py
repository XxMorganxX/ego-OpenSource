"""KnowledgeBase.expand(): the read-only one-hop neighborhood pivot.

Seeds a two-project shape by hand (concept runs applied directly, not via
consolidation) so every score is exactly predictable: Alpha (3 memories, exclusive
AlphaRepo) and Beta (2 memories, exclusive BetaRepo), bridged by Quill who is
mentioned in one Alpha memory and both Beta memories at equal weight."""
import pytest

from kb_core.domain import NodeRecord
from kb_core.entities import MENTIONS


def _vec(axis):
    v = [0.0] * 1024
    v[axis] = 1.0
    return v


def _seed(kb):
    mems = {}
    for i, slug in enumerate(("a0", "a1", "a2", "b0", "b1")):
        m = kb.upsert(NodeRecord(source="agent", external_id=slug, type="note",
                                 content=f"note {slug}", slug=slug,
                                 title=f"Note {slug}"))
        kb.set_embedding(m.id, _vec(0 if slug.startswith("a") else 1),
                         embedded_hash=f"h{slug}")
        mems[slug] = m
    alpha_repo = kb.upsert_entity("AlphaRepo", categories=["tool"])
    beta_repo = kb.upsert_entity("BetaRepo", categories=["tool"])
    quill = kb.upsert_entity("Quill", categories=["person"])
    for slug in ("a0", "a1", "a2"):
        kb.link_entity(mems[slug].id, alpha_repo.node.id, MENTIONS, weight=0.9)
    for slug in ("b0", "b1"):
        kb.link_entity(mems[slug].id, beta_repo.node.id, MENTIONS, weight=0.9)
    for slug in ("a1", "b0", "b1"):
        kb.link_entity(mems[slug].id, quill.node.id, MENTIONS, weight=0.9)

    run = kb.begin_concept_run({})
    kb.apply_concept_run(run, [
        {"key": "a", "prev_id": None, "parent_key": None, "level": 0,
         "name": "Alpha", "kind": "project", "description": None, "embedding": _vec(0),
         "members": [(str(mems["a0"].id), "memory", 1.0),
                     (str(mems["a1"].id), "memory", 0.9),
                     (str(mems["a2"].id), "memory", 0.8),
                     (str(alpha_repo.node.id), "entity", 1.0),
                     (str(quill.node.id), "entity", 0.333)]},
        {"key": "b", "prev_id": None, "parent_key": None, "level": 0,
         "name": "Beta", "kind": "project", "description": None, "embedding": _vec(0),
         "members": [(str(mems["b0"].id), "memory", 1.0),
                     (str(mems["b1"].id), "memory", 0.9),
                     (str(beta_repo.node.id), "entity", 1.0),
                     (str(quill.node.id), "entity", 0.667)]},
    ], [])
    kb.finish_concept_run(run, status="done", stats={})
    concepts = {c["name"]: c for c in kb.active_concepts()}
    return mems, {"AlphaRepo": alpha_repo, "BetaRepo": beta_repo,
                  "Quill": quill}, concepts


def test_expand_entity_ranks_concepts_by_live_share(kb):
    mems, ents, concepts = _seed(kb)
    out = kb.expand(ents["Quill"].node.id)
    assert out["kind"] == "entity" and out["label"] == "Quill"
    assert out["consolidated_at"] is not None
    cg = out["groups"]["concepts"]
    assert [c["label"] for c in cg] == ["Beta", "Alpha"]   # 2/3 vs 1/3 of his weight
    assert cg[0]["score"] == pytest.approx(0.667, abs=0.001)
    assert cg[1]["score"] == pytest.approx(0.333, abs=0.001)
    assert all(c["via"] == "share" for c in cg)
    co = out["groups"]["co_entities"]
    assert {c["label"] for c in co} == {"AlphaRepo", "BetaRepo"}
    assert all(c["via"] == "co-mention" for c in co)
    assert len(out["groups"]["memories"]) == 3


def test_expand_entity_live_share_ignores_membership_cliff(kb):
    """Quill is NOT a stored member of a third concept holding one of his memories —
    live share still surfaces it (the 0.25 storage threshold must not gate search)."""
    mems, ents, concepts = _seed(kb)
    run = kb.begin_concept_run({})
    kb.apply_concept_run(run, [
        {"key": "c", "prev_id": None, "parent_key": None, "level": 0,
         "name": "Gamma", "kind": "topic", "description": None, "embedding": None,
         "members": [(str(mems["b1"].id), "memory", 1.0)]}], [])
    kb.finish_concept_run(run, status="done", stats={})
    cg = kb.expand(ents["Quill"].node.id)["groups"]["concepts"]
    gamma = next((c for c in cg if c["label"] == "Gamma"), None)
    assert gamma is not None
    assert gamma["score"] == pytest.approx(0.333, abs=0.001)


def test_expand_concept_ranks_entities_by_inside_weight_not_share(kb):
    """The direction fix: a marginal-but-exclusive entity (share 1.0, one memory)
    must NOT outrank the concept's defining entity (three memories, also exclusive)."""
    mems, ents, concepts = _seed(kb)
    one_note = kb.upsert_entity("OneNote", categories=["tool"])
    kb.link_entity(mems["a1"].id, one_note.node.id, MENTIONS, weight=0.9)
    out = kb.expand(concepts["Alpha"]["id"])
    assert out["kind"] == "concept"
    eg = out["groups"]["entities"]
    labels = [e["label"] for e in eg]
    assert labels[0] == "AlphaRepo"                      # central: 3 damped mentions
    assert labels.index("AlphaRepo") < labels.index("OneNote")
    one = next(e for e in eg if e["label"] == "OneNote")
    assert one["share"] == pytest.approx(1.0, abs=0.001)  # exclusive but marginal
    assert all(e["via"] == "inside-weight" for e in eg)
    assert [m["via"] for m in out["groups"]["memories"]] == ["member"] * 3


def test_expand_concept_related_bridges_and_separation_suppression(kb):
    mems, ents, concepts = _seed(kb)
    rel = kb.expand(concepts["Alpha"]["id"])["groups"]["related_concepts"]
    beta = next((r for r in rel if r["label"] == "Beta"), None)
    assert beta is not None and beta["via"] == "entity-bridge:Quill"
    assert beta["score"] == pytest.approx(0.333, abs=0.001)  # min(0.333, 0.667)

    kb.add_concept_separation(concepts["Alpha"]["id"], concepts["Beta"]["id"],
                              note="unrelated")
    rel = kb.expand(concepts["Alpha"]["id"])["groups"]["related_concepts"]
    # Beta bridges via Quill AND matches by embedding — the separation kills both.
    assert all(r["label"] != "Beta" for r in rel)


def test_expand_concept_related_includes_family(kb):
    mems, ents, concepts = _seed(kb)
    run = kb.begin_concept_run({})
    kb.apply_concept_run(run, [
        {"key": "a", "prev_id": concepts["Alpha"]["id"], "parent_key": None,
         "level": 0, "name": "Alpha", "kind": "project", "description": None,
         "embedding": None, "members": [(str(mems["a0"].id), "memory", 1.0)]},
        {"key": "a.0", "prev_id": None, "parent_key": "a", "level": 1,
         "name": "Alpha subtopic", "kind": "topic", "description": None,
         "embedding": None, "members": [(str(mems["a0"].id), "memory", 1.0)]}], [])
    kb.finish_concept_run(run, status="done", stats={})
    rel = kb.expand(concepts["Alpha"]["id"])["groups"]["related_concepts"]
    child = next((r for r in rel if r["label"] == "Alpha subtopic"), None)
    assert child is not None and child["via"] == "child" and child["score"] == 1.0
    sub = next(c for c in kb.active_concepts() if c["name"] == "Alpha subtopic")
    rel_up = kb.expand(sub["id"])["groups"]["related_concepts"]
    assert rel_up[0]["label"] == "Alpha" and rel_up[0]["via"] == "parent"


def test_expand_memory_groups(kb):
    mems, ents, concepts = _seed(kb)
    out = kb.expand(mems["a1"].id)
    assert out["kind"] == "memory"
    g = out["groups"]
    assert {e["label"] for e in g["entities"]} == {"AlphaRepo", "Quill"}
    assert [c["label"] for c in g["concepts"]] == ["Alpha"]
    sim = [m["label"] for m in g["similar_memories"]]
    assert sim[:2] == ["Note a0", "Note a2"] or sim[:2] == ["Note a2", "Note a0"]
    assert all(m["via"] == "embedding" for m in g["similar_memories"])
    co = {m["label"] for m in g["co_mentioned"]}
    assert {"Note a0", "Note a2", "Note b0", "Note b1"} <= co   # via AlphaRepo + Quill


def test_expand_unknown_and_invalid_ids(kb):
    _seed(kb)
    with pytest.raises(LookupError):
        kb.expand("00000000-0000-0000-0000-000000000000")
    with pytest.raises(ValueError):
        kb.expand("not-a-uuid")


def test_expand_k_caps_every_group(kb):
    mems, ents, concepts = _seed(kb)
    g = kb.expand(ents["Quill"].node.id, k=1)["groups"]
    assert all(len(rows) <= 1 for rows in g.values())
