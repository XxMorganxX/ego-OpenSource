from kb_core.entities import STATUS_INACTIVE


def _instance(kb, name, *, pinned=False):
    ent = kb.upsert_entity(name, categories=["tool"])
    if pinned:
        with kb._pool.connection() as conn:
            conn.execute("UPDATE entities SET pinned = true WHERE node_id = %s", (ent.node.id,))
    return ent.node.id


def test_demote_sets_inactive_and_logs(kb):
    nid = _instance(kb, "build_pick_list")
    assert kb.demote_instance(nid) is True
    with kb._pool.connection() as conn:
        st = conn.execute("SELECT status FROM entities WHERE node_id = %s", (nid,)).fetchone()["status"]
        ev = conn.execute("SELECT count(*) AS n FROM ingest_log "
                          "WHERE event = 'node_demoted' AND node_id = %s", (nid,)).fetchone()["n"]
    assert st == STATUS_INACTIVE and ev == 1
    assert kb.demote_instance(nid) is False              # already inactive -> no-op


def test_active_instances_lists_pinned_flag(kb):
    _instance(kb, "lantern_sync")
    _instance(kb, "keep_me", pinned=True)
    rows = {r["name"]: r for r in kb.active_instances()}
    assert set(rows) == {"lantern_sync", "keep_me"}
    assert rows["keep_me"]["pinned"] is True
    assert rows["lantern_sync"]["pinned"] is False
