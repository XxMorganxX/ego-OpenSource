# tests/test_concept_persist.py
"""Persisting a consolidation run: id carry-over, member replacement, retirement."""
from kb_core.domain import NodeRecord


def _mem(kb, eid):
    return kb.upsert(NodeRecord(source="agent", external_id=eid, type="note",
                                content="c", slug=eid, title=eid))


def _upsert(key, members, *, prev_id=None, name="KB project", parent_key=None, level=0):
    return {"key": key, "prev_id": prev_id, "parent_key": parent_key, "level": level,
            "name": name, "kind": "project", "description": "engine work",
            "members": members, "embedding": None}


def test_apply_creates_concepts_with_members_and_run(kb, pool):
    m1, m2, m3 = (_mem(kb, e) for e in ("m1", "m2", "m3"))
    run_id = kb.begin_concept_run({"knn_k": 8})
    res = kb.apply_concept_run(run_id, [
        _upsert("c0", [(str(m1.id), "memory", 1.0), (str(m2.id), "memory", 0.8)]),
        _upsert("c1", [(str(m3.id), "memory", 1.0)], name="Other topic")], [])
    kb.finish_concept_run(run_id, status="done", stats={"kept": 2})
    assert res == {"created": 2, "updated": 0, "retired": 0}
    active = kb.active_concepts()
    assert {c["name"] for c in active} == {"KB project", "Other topic"}
    kbp = next(c for c in active if c["name"] == "KB project")
    assert set(map(str, kbp["memory_ids"])) == {str(m1.id), str(m2.id)}
    assert kbp["member_count"] == 2
    with pool.connection() as conn:
        row = conn.execute("SELECT status, stats FROM concept_runs WHERE id = %s",
                           (run_id,)).fetchone()
    assert row["status"] == "done" and row["stats"]["kept"] == 2


def test_apply_carries_id_and_replaces_members(kb):
    m1, m2, m3 = (_mem(kb, e) for e in ("m1", "m2", "m3"))
    r1 = kb.begin_concept_run({})
    kb.apply_concept_run(r1, [
        _upsert("c0", [(str(m1.id), "memory", 1.0), (str(m2.id), "memory", 1.0)])], [])
    cid = kb.active_concepts()[0]["id"]
    r2 = kb.begin_concept_run({})
    res = kb.apply_concept_run(r2, [
        _upsert("c9", [(str(m1.id), "memory", 1.0), (str(m3.id), "memory", 0.7)],
                prev_id=cid, name="KB project v2")], [])
    assert res == {"created": 0, "updated": 1, "retired": 0}
    active = kb.active_concepts()
    assert len(active) == 1 and active[0]["id"] == cid            # id stable
    assert active[0]["name"] == "KB project v2"                   # label refreshed
    assert set(map(str, active[0]["memory_ids"])) == {str(m1.id), str(m3.id)}


def test_apply_retires_unmatched_and_links_parents(kb, pool):
    m1, m2, m3 = (_mem(kb, e) for e in ("m1", "m2", "m3"))
    r1 = kb.begin_concept_run({})
    kb.apply_concept_run(r1, [_upsert("c0", [(str(m1.id), "memory", 1.0)])], [])
    old_id = kb.active_concepts()[0]["id"]
    r2 = kb.begin_concept_run({})
    res = kb.apply_concept_run(r2, [
        _upsert("p", [(str(m2.id), "memory", 1.0), (str(m3.id), "memory", 1.0)],
                name="Parent"),
        _upsert("p.0", [(str(m2.id), "memory", 1.0)], name="Child",
                parent_key="p", level=1)], [old_id])
    assert res["retired"] == 1                                    # one row actually flipped
    assert {c["name"] for c in kb.active_concepts()} == {"Parent", "Child"}
    with pool.connection() as conn:
        retired = conn.execute("SELECT status FROM concepts WHERE id = %s",
                               (old_id,)).fetchone()
        child = conn.execute(
            "SELECT p.name AS parent FROM concepts c JOIN concepts p ON p.id = c.parent_id "
            "WHERE c.name = 'Child'").fetchone()
    assert retired["status"] == "retired"                         # retired, never deleted
    assert child["parent"] == "Parent"

    r3 = kb.begin_concept_run({})
    res2 = kb.apply_concept_run(r3, [], [old_id])
    assert res2["retired"] == 0                                   # idempotent no-op reported truthfully


def test_child_whose_parent_was_dropped_is_promoted_to_a_root(kb, pool):
    """A community's level-1 children are separate upsert entries carrying parent_key.
    run_once drops a community when its label comes back incoherent or its previous
    concept was dismissed -- but its children are still appended, still pointing at the
    key that is now absent. Persisting such a child as level > 0 with a NULL parent makes
    it UNREACHABLE in the dashboard, which lists roots (level 0, no parent) and then each
    root's children (non-null parent): a parentless level-1 concept matches neither.
    Such concepts were invisible this way. A child with no surviving parent IS a
    root, so it must be re-levelled rather than persisted into that dead zone."""
    m1, m2 = (_mem(kb, e) for e in ("m1", "m2"))
    run = kb.begin_concept_run({})
    # "c0" is deliberately absent from upserts -- run_once dropped it this pass.
    kb.apply_concept_run(run, [
        _upsert("c0.1", [(str(m1.id), "memory", 1.0), (str(m2.id), "memory", 1.0)],
                parent_key="c0", level=1, name="Orphaned sub-cluster")], [])
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT level, parent_id FROM concepts WHERE name = %s",
            ("Orphaned sub-cluster",)).fetchone()
    assert row["parent_id"] is None
    assert row["level"] == 0, "no surviving parent means this concept is a root"


def test_a_real_parent_child_pair_keeps_its_nesting(kb, pool):
    """The re-levelling above must not flatten legitimate hierarchy: when the parent IS
    present, the child keeps level 1 and its parent_id resolves."""
    m1, m2, m3 = (_mem(kb, e) for e in ("m1", "m2", "m3"))
    run = kb.begin_concept_run({})
    kb.apply_concept_run(run, [
        _upsert("c0", [(str(m1.id), "memory", 1.0), (str(m2.id), "memory", 1.0)],
                name="Parent topic"),
        _upsert("c0.1", [(str(m3.id), "memory", 1.0)],
                parent_key="c0", level=1, name="Child topic")], [])
    with pool.connection() as conn:
        rows = {r["name"]: r for r in conn.execute(
            "SELECT name, id, level, parent_id FROM concepts WHERE status = 'active'")}
    assert rows["Parent topic"]["level"] == 0
    assert rows["Parent topic"]["parent_id"] is None
    assert rows["Child topic"]["level"] == 1
    assert rows["Child topic"]["parent_id"] == rows["Parent topic"]["id"]
