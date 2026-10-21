import pytest

from evals.concepts.co_membership import membership, pos_recall, null_mean, score


def _comm(key, memories, entities=()):
    members = [(m, "memory", 1.0) for m in memories] + [(e, "entity", 1.0) for e in entities]
    return {"key": key, "level": 0, "parent_key": None, "members": members}


def test_membership_ignores_entity_members():
    """Entities are graph vertices and land in communities too, but the judge is about
    memories co-clustering."""
    memb = membership([_comm("c0", ["m1", "m2"], entities=["e1"])])
    assert memb == {"m1": {"c0"}, "m2": {"c0"}}


def test_membership_records_multi_membership():
    """A memory can sit in a level-0 concept and its level-1 child."""
    memb = membership([_comm("c0", ["m1"]), _comm("c0.1", ["m1"])])
    assert memb["m1"] == {"c0", "c0.1"}


def test_pos_recall_all_together_is_one():
    memb = membership([_comm("c0", ["m1", "m2", "m3"])])
    assert pos_recall({("m1", "m2"), ("m1", "m3")}, memb) == 1.0


def test_pos_recall_all_apart_is_zero():
    memb = membership([_comm("c0", ["m1"]), _comm("c1", ["m2"])])
    assert pos_recall({("m1", "m2")}, memb) == 0.0


def test_pos_recall_counts_a_memory_in_no_concept_as_apart():
    memb = membership([_comm("c0", ["m1"])])
    assert pos_recall({("m1", "orphan")}, memb) == 0.0


def test_pos_recall_pair_together_if_they_share_any_concept():
    memb = membership([_comm("c0", ["m1", "m2"]), _comm("c1", ["m1"]), _comm("c2", ["m2"])])
    assert pos_recall({("m1", "m2")}, memb) == 1.0


def test_null_destroys_the_signal_but_keeps_the_shape():
    """Relabelling preserves community sizes; a perfectly-satisfied partition should
    score near chance once identities are shuffled across a big universe."""
    memb = membership([_comm("c0", ["m1", "m2"])])
    universe = [f"m{i}" for i in range(1, 41)]
    assert pos_recall({("m1", "m2")}, memb) == 1.0
    assert null_mean({("m1", "m2")}, memb, universe, n_perm=400, seed=42) < 0.15


def test_null_is_deterministic_for_a_given_seed():
    memb = membership([_comm("c0", ["m1", "m2"])])
    universe = [f"m{i}" for i in range(1, 21)]
    a = null_mean({("m1", "m2")}, memb, universe, n_perm=100, seed=7)
    b = null_mean({("m1", "m2")}, memb, universe, n_perm=100, seed=7)
    assert a == b


def test_lift_is_zero_for_one_giant_concept():
    """The degeneracy guard that replaces the dropped negative arm: a partition that puts
    everything together scores pos_recall 1.0 AND null 1.0, so lift collapses to 0."""
    universe = [f"m{i}" for i in range(1, 11)]
    memb = membership([_comm("c0", universe)])
    out = score({("m1", "m2"), ("m3", "m4")}, memb, universe, n_perm=200, seed=42)
    assert out["pos_recall"] == 1.0
    assert out["lift"] == pytest.approx(0.0, abs=1e-9)


def test_score_reports_pair_count_and_hits():
    memb = membership([_comm("c0", ["m1", "m2"]), _comm("c1", ["m3"])])
    out = score({("m1", "m2"), ("m1", "m3")}, memb, ["m1", "m2", "m3"], n_perm=50)
    assert out["n_pairs"] == 2 and out["hits"] == 1
