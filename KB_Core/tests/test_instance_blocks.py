from kb_core.domain import NodeRecord


def _mem(kb, eid):
    return kb.upsert(NodeRecord(source="agent", external_id=eid, type="note",
                                content="c", slug=eid)).id


def test_blocked_name_is_excluded_from_promotable(kb):
    # Two memories stage the same instance -> it would be promotable at threshold 2.
    for i in range(2):
        kb.stage_entity_candidate(_mem(kb, f"m{i}"), "tool", "build_pick_list")
    assert any(g["normalized_name"] == "build_pick_list"
               for g in kb.promotable_entities(threshold=2))

    kb.block_instance_promotion("build_pick_list", "code symbol", origin="judge")
    assert all(g["normalized_name"] != "build_pick_list"
               for g in kb.promotable_entities(threshold=2))


def test_block_is_idempotent_and_normalizes(kb):
    kb.block_instance_promotion("  Gather Widgets ", "x")
    kb.block_instance_promotion("gather widgets", "y")   # same normalized key
    with kb._pool.connection() as conn:
        n = conn.execute(
            "SELECT count(*) AS n FROM entity_instance_blocks "
            "WHERE normalized_name = 'gather widgets'").fetchone()["n"]
    assert n == 1
