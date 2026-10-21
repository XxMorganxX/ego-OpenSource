from evals.concepts.arms import blocked_mention_rows


def _cand(mid, name, conf=0.9):
    return {"memory_id": mid, "name": name, "confidence": conf}


LIVE = {f"m{i}" for i in range(20)}


def test_recurring_blocked_name_becomes_a_pseudo_entity():
    cands = [_cand(f"m{i}", "lantern") for i in range(3)]
    rows = blocked_mention_rows(cands, LIVE, min_memories=3)
    assert len(rows) == 3
    assert {r["entity_id"] for r in rows} == {"blocked:lantern"}
    assert {r["memory_id"] for r in rows} == {"m0", "m1", "m2"}


def test_support_is_distinct_memory_count_and_shared_by_every_row():
    """build_graph assumes support is constant per entity, matching
    concept_mention_edges' count(*) OVER (PARTITION BY entity_id)."""
    cands = [_cand(f"m{i}", "lantern") for i in range(4)]
    rows = blocked_mention_rows(cands, LIVE, min_memories=3)
    assert {r["support"] for r in rows} == {4}


def test_a_name_below_the_recurrence_floor_is_skipped():
    """Only names recurring in >=10 memories are simulated -- the rest cost the graph no
    meaningful linkage. (How many names qualify drifts with the corpus; the threshold
    does not.)"""
    cands = [_cand("m0", "rare"), _cand("m1", "rare")]
    assert blocked_mention_rows(cands, LIVE, min_memories=3) == []


def test_duplicate_candidate_rows_collapse_to_one_edge_at_max_confidence():
    cands = [_cand("m0", "lantern", 0.6), _cand("m0", "lantern", 0.9),
             _cand("m1", "lantern"), _cand("m2", "lantern")]
    rows = blocked_mention_rows(cands, LIVE, min_memories=3)
    assert len(rows) == 3
    assert next(r for r in rows if r["memory_id"] == "m0")["weight"] == 0.9


def test_dead_memories_are_excluded_before_the_recurrence_test():
    """A name kept alive only by dead memories must not qualify."""
    cands = [_cand("m0", "lantern"), _cand("m1", "lantern"), _cand("ghost", "lantern")]
    assert blocked_mention_rows(cands, LIVE, min_memories=3) == []


def test_rows_are_ordered_deterministically():
    cands = [_cand(f"m{i}", "lantern") for i in (2, 0, 1)]
    rows = blocked_mention_rows(cands, LIVE, min_memories=3)
    assert [r["memory_id"] for r in rows] == ["m0", "m1", "m2"]


import pytest

from evals.concepts.arms import level0_count, match_resolution, partition


def _fixture_graph_inputs():
    """Three clean blobs joined by nothing -- resolution should control how many
    communities Leiden reports."""
    mention, knn = [], []
    for blob in range(3):
        members = [f"b{blob}m{i}" for i in range(5)]
        for m in members:
            mention.append({"memory_id": m, "entity_id": f"b{blob}e",
                            "weight": 0.9, "support": 5})
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                knn.append({"src": members[i], "dst": members[j], "sim": 0.9})
    return mention, knn


def test_level0_count_ignores_children():
    comms = [{"key": "c0", "level": 0, "parent_key": None, "members": []},
             {"key": "c0.1", "level": 1, "parent_key": "c0", "members": []},
             {"key": "c1", "level": 0, "parent_key": None, "members": []}]
    assert level0_count(comms) == 2


def test_match_resolution_hits_the_target_on_a_clean_fixture():
    mention, knn = _fixture_graph_inputs()
    res, count = match_resolution(mention, knn, target=3, entity_alpha=1.0)
    assert count == 3
    assert 0.2 <= res <= 6.0


def test_match_resolution_returns_the_closest_reachable_count():
    """Community count is a step function of resolution -- an unreachable target must
    return the nearest, not loop forever or raise."""
    mention, knn = _fixture_graph_inputs()
    _res, count = match_resolution(mention, knn, target=97, entity_alpha=1.0)
    assert count >= 3


def test_match_resolution_is_deterministic():
    mention, knn = _fixture_graph_inputs()
    a = match_resolution(mention, knn, target=3, entity_alpha=1.0)
    b = match_resolution(mention, knn, target=3, entity_alpha=1.0)
    assert a == b


def test_partition_rejects_a_caller_supplied_seed():
    """Determinism is a correctness requirement; an override must fail loudly."""
    mention, knn = _fixture_graph_inputs()
    with pytest.raises(TypeError, match="seed"):
        partition(mention, knn, entity_alpha=1.0, seed=7)
