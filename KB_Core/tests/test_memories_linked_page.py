"""memories_linked_page: the paged, importance-first sibling of memories_linked used by
the dashboard's entity → source-memories drill-down."""

from kb_core.domain import NodeRecord
from kb_core.entities import BELONGS_TO, MENTIONS


def _mem(kb, eid, manual=0.0):
    n = kb.upsert(NodeRecord(source="agent", external_id=eid, type="note",
                             content="c", slug=eid, parent_path="notes"))
    if manual:
        kb.set_importance(n.id, manual)
    return n


def test_pages_importance_first_with_total(kb):
    ent = kb.upsert_entity("widget")
    low = _mem(kb, "low", manual=0.2)
    high = _mem(kb, "high", manual=1.5)
    mid = _mem(kb, "mid", manual=1.0)
    for m in (low, high, mid):
        kb.link_entity(m.id, ent.node.id, MENTIONS, weight=0.5)

    rows, total = kb.memories_linked_page(ent.node.id, limit=2, offset=0)
    assert total == 3
    assert [n.id for n, _, _ in rows] == [high.id, mid.id]
    assert rows[0][1] == MENTIONS and rows[0][2] == 0.5

    rows2, total2 = kb.memories_linked_page(ent.node.id, limit=2, offset=2)
    assert total2 == 3
    assert [n.id for n, _, _ in rows2] == [low.id]


def test_dedupes_memory_linked_under_both_link_types(kb):
    ent = kb.upsert_entity("gadget")
    m = _mem(kb, "both")
    kb.link_entity(m.id, ent.node.id, MENTIONS, weight=0.3)
    kb.link_entity(m.id, ent.node.id, BELONGS_TO, weight=0.9)
    rows, total = kb.memories_linked_page(ent.node.id)
    assert total == 1 and len(rows) == 1
    node, lt, w = rows[0]
    assert node.id == m.id
    assert lt == BELONGS_TO  # min(link_type) — alphabetically first
    assert w == 0.9          # max(weight)


def test_link_type_filter(kb):
    ent = kb.upsert_entity("gizmo")
    a = _mem(kb, "mention-only")
    b = _mem(kb, "member-only")
    kb.link_entity(a.id, ent.node.id, MENTIONS)
    kb.link_entity(b.id, ent.node.id, BELONGS_TO)
    rows, total = kb.memories_linked_page(ent.node.id, link_type=MENTIONS)
    assert total == 1
    assert [n.id for n, _, _ in rows] == [a.id]


def test_unlinked_entity_and_offset_past_end(kb):
    ent = kb.upsert_entity("lonely")
    assert kb.memories_linked_page(ent.node.id) == ([], 0)
    m = _mem(kb, "one")
    kb.link_entity(m.id, ent.node.id, MENTIONS)
    assert kb.memories_linked_page(ent.node.id, offset=5) == ([], 0)


def test_non_entity_node_returns_empty(kb):
    plain = _mem(kb, "plain-note")
    assert kb.memories_linked_page(plain.id) == ([], 0)
