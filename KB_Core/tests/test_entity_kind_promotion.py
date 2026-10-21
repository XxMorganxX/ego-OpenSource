"""Promotion-layer entity-signal behavior: alias folding, event threshold, kind derivation."""
from datetime import datetime, timezone

import pytest

from kb_core.domain import NodeRecord


@pytest.fixture
def two_memory_nodes(kb):
    """Two note nodes to stage candidates against."""
    m1 = kb.upsert(NodeRecord(source="agent", external_id="tekp-m1", type="note",
                              content="c1", slug="tekp-m1"))
    m2 = kb.upsert(NodeRecord(source="agent", external_id="tekp-m2", type="note",
                              content="c2", slug="tekp-m2"))
    return m1.id, m2.id


def _stage(kb, mem_id, category, name, **kw):
    kb.stage_entity_candidate(mem_id, category, name, confidence=0.9, **kw)


def test_alias_fold_links_to_survivor(kb, two_memory_nodes):
    m1, m2 = two_memory_nodes
    a = kb.upsert_entity("Avery Quinn", categories=["person"])
    with kb._pool.connection() as conn:
        conn.execute("UPDATE entities SET aliases = ARRAY['Avery'] WHERE node_id = %s",
                     (a.node.id,))
    _stage(kb, m1, "person", "Avery")
    _stage(kb, m2, "person", "Avery")
    for g in kb.promotable_entities(threshold=2):
        kb.promote_entity(g["entity_type"], g["raw_name"])
    with kb._pool.connection() as conn:
        # no separate 'instance:avery' node was created
        dup = conn.execute(
            "SELECT 1 FROM nodes WHERE source = 'entity' AND external_id = 'instance:avery'"
        ).fetchone()
        links = conn.execute(
            "SELECT count(*) AS c FROM entity_links WHERE entity_id = %s AND link_type = 'mentions'",
            (a.node.id,)).fetchone()
    assert dup is None
    assert links["c"] == 2                      # both mentions folded onto the survivor


def test_event_kind_promotes_at_one_memory(kb, two_memory_nodes):
    m1, _ = two_memory_nodes
    kb.upsert_entity_class("event", active=True)          # seeded kind = event
    ts = datetime(2026, 7, 15, tzinfo=timezone.utc)
    _stage(kb, m1, "event", "Vandelay kickoff sync", occurred_at=ts)
    groups = {g["normalized_name"]: g for g in kb.promotable_entities(threshold=2)}
    assert "vandelay kickoff sync" in groups             # 1 memory suffices for events
    kb.promote_entity("event", "Vandelay kickoff sync")
    with kb._pool.connection() as conn:
        row = conn.execute(
            """SELECT e.occurred_at FROM entities e JOIN nodes n ON n.id = e.node_id
               WHERE n.external_id = 'instance:vandelaykickoffsync'""").fetchone()
    assert row["occurred_at"] == ts


def test_non_event_still_needs_threshold(kb, two_memory_nodes):
    m1, _ = two_memory_nodes
    kb.upsert_entity_class("tool", active=True)
    _stage(kb, m1, "tool", "Solo Tool")
    names = {g["normalized_name"] for g in kb.promotable_entities(threshold=2)}
    assert "solo tool" not in names


def test_role_backfilled_onto_links(kb, two_memory_nodes):
    m1, m2 = two_memory_nodes
    kb.upsert_entity_class("person", active=True)
    _stage(kb, m1, "person", "Jordan Reyes", role="stakeholder")
    _stage(kb, m2, "person", "Jordan Reyes")
    kb.promote_entity("person", "Jordan Reyes")
    with kb._pool.connection() as conn:
        roles = {r["memory_id"]: r["role"] for r in conn.execute(
            """SELECT l.memory_id, l.role FROM entity_links l
               JOIN nodes n ON n.id = l.entity_id
               WHERE n.external_id = 'instance:jordanreyes' AND l.link_type = 'mentions'""").fetchall()}
    assert roles[m1] == "stakeholder"
    assert roles[m2] is None


def test_derive_instance_kinds(kb, two_memory_nodes):
    kb.upsert_entity_class("person", active=True)
    inst = kb.upsert_entity("Casey Lin", categories=["person"])
    changed = kb.derive_instance_kinds()
    assert changed >= 1
    with kb._pool.connection() as conn:
        row = conn.execute("SELECT kind FROM entities WHERE node_id = %s",
                           (inst.node.id,)).fetchone()
    assert row["kind"] == "person"
