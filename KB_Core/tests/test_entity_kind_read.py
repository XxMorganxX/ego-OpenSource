"""Read-path surfacing: kind in profiles/find/expand, expand entity_kind filter."""

import pytest

from kb_core.domain import NodeRecord


@pytest.fixture
def two_memory_nodes(kb):
    """Two note nodes to stage candidates against (copied from
    test_entity_kind_promotion.py's fixture of the same name)."""
    m1 = kb.upsert(NodeRecord(source="agent", external_id="tekr-m1", type="note",
                              content="c1", slug="tekr-m1"))
    m2 = kb.upsert(NodeRecord(source="agent", external_id="tekr-m2", type="note",
                              content="c2", slug="tekr-m2"))
    return m1.id, m2.id


def _seed(kb, two_memory_nodes):
    m1, m2 = two_memory_nodes
    kb.upsert_entity_class("person", active=True)
    kb.upsert_entity_class("tool", active=True)
    person = kb.upsert_entity("Jordan Reyes", categories=["person"])
    tool = kb.upsert_entity("Playwright", categories=["tool"])
    kb.derive_instance_kinds()
    kb.link_entity(m1, person.node.id, "mentions")
    kb.link_entity(m1, tool.node.id, "mentions")
    return m1, person, tool


def test_find_entities_carries_kind(kb, two_memory_nodes):
    _seed(kb, two_memory_nodes)
    hits = kb.find_entities("Jordan Reyes")
    assert hits and hits[0]["kind"] == "person"
    assert "occurred_at" in hits[0]


def test_entity_profile_carries_kind(kb, two_memory_nodes):
    _, person, _ = _seed(kb, two_memory_nodes)
    prof = kb.entity_profile(person.node.id)
    assert prof["kind"] == "person"
    assert "occurred_at" in prof


def test_expand_rows_carry_entity_kind(kb, two_memory_nodes):
    m1, person, tool = _seed(kb, two_memory_nodes)
    out = kb.expand(m1)
    ent_rows = out["groups"]["entities"]
    assert ent_rows and all("entity_kind" in r for r in ent_rows)
    kinds = {r["id"]: r["entity_kind"] for r in ent_rows}
    assert kinds[str(person.node.id)] == "person"
    assert kinds[str(tool.node.id)] == "system"          # LEGACY_POLICY: category tool -> system


def test_expand_entity_kind_filter(kb, two_memory_nodes):
    m1, person, tool = _seed(kb, two_memory_nodes)
    out = kb.expand(m1, entity_kind="person")
    ids = {r["id"] for r in out["groups"]["entities"]}
    assert str(person.node.id) in ids
    assert str(tool.node.id) not in ids
    # non-entity groups are untouched by the filter
    assert "concepts" in out["groups"]


def test_expand_entity_kind_filter_on_entity_pivot(kb, two_memory_nodes):
    """co_entities (entity pivot) also honors entity_kind — a different arm than
    the memory pivot's `entities` group, exercising the same helper."""
    m1, person, tool = _seed(kb, two_memory_nodes)
    out = kb.expand(person.node.id, entity_kind="system")
    ids = {r["id"] for r in out["groups"]["co_entities"]}
    assert str(tool.node.id) in ids


def test_expand_concept_entity_kind_reaches_below_top_k(kb, two_memory_nodes):
    """Reproduces the reviewer's exact bug: a concept with 10 high-weight tool
    entities and 3 low-weight person entities. The unfiltered top-k (default k=8)
    is entirely crowded out by tools, so a naive post-trim filter finds zero
    persons in that top-8 — entity_kind='person' must still surface them."""
    m1, _m2 = two_memory_nodes
    kb.upsert_entity_class("person", active=True)
    kb.upsert_entity_class("tool", active=True)
    tools = [kb.upsert_entity(f"Tool{i}", categories=["tool"]) for i in range(10)]
    persons = [kb.upsert_entity(f"Person{i}", categories=["person"]) for i in range(3)]
    kb.derive_instance_kinds()
    for t in tools:
        kb.link_entity(m1, t.node.id, "mentions", weight=0.9)
    for p in persons:
        kb.link_entity(m1, p.node.id, "mentions", weight=0.1)

    run = kb.begin_concept_run({})
    kb.apply_concept_run(run, [
        {"key": "proj", "prev_id": None, "parent_key": None, "level": 0,
         "name": "Proj", "kind": "project", "description": None, "embedding": None,
         "members": [(str(m1), "memory", 1.0)]},
    ], [])
    kb.finish_concept_run(run, status="done", stats={})
    concept = next(c for c in kb.active_concepts() if c["name"] == "Proj")

    unfiltered = kb.expand(concept["id"])["groups"]["entities"]
    assert len(unfiltered) == 8                       # default k, tools crowd it out
    assert all(r["entity_kind"] != "person" for r in unfiltered)

    filtered = kb.expand(concept["id"], entity_kind="person")["groups"]["entities"]
    ids = {r["id"] for r in filtered}
    assert {str(p.node.id) for p in persons} <= ids    # the bug: this was empty


def test_expand_entity_kind_filter_reaches_below_top_k_on_entity_pivot(kb,
                                                                        two_memory_nodes):
    """Same crowding bug on the entity-pivot's co_entities arm (a different SQL
    query with its own LIMIT %(k)s) — pivoting off one of the tools."""
    m1, _m2 = two_memory_nodes
    kb.upsert_entity_class("person", active=True)
    kb.upsert_entity_class("tool", active=True)
    pivot = kb.upsert_entity("PivotTool", categories=["tool"])
    tools = [kb.upsert_entity(f"Tool{i}", categories=["tool"]) for i in range(10)]
    persons = [kb.upsert_entity(f"Person{i}", categories=["person"]) for i in range(3)]
    kb.derive_instance_kinds()
    kb.link_entity(m1, pivot.node.id, "mentions", weight=0.9)
    for t in tools:
        kb.link_entity(m1, t.node.id, "mentions", weight=0.9)
    for p in persons:
        kb.link_entity(m1, p.node.id, "mentions", weight=0.1)

    unfiltered = kb.expand(pivot.node.id)["groups"]["co_entities"]
    assert len(unfiltered) == 8
    assert all(r["entity_kind"] != "person" for r in unfiltered)

    filtered = kb.expand(pivot.node.id, entity_kind="person")["groups"]["co_entities"]
    ids = {r["id"] for r in filtered}
    assert {str(p.node.id) for p in persons} <= ids


def _class_kind(kb, name):
    """Read a class's stored kind the way test_class_curation.py/test_curation.py do —
    `list_entity_classes()` is the read path this file's other tests don't need (they read
    kind off an *instance* via find_entities/entity_profile/expand, which requires deriving
    instance kinds from a class). `Entity` (upsert_entity_class's return value) carries no
    `kind` attribute, so class-kind assertions go through this helper instead."""
    return next(r["kind"] for r in kb.list_entity_classes() if r["name"] == name)


def test_upsert_class_kind_follows_the_kb_policy(kb, pool):
    from kb_core.knowledge_base import KnowledgeBase
    from kb_core.policy import Category, Policy, Taxonomy
    from kb_core.registry import default_registry
    custom = KnowledgeBase(pool, default_registry(), policy=Policy(taxonomy=Taxonomy(
        categories=(Category("vessel", "artifact", "a ship or boat"),))))
    custom.upsert_entity_class("vessel", kind="system")
    assert _class_kind(custom, "vessel") == "artifact"    # seed wins
    custom.upsert_entity_class("marina", kind="place")
    assert _class_kind(custom, "marina") == "place"       # no seed: model
    kb.upsert_entity_class("repository")
    assert _class_kind(kb, "repository") == "system"      # legacy seed


def test_expand_memory_entities_carry_role(kb, two_memory_nodes):
    """role is best-effort: the memory pivot's `entities` arm reads directly off a
    single entity_links mention edge, so it carries whatever role that edge has."""
    m1, m2 = two_memory_nodes
    kb.upsert_entity_class("person", active=True)
    kb.stage_entity_candidate(m1, "person", "Jordan Reyes", confidence=0.9,
                              role="stakeholder")
    kb.stage_entity_candidate(m2, "person", "Jordan Reyes", confidence=0.9)
    kb.promote_entity("person", "Jordan Reyes")
    out = kb.expand(m1)
    ent = next(r for r in out["groups"]["entities"] if r["label"] == "Jordan Reyes")
    assert ent["role"] == "stakeholder"
    out2 = kb.expand(m2)
    ent2 = next(r for r in out2["groups"]["entities"] if r["label"] == "Jordan Reyes")
    assert "role" not in ent2 or ent2.get("role") is None
