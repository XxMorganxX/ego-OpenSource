"""Scoring is pure — these tests need no DB, no Ollama, and no Claude. Run with KB_Core's
venv (siblings in this package import kb_core):
   cd KB_Core && uv run pytest ../evals/extraction/tests -q

`evals/` is deliberately not a package — the house pattern (see
evals/retrieval/tests/test_bench_metrics.py) is to put the module dir on sys.path.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from metrics import hit_accuracy, match_anchors, normalize, prf


def test_normalize_folds_case_and_punctuation():
    assert normalize("KB_Core") == normalize("kb core")
    assert normalize("Avery's KB") == normalize("averys kb")


def test_exact_match_after_normalization_is_a_hit():
    m = match_anchors(["Entity Resolution"], ["entity resolution"])
    assert len(m["hits"]) == 1
    assert m["false_positives"] == [] and m["misses"] == []


def test_close_variant_is_a_near_miss_not_a_hit():
    m = match_anchors(["retrieval evals"], ["retrieval eval"])
    assert m["hits"] == []
    assert len(m["near_misses"]) == 1


def test_unrelated_prediction_is_a_false_positive():
    m = match_anchors(["kubernetes"], ["entity resolution"])
    assert m["false_positives"] == ["kubernetes"]
    assert m["misses"] == ["entity resolution"]


def test_each_gold_anchor_is_consumed_at_most_once():
    # Two predictions collapsing onto one gold anchor: one hit, one false positive.
    m = match_anchors(["entity resolution", "Entity Resolution"], ["entity resolution"])
    assert len(m["hits"]) == 1
    assert len(m["false_positives"]) == 1


def test_a_near_miss_cannot_steal_a_gold_anchor_an_exact_match_claims():
    # Adversarial ordering: the near-miss candidate comes FIRST. A single interleaved
    # pass would let it consume the only gold anchor and demote the exact match to a
    # false positive. The two-pass structure must not.
    m = match_anchors(["retrieval evals", "retrieval eval"], ["retrieval eval"])
    assert len(m["hits"]) == 1
    assert m["hits"][0][0] == "retrieval eval"
    assert m["false_positives"] == ["retrieval evals"]


def test_prf_counts_near_misses_as_misses():
    m = match_anchors(["retrieval evals", "kb core"], ["retrieval eval", "kb core"])
    s = prf(m)                      # 1 hit, 1 near-miss
    assert s["precision"] == 0.5 and s["recall"] == 0.5


def test_prf_is_zero_when_nothing_predicted():
    s = prf(match_anchors([], ["entity resolution"]))
    assert s == {"precision": 0.0, "recall": 0.0, "f1": 0.0}


def test_perfect_prediction_scores_one():
    s = prf(match_anchors(["a", "b"], ["a", "b"]))
    assert s == {"precision": 1.0, "recall": 1.0, "f1": 1.0}


# --- type / role / occurred_at correctness over matched anchors ---------------------

def _pair(ptypes, gtypes, prole=None, grole=None, pdt=None, gdt=None):
    """One (predicted, gold) aligned anchor pair."""
    return ({"name": "x", "types": list(ptypes), "role": prole, "occurred_at": pdt},
            {"name": "x", "types": list(gtypes), "role": grole, "occurred_at": gdt})


def test_hit_accuracy_scores_exact_type_match():
    a = hit_accuracy([_pair(["person"], ["person"])])
    assert a["n"] == 1 and a["type_exact"] == 1.0 and a["type_jaccard"] == 1.0


def test_hit_accuracy_gives_partial_credit_for_overlapping_types():
    a = hit_accuracy([_pair(["system", "project"], ["system"])])
    assert a["type_exact"] == 0.0
    assert a["type_jaccard"] == 0.5            # |{system}| / |{system, project}|


def test_hit_accuracy_scores_role_only_where_gold_expects_one():
    a = hit_accuracy([_pair(["person"], ["person"], prole="owner", grole="owner"),
                      _pair(["person"], ["person"])])
    assert a["role_n"] == 1 and a["role_accuracy"] == 1.0 and a["role_spurious"] == 0


def test_hit_accuracy_counts_an_unexpected_role_as_spurious():
    a = hit_accuracy([_pair(["concept"], ["concept"], prole="subject")])
    assert a["role_n"] == 0 and a["role_spurious"] == 1


def test_hit_accuracy_scores_a_wrong_role_as_incorrect():
    a = hit_accuracy([_pair(["person"], ["person"], prole="owner", grole="stakeholder")])
    assert a["role_n"] == 1 and a["role_accuracy"] == 0.0


def test_hit_accuracy_scores_occurred_at_where_gold_expects_it():
    a = hit_accuracy([_pair(["meeting"], ["meeting"], pdt="2026-07-15", gdt="2026-07-15")])
    assert a["occurred_at_n"] == 1 and a["occurred_at_accuracy"] == 1.0


def test_hit_accuracy_of_no_hits_is_zero_not_an_error():
    a = hit_accuracy([])
    assert a["n"] == 0 and a["type_jaccard"] == 0.0


# --- proxy metrics (no gold labels needed) -----------------------------------------

from metrics import conditional_fill, confidence_spread, duplicate_rate, percentiles


def _a(name, conf=0.9, types=(), role=None, occurred_at=None):
    return {"name": name, "types": list(types), "confidence": conf,
            "role": role, "occurred_at": occurred_at}


def test_confidence_spread_flags_a_single_level_as_inert():
    s = confidence_spread([_a("a"), _a("b"), _a("c")])
    assert s["levels_used"] == 1
    assert s["top_share"] == 1.0


def test_confidence_spread_reports_share_of_the_dominant_level():
    s = confidence_spread([_a("a", 0.9), _a("b", 0.9), _a("c", 0.9), _a("d", 0.6)])
    assert s["levels_used"] == 2
    assert s["top_share"] == 0.75


def test_confidence_spread_ignores_missing_confidence():
    s = confidence_spread([_a("a", 0.9), _a("b", None)])
    assert s["counts"] == {0.9: 1}


def test_conditional_fill_uses_the_kind_denominator_not_the_total():
    anchors = [_a("Avery", types=["person"], role="owner"),
               _a("Jules", types=["person"]),
               _a("entity resolution", types=["concept"])]
    fill = conditional_fill(anchors, {"person": "person", "concept": "concept"})
    # 1 of 2 person anchors carries a role; the concept anchor is not in the denominator.
    assert fill["person"]["n"] == 2
    assert fill["person"]["role"] == 0.5


def test_conditional_fill_reports_occurred_at_for_events():
    anchors = [_a("Q3 offsite", types=["meeting"], occurred_at="2026-07-15"),
               _a("Q4 offsite", types=["meeting"])]
    fill = conditional_fill(anchors, {"meeting": "event"})
    assert fill["event"]["occurred_at"] == 0.5


def test_duplicate_rate_counts_normalized_collisions():
    assert duplicate_rate([_a("KB Core"), _a("kb core"), _a("other")]) == 1 / 3


def test_duplicate_rate_is_zero_for_distinct_anchors():
    assert duplicate_rate([_a("a"), _a("b")]) == 0.0


def test_percentiles_of_a_known_series():
    p = percentiles(list(range(1, 101)))
    assert p["p50"] == 50 and p["p90"] == 90 and p["p99"] == 99


def test_percentiles_of_empty_series_is_zero():
    assert percentiles([]) == {"p50": 0.0, "p90": 0.0, "p99": 0.0}
