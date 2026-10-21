"""Tests for KnowledgeBase.find_merge_candidates — same-kind nearest-neighbour scan."""
from kb_core.entities import merge_pair


def _set_vec(kb, eid, vec):
    """Set entity embedding directly, padding to 1024 dimensions."""
    with kb._pool.connection() as conn:
        conn.execute(
            "UPDATE entities SET embedding = %s, embed_dirty = false WHERE node_id = %s",
            (vec + [0.0] * (1024 - len(vec)), eid),
        )


def test_scan_finds_near_same_kind_pair(kb):
    a = kb.upsert_entity("Acme")
    b = kb.upsert_entity("Acme University")
    _set_vec(kb, a.node.id, [1.0, 0.0, 0.0])
    _set_vec(kb, b.node.id, [0.99, 0.01, 0.0])
    out = kb.find_merge_candidates(max_distance=0.2, top_k=5)
    pairs = {(c["left_id"], c["right_id"]) for c in out}
    assert merge_pair(a.node.id, b.node.id) in pairs


def test_scan_excludes_cross_kind(kb):
    inst = kb.upsert_entity("People")
    cls = kb.upsert_entity_class("People")
    _set_vec(kb, inst.node.id, [1.0, 0.0, 0.0])
    _set_vec(kb, cls.node.id, [1.0, 0.0, 0.0])
    out = kb.find_merge_candidates(max_distance=0.2)
    assert all(not {c["left_id"], c["right_id"]} == {inst.node.id, cls.node.id} for c in out)


def test_scan_excludes_blocked_pair(kb):
    a = kb.upsert_entity("Acme")
    b = kb.upsert_entity("Acme University")
    _set_vec(kb, a.node.id, [1.0, 0.0, 0.0])
    _set_vec(kb, b.node.id, [0.99, 0.01, 0.0])
    # Direct SQL insert of the blocked pair (block_merge arrives in a later task)
    a_id, b_id = a.node.id, b.node.id
    lo, hi = (a_id, b_id) if str(a_id) < str(b_id) else (b_id, a_id)
    with kb._pool.connection() as conn:
        conn.execute(
            "INSERT INTO entity_merge_blocks (left_id, right_id, origin) VALUES (%s, %s, 'human')",
            (lo, hi),
        )
    out = kb.find_merge_candidates(max_distance=0.2)
    assert out == [] or all(
        {c["left_id"], c["right_id"]} != {a.node.id, b.node.id} for c in out
    )


def test_name_arm_finds_containment_pair_without_embeddings(kb):
    """'Avery' vs 'Avery Quinn': divergent descriptions keep these far apart in
    embedding space — the lexical arm must catch them with NO embeddings at all."""
    a = kb.upsert_entity("Avery", categories=["person"])
    b = kb.upsert_entity("Avery Quinn", categories=["person"])
    out = kb.find_merge_candidates()
    match = [c for c in out if {c["left_id"], c["right_id"]} == {a.node.id, b.node.id}]
    assert match and match[0]["via"] == "name"
    assert 0 < match[0]["distance"] <= 1


def test_name_arm_alias_overlap(kb):
    a = kb.upsert_entity("GitHub", aliases=["gh"])
    b = kb.upsert_entity("gh")
    out = kb.find_merge_candidates()
    match = [c for c in out if {c["left_id"], c["right_id"]} == {a.node.id, b.node.id}]
    assert match and match[0]["via"] == "name"


def test_name_arm_respects_blocks_and_dedupes_with_embedding_arm(kb):
    a = kb.upsert_entity("Acme")
    b = kb.upsert_entity("Acme University")
    _set_vec(kb, a.node.id, [1.0, 0.0, 0.0])
    _set_vec(kb, b.node.id, [0.99, 0.01, 0.0])
    out = kb.find_merge_candidates(max_distance=0.2)
    match = [c for c in out if {c["left_id"], c["right_id"]} == {a.node.id, b.node.id}]
    assert len(match) == 1                       # one pair, not one per arm
    assert match[0]["via"] == "embedding"        # embedding arm wins the tag
    kb.block_merge(a.node.id, b.node.id, origin="human")
    out = kb.find_merge_candidates(max_distance=0.2)
    assert all({c["left_id"], c["right_id"]} != {a.node.id, b.node.id} for c in out)
