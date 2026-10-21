from kb_core.domain import NodeRecord
from kb_core.entities import ENTITY_INSTANCE
from kb_core.ingest.promotion import EntityPromotionWorker


def _mem(eid):
    return NodeRecord(source="agent", external_id=eid, type="note", content="c", slug=eid)


def test_memories_for_entity_unions_links_and_pending(kb):
    kb.upsert_entity_class("organization", active=True)
    m0, m1, m2 = (kb.upsert(_mem(f"p{i}")) for i in range(3))
    kb.stage_entity_candidate(m0.id, "organization", "Vandelay")
    kb.stage_entity_candidate(m1.id, "organization", "Vandelay")
    EntityPromotionWorker(kb, threshold=2, class_floor=0).run()
    kb.stage_entity_candidate(m2.id, "organization", "Vandelay")     # pending
    inst = next(e for e in kb.list_entities(entity_type=ENTITY_INSTANCE)
                if e.canonical_name == "Vandelay")
    got = {n.id for n in kb.memories_for_entity(inst.node.id, "organization", "Vandelay")}
    assert got == {m0.id, m1.id, m2.id}


def _recrawl_pending(kb, memory_id):
    with kb._pool.connection() as conn:
        row = conn.execute(
            "SELECT status FROM node_processing WHERE node_id = %s AND task = 'recrawl'",
            (memory_id,)).fetchone()
    return row is not None and row["status"] == "pending"


def _promote_instance(kb, name, *eids, category="organization"):
    kb.upsert_entity_class(category, active=True)
    mems = [kb.upsert(_mem(e)) for e in eids]
    for m in mems:
        kb.stage_entity_candidate(m.id, category, name)
    EntityPromotionWorker(kb, threshold=2, class_floor=0).run()
    inst = next(e for e in kb.list_entities(entity_type=ENTITY_INSTANCE)
                if e.canonical_name == name)
    return inst, mems


def test_reparent_instance_requeues_mentioners(kb):
    kb.upsert_entity_class("project", active=True)
    inst, mems = _promote_instance(kb, "Vandelay", "q0", "q1")
    kb.reparent_instance(inst.node.id, "project")
    assert all(_recrawl_pending(kb, m.id) for m in mems)


def test_add_remove_instance_class_requeue(kb):
    kb.upsert_entity_class("project", active=True)
    inst, mems = _promote_instance(kb, "Acme", "q2", "q3")
    kb.add_instance_class(inst.node.id, "project")
    assert _recrawl_pending(kb, mems[0].id)
    with kb._pool.connection() as conn:
        conn.execute("DELETE FROM node_processing WHERE task = 'recrawl'")
    kb.remove_instance_class(inst.node.id, "project")
    assert _recrawl_pending(kb, mems[0].id)


def test_manual_reparent_survives_reconcile_sweep(kb):
    # The human-set type sticks through a full promotion+reconcile sweep (authoritative).
    inst, _ = _promote_instance(kb, "Vandelay", "q4", "q5", category="organization")
    kb.upsert_entity_class("project", active=True)
    kb.reparent_instance(inst.node.id, "project")
    assert kb.classes_of(inst.node.id)[0].canonical_name == "project"
    EntityPromotionWorker(kb, threshold=2, class_floor=0).run()          # reconcile runs
    names = [c.canonical_name for c in kb.classes_of(inst.node.id)]
    assert "project" in names                                            # human type survived


def test_class_merge_recrawls_instance_mentioners(kb):
    kb.upsert_entity_class("organization", active=True)
    inst, mems = _promote_instance(kb, "Vandelay", "s0", "s1", category="org")
    kb.merge_classes("org", "organization")
    assert all(_recrawl_pending(kb, m.id) for m in mems)
