from kb_core.domain import NodeRecord
from kb_core.entities import MENTIONS


def _mem(eid, content="met avery today"):
    return NodeRecord(source="agent", external_id=eid, type="note",
                      content=content, slug=eid)


def test_upsert_entity_creates_node_and_extension(kb):
    ent = kb.upsert_entity("Avery", categories=["person"], description="A person")
    assert ent.entity_type == "instance" and ent.canonical_name == "Avery"
    assert ent.node.type == "entity" and ent.node.source == "entity"
    assert ent.node.path == "entities.instances.Avery"     # flat; category is a link
    assert [c.canonical_name for c in kb.classes_of(ent.node.id)] == ["person"]
    got = kb.get_entity(ent.node.id)
    assert got is not None and got.description == "A person"


def test_upsert_entity_dedups_by_canonical_key(kb):
    a = kb.upsert_entity("Avery", categories=["person"])
    b = kb.upsert_entity("  avery ", categories=["person"])   # same concept, different surface
    assert a.node.id == b.node.id                              # deduped to one node
    assert len(kb.list_entities(entity_type="instance")) == 1


def test_case_variant_upsert_preserves_established_casing(kb):
    """Without this, an entity's canonical_name flips 'Widgetron'->'widgetron'->'Widgetron'
    repeatedly because a case/punctuation-only variant re-upserting onto the SAME node (same
    canonical_key -> same instance_external_id -> same node_id) hits `_upsert_instance_node`'s
    ON CONFLICT ... DO UPDATE, which would be last-write-wins on name, title, AND content (content
    defaults to `description or name`). First-established casing must stick: a later upsert that
    only differs in casing/punctuation reuses the stored canonical_name for the whole write, so
    the node's title and content hash stop churning too. A genuine rename (different letters)
    mints a different canonical_key/external_id/node_id and is untouched by this -- see
    test_upsert_entity_dedups_by_canonical_key for the dedup half of this same key."""
    a = kb.upsert_entity("Widgetron", categories=["organization"])
    b = kb.upsert_entity("widgetron", categories=["organization"])
    assert a.node.id == b.node.id
    assert b.canonical_name == "Widgetron"
    got = kb.get_entity(a.node.id)
    assert got.canonical_name == "Widgetron"
    node = kb.get(a.node.id)
    assert node.title == "Widgetron"
    assert node.content == "Widgetron"      # would be "widgetron" pre-fix -- the re-embed churn half


def test_same_name_across_types_is_one_node_with_many_classes(kb):
    # Phase 2: identity is name-only, so the same name under different categories is ONE node
    # that belongs to both — the core de-dup behavior the refactor exists for.
    person = kb.upsert_entity("Mercury", categories=["person"])
    topic = kb.upsert_entity("Mercury", categories=["topic"])
    assert person.node.id == topic.node.id
    assert sorted(c.canonical_name for c in kb.classes_of(person.node.id)) == ["person", "topic"]


def test_list_and_instances_of_by_category_link(kb):
    kb.upsert_entity("Avery", categories=["person"])
    kb.upsert_entity("Postgres", categories=["topic"])
    assert len(kb.list_entities(entity_type="instance")) == 2          # both are instances
    assert {e.canonical_name for e in kb.instances_of("person")} == {"Avery"}
    assert {e.canonical_name for e in kb.instances_of("topic")} == {"Postgres"}


def test_entity_links_to_memories_via_mentions(kb, pool):
    m1 = kb.upsert(_mem("m1"))
    m2 = kb.upsert(_mem("m2"))
    ent = kb.upsert_entity("Avery", categories=["person"])
    kb.link_entity(m1.id, ent.node.id, MENTIONS, weight=0.9)
    kb.link_entity(m2.id, ent.node.id, MENTIONS, weight=0.4)
    # "memories about Avery" = incoming 'mentions' links in the entity_links overlay
    about = kb.memories_linked(ent.node.id, link_type=MENTIONS)
    assert {n.id for n in about} == {m1.id, m2.id}
    # and the weighted links are inspectable
    with pool.connection() as conn:
        weights = sorted(r["weight"] for r in conn.execute(
            "SELECT weight FROM entity_links WHERE entity_id = %s", (ent.node.id,)).fetchall())
    assert weights == [0.4, 0.9]


def test_upsert_entity_class_is_top_level_category(kb):
    cls = kb.upsert_entity_class("people")
    assert cls.entity_type == "class" and cls.node.path == "entities.people"
    assert kb.entity_class_names() == ["people"]
    assert kb.get_entity_class("People").node.id == cls.node.id   # normalized dedup


def test_memory_belongs_to_class_edge(kb):
    from kb_core.entities import BELONGS_TO
    mem = kb.upsert(_mem("m1"))
    cls = kb.upsert_entity_class("people")
    kb.link_entity(mem.id, cls.node.id, BELONGS_TO)
    grouped = kb.memories_linked(cls.node.id, link_type=BELONGS_TO)
    assert [n.id for n in grouped] == [mem.id]
