"""Ideas that were built, measured, and found harmful.

This module exists because two strategies can now rediscover the same bad idea
independently. Each entry carries n and a re-test trigger so a future reader can
tell a settled question from a stale one.
"""

from kb_retrieval.negatives import MEASURED_NEGATIVES


def test_every_negative_carries_evidence_and_a_retest_trigger():
    assert MEASURED_NEGATIVES
    for key, neg in MEASURED_NEGATIVES.items():
        assert neg.claim, key
        assert neg.measurement, key
        assert neg.n > 0, key
        assert neg.retest_trigger, key
        assert neg.citation, key


def test_the_four_load_bearing_negatives_are_present():
    for key in ("similarity_floor", "margin_signal", "query_prefix", "p90_hub_gate"):
        assert key in MEASURED_NEGATIVES


def test_load_bearing_measurements_are_pinned_against_silent_drift():
    """A dropped or reversed finding must fail here, or the measurement is folklore."""
    similarity_floor = MEASURED_NEGATIVES["similarity_floor"]
    assert "must_recall fell" in similarity_floor.measurement, "similarity_floor"
    assert "overlap" in similarity_floor.measurement, "similarity_floor"

    margin_signal = MEASURED_NEGATIVES["margin_signal"]
    assert "EXCEEDS the gold median" in margin_signal.measurement, "margin_signal"
    assert "worse than the raw threshold" in margin_signal.measurement, "margin_signal"

    query_prefix = MEASURED_NEGATIVES["query_prefix"]
    assert "top-1 and top-3 both fell" in query_prefix.measurement, "query_prefix"
    assert "LABEL routing only" in query_prefix.measurement, "query_prefix"

    p90_hub_gate = MEASURED_NEGATIVES["p90_hub_gate"]
    assert "Anti-subject, not anti-hub" in p90_hub_gate.measurement, "p90_hub_gate"
