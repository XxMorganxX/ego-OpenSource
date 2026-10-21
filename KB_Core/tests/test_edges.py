from kb_core.domain import NodeRecord


def _rec(eid, slug):
    return NodeRecord(source="agent", external_id=eid, type="note",
                      content="c", slug=slug)


def test_link_creates_edge_and_neighbors_respects_direction(kb):
    a = kb.upsert(_rec("a", "a"))
    b = kb.upsert(_rec("b", "b"))
    kb.link(a.id, b.id, "relates_to", weight=0.7)
    assert [n.id for n in kb.neighbors(a.id)] == [b.id]                 # a -> b (out)
    assert [n.id for n in kb.neighbors(b.id, direction="in")] == [a.id]  # b <- a (in)
    assert kb.neighbors(b.id) == []                                    # b has no outgoing


def test_link_is_idempotent_and_updates_weight(kb):
    a = kb.upsert(_rec("a", "a"))
    b = kb.upsert(_rec("b", "b"))
    kb.link(a.id, b.id, "relates_to", weight=0.1)
    kb.link(a.id, b.id, "relates_to", weight=0.9)      # same key -> update, not duplicate
    edges = kb.get_edges(a.id, relation_type="relates_to")
    assert len(edges) == 1 and edges[0].weight == 0.9


def test_relation_type_separates_entity_and_memory_edges(kb):
    m = kb.upsert(_rec("m", "m"))
    other = kb.upsert(_rec("o", "o"))
    ent = kb.upsert(_rec("e", "e"))
    kb.link(m.id, other.id, "relates_to")              # memory <-> memory
    kb.link(m.id, ent.id, "mentions", weight=0.8)      # entity mention
    assert [n.id for n in kb.neighbors(m.id, relation_type="mentions")] == [ent.id]
    assert [n.id for n in kb.neighbors(m.id, relation_type="relates_to")] == [other.id]


def test_unlink_removes_edge(kb):
    a = kb.upsert(_rec("a", "a"))
    b = kb.upsert(_rec("b", "b"))
    kb.link(a.id, b.id, "relates_to")
    assert kb.unlink(a.id, b.id, "relates_to") == 1
    assert kb.neighbors(a.id) == []
    assert kb.unlink(a.id, b.id, "relates_to") == 0    # already gone


def test_deleting_a_node_cascades_its_edges(kb):
    a = kb.upsert(_rec("a", "a"))
    b = kb.upsert(_rec("b", "b"))
    kb.link(a.id, b.id, "relates_to")
    kb.delete(b.id)                                    # FK ON DELETE CASCADE
    assert kb.get_edges(a.id) == []
