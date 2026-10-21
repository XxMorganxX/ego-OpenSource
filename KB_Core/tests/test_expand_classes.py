from kb_core.domain import NodeRecord
from kb_core.entities import MENTIONS


def _note(kb, ext):
    return kb.upsert(NodeRecord(source="agent", external_id=ext, type="note",
                                content=f"body {ext}", slug=ext))


def _seed_project_with_people(kb):
    """A project entity, two people, one tool — all co-mentioned in one memory."""
    mem = _note(kb, "proj_mem")
    proj = kb.upsert_entity("Acme Project", categories=["project"])
    alice = kb.upsert_entity("Alice", categories=["person"])
    bob = kb.upsert_entity("Bob", categories=["person"])
    tool = kb.upsert_entity("Supabase", categories=["tool"])
    for ent, w in ((proj, 1.0), (alice, 0.8), (bob, 0.7), (tool, 0.9)):
        kb.link_entity(mem.id, ent.node.id, MENTIONS, weight=w)
    return mem, proj


def test_expand_entity_rows_carry_classes(kb):
    mem, proj = _seed_project_with_people(kb)
    out = kb.expand(proj.node.id)
    co = {r["label"]: r for r in out["groups"]["co_entities"]}
    assert co["Alice"]["classes"] == ["person"]
    assert co["Supabase"]["classes"] == ["tool"]


def test_expand_class_filter_restricts_to_persons(kb):
    mem, proj = _seed_project_with_people(kb)
    out = kb.expand(proj.node.id, classes=["person"])
    labels = [r["label"] for r in out["groups"]["co_entities"]]
    assert sorted(labels) == ["Alice", "Bob"]        # the stakeholder query
    # non-entity groups are untouched by the filter
    assert [r["label"] for r in out["groups"]["memories"]]


def test_expand_memory_entities_honor_class_filter(kb):
    mem, proj = _seed_project_with_people(kb)
    out = kb.expand(mem.id, classes=["person"])
    labels = sorted(r["label"] for r in out["groups"]["entities"])
    assert labels == ["Alice", "Bob"]


def test_expand_class_filter_is_case_insensitive_and_optional(kb):
    mem, proj = _seed_project_with_people(kb)
    upper = kb.expand(proj.node.id, classes=["Person"])
    assert sorted(r["label"] for r in upper["groups"]["co_entities"]) == \
        ["Alice", "Bob"]
    unfiltered = kb.expand(proj.node.id)
    assert len(unfiltered["groups"]["co_entities"]) == 3
