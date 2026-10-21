"""Tests for merge_instances and the canonical merge_pair helper."""
import uuid


from kb_core.domain import NodeRecord
from kb_core.entities import MENTIONS, merge_pair


# ---------------------------------------------------------------------------
# Pure helper
# ---------------------------------------------------------------------------

def test_merge_pair_is_canonical_and_symmetric():
    a, b = uuid.uuid4(), uuid.uuid4()
    assert merge_pair(a, b) == merge_pair(b, a)
    lo, hi = merge_pair(a, b)
    assert str(lo) < str(hi)


# ---------------------------------------------------------------------------
# Integration tests (need a live KB)
# ---------------------------------------------------------------------------

def _mem(eid, content="c"):
    return NodeRecord(source="agent", external_id=eid, type="note", content=content, slug=eid)


def test_merge_instances_folds_source_into_target(kb):
    # two instances, each mentioned by a different memory
    src = kb.upsert_entity("Acme")
    tgt = kb.upsert_entity("Acme University")
    m1 = kb.upsert(_mem("n1", "about acme"))
    m2 = kb.upsert(_mem("n2", "about acme university"))
    kb.link_entity(m1.id, src.node.id, MENTIONS)
    kb.link_entity(m2.id, tgt.node.id, MENTIONS)

    survivor = kb.merge_instances(src.node.id, tgt.node.id)

    assert survivor is not None and survivor.node.id == tgt.node.id
    assert "Acme" in survivor.aliases                        # source name kept as alias
    assert kb.get_entity(src.node.id) is None                   # source tombstoned
    mentioners = {m.id for m in kb.memories_linked(tgt.node.id, link_type=MENTIONS)}
    assert {m1.id, m2.id} <= mentioners                         # both memories now mention target


def test_merge_instances_rejects_non_instance(kb):
    cls = kb.upsert_entity_class("People")
    inst = kb.upsert_entity("Avery")
    assert kb.merge_instances(cls.node.id, inst.node.id) is None


def test_merge_instances_preserves_human_is_a(kb):
    """Human-origin class link must win over derived when folding source into target."""
    cls = kb.upsert_entity_class("Organization", active=True)
    src = kb.upsert_entity("OrgA")
    tgt = kb.upsert_entity("OrgB")
    # Give target a derived link to the class
    with kb._pool.connection() as conn:
        conn.execute(
            """INSERT INTO entity_class_links (instance_id, class_id, weight, origin)
               VALUES (%s, %s, 1.0, 'derived')
               ON CONFLICT (instance_id, class_id) DO UPDATE SET origin = 'derived'""",
            (tgt.node.id, cls.node.id))
    # Give source a human link to the same class
    with kb._pool.connection() as conn:
        conn.execute(
            """INSERT INTO entity_class_links (instance_id, class_id, weight, origin)
               VALUES (%s, %s, 1.0, 'human')
               ON CONFLICT (instance_id, class_id) DO UPDATE SET origin = 'human'""",
            (src.node.id, cls.node.id))

    survivor = kb.merge_instances(src.node.id, tgt.node.id)

    assert survivor is not None
    with kb._pool.connection() as conn:
        row = conn.execute(
            "SELECT origin FROM entity_class_links WHERE instance_id = %s AND class_id = %s",
            (tgt.node.id, cls.node.id)).fetchone()
    assert row is not None
    assert row["origin"] == "human", f"expected 'human', got {row['origin']!r}"


def test_merge_instances_carries_source_aliases_through_a_chain(kb):
    """A -> B then B -> C: C must keep A's name too. Only the source's canonical name used to
    be carried, so every earlier name in a chain was dropped."""
    a = kb.upsert_entity("tenancy model")
    b = kb.upsert_entity("multi-tenancy")
    c = kb.upsert_entity("multi-tenant architecture")

    kb.merge_instances(a.node.id, b.node.id)
    survivor = kb.merge_instances(b.node.id, c.node.id)

    assert {"tenancy model", "multi-tenancy"} <= set(survivor.aliases)


def test_merge_instances_never_aliases_the_survivor_to_its_own_name(kb):
    src = kb.upsert_entity("Initrode Corp")
    tgt = kb.upsert_entity("Initrode")
    with kb._pool.connection() as conn:
        conn.execute("UPDATE entities SET aliases = ARRAY['initrode'] WHERE node_id = %s",
                     (src.node.id,))

    survivor = kb.merge_instances(src.node.id, tgt.node.id)

    assert survivor.aliases == ["Initrode Corp"]


def test_merge_instances_carries_link_roles(kb):
    src = kb.upsert_entity("Globex Labs")
    tgt = kb.upsert_entity("Globex")
    m = kb.upsert(_mem("g1", "Globex Labs profile"))
    kb.link_entity(m.id, src.node.id, MENTIONS, weight=0.9, role="subject")

    kb.merge_instances(src.node.id, tgt.node.id)

    with kb._pool.connection() as conn:
        row = conn.execute("SELECT role FROM entity_links WHERE memory_id = %s AND entity_id = %s",
                           (m.id, tgt.node.id)).fetchone()
    assert row["role"] == "subject"
