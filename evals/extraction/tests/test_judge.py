"""Unit tests for judge.py's pure functions. No provider calls, no run directories.

Run with KB_Core's venv (judge.py imports kb_core):
   cd KB_Core && uv run pytest ../evals/extraction/tests -q

`evals/` is deliberately not a package — the house pattern is to put the module dir on
sys.path, same as tests/test_metrics.py alongside this file.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from judge import (  # noqa: E402
    build_prompts,
    parse_verdict,
    pool_candidates,
    score_arms,
)


def _arm(*items):
    """{(set, id): item} from (id, [names]) pairs."""
    return {("gold", i): {"id": i, "set": "gold",
                          "anchors": [{"name": n} for n in names]}
            for i, names in items}


# --- pooling ------------------------------------------------------------------------

def test_pool_is_the_union_across_arms():
    arms = {"a": _arm(("x", ["Avery", "kNN"])), "b": _arm(("x", ["kNN", "Postgres"]))}
    assert sorted(pool_candidates(arms, ("gold", "x"), 0)) == ["Avery", "Postgres", "kNN"]


def test_pool_dedupes_spelling_variants_to_one_candidate():
    """Two arms spelling one anchor differently must be graded once, not twice."""
    arms = {"a": _arm(("x", ["KB_Core"])), "b": _arm(("x", ["kb core"]))}
    assert len(pool_candidates(arms, ("gold", "x"), 0)) == 1


def test_pool_prefers_the_most_common_spelling():
    arms = {"a": _arm(("x", ["KB_Core"])), "b": _arm(("x", ["kb core"])),
            "c": _arm(("x", ["kb core"]))}
    assert pool_candidates(arms, ("gold", "x"), 0) == ["kb core"]


def test_pool_order_is_deterministic_for_a_seed():
    arms = {"a": _arm(("x", ["one", "two", "three", "four", "five"]))}
    assert pool_candidates(arms, ("gold", "x"), 7) == pool_candidates(arms, ("gold", "x"), 7)


def test_pool_shuffles_so_arm_order_cannot_leak_provenance():
    """Alphabetical or insertion order would tell the judge who proposed what."""
    names = [f"anchor {i:02d}" for i in range(20)]
    arms = {"a": _arm(("x", names))}
    assert pool_candidates(arms, ("gold", "x"), 0) != sorted(names)


def test_pool_skips_blank_and_missing_names():
    arms = {"a": _arm(("x", ["Avery", "", "   "]))}
    assert pool_candidates(arms, ("gold", "x"), 0) == ["Avery"]


def test_pool_is_empty_when_no_arm_covered_the_item():
    arms = {"a": _arm(("x", ["Avery"]))}
    assert pool_candidates(arms, ("gold", "other"), 0) == []


# --- verdict parsing ----------------------------------------------------------------

def _reply(verdicts, missing=()):
    return json.dumps({"verdicts": verdicts, "missing": [{"name": m, "reasoning": "r"}
                                                         for m in missing]})


def test_parse_splits_valid_from_invalid():
    reply = _reply([{"name": "Avery", "reasoning": "named", "valid": True},
                    {"name": "the", "reasoning": "incidental", "valid": False}])
    got = parse_verdict(reply, ["Avery", "the"])
    assert got["valid"] == ["Avery"] and got["invalid"] == ["the"]


def test_parse_records_anchors_every_arm_missed():
    got = parse_verdict(_reply([], missing=["pgvector"]), [])
    assert got["missing"] == ["pgvector"]


def test_parse_drops_verdicts_for_names_never_offered():
    """A judge that invents a candidate must not have it counted as a real anchor."""
    reply = _reply([{"name": "Invented", "reasoning": "r", "valid": True}])
    got = parse_verdict(reply, ["Avery"])
    assert got["valid"] == [] and got["unjudged"] == ["Avery"]


def test_parse_leaves_an_omitted_candidate_unjudged_not_invalid():
    """Defaulting an omission to invalid would deflate the proposing arm's precision."""
    reply = _reply([{"name": "Avery", "reasoning": "r", "valid": True}])
    got = parse_verdict(reply, ["Avery", "kNN"])
    assert got["unjudged"] == ["kNN"]
    assert "kNN" not in got["invalid"]


def test_parse_matches_candidates_case_insensitively():
    reply = _reply([{"name": "avery quinn", "reasoning": "r", "valid": True}])
    got = parse_verdict(reply, ["Avery Quinn"])
    assert got["valid"] == ["Avery Quinn"]      # the candidate's own spelling is preserved


def test_parse_ignores_a_repeated_verdict():
    reply = _reply([{"name": "Avery", "reasoning": "r", "valid": True},
                    {"name": "Avery", "reasoning": "r", "valid": False}])
    got = parse_verdict(reply, ["Avery"])
    assert got["valid"] == ["Avery"] and got["invalid"] == []


def test_parse_rejects_malformed_json():
    assert parse_verdict("not json", ["Avery"]) is None
    assert parse_verdict('{"wrong": []}', ["Avery"]) is None


# --- scoring ------------------------------------------------------------------------

def test_precision_counts_only_judged_valid_anchors():
    arms = {"a": _arm(("x", ["Avery", "junk"]))}
    judgments = {("gold", "x"): {"valid": ["Avery"], "invalid": ["junk"],
                                 "missing": [], "unjudged": []}}
    s = score_arms(arms, judgments, {})["a"]
    assert s["precision"] == 0.5


def test_pooled_recall_measures_against_the_union_the_judge_accepted():
    """The arm found one of the two valid anchors in the pool."""
    arms = {"a": _arm(("x", ["Avery"])), "b": _arm(("x", ["Avery", "kNN"]))}
    judgments = {("gold", "x"): {"valid": ["Avery", "kNN"], "invalid": [],
                                 "missing": [], "unjudged": []}}
    scores = score_arms(arms, judgments, {})
    assert scores["a"]["pooled_recall"] == 0.5
    assert scores["b"]["pooled_recall"] == 1.0


def test_a_perfect_arm_scores_one_on_both_axes():
    arms = {"a": _arm(("x", ["Avery"]))}
    judgments = {("gold", "x"): {"valid": ["Avery"], "invalid": [],
                                 "missing": [], "unjudged": []}}
    s = score_arms(arms, judgments, {})["a"]
    assert s["precision"] == 1.0 and s["pooled_recall"] == 1.0 and s["pooled_f1"] == 1.0


def test_extended_recall_counts_anchors_every_arm_missed_against_all_arms():
    arms = {"a": _arm(("x", ["Avery"]))}
    judgments = {("gold", "x"): {"valid": ["Avery"], "invalid": [],
                                 "missing": ["pgvector"], "unjudged": []}}
    s = score_arms(arms, judgments, {})["a"]
    assert s["pooled_recall"] == 1.0          # perfect against what some arm found
    assert s["extended_recall"] == 0.5        # but the judge says half the memory is uncovered


def test_arms_are_scored_only_on_items_they_actually_ran():
    """A resumed arm with fewer items must not be punished for the ones it never saw."""
    arms = {"a": _arm(("x", ["Avery"])), "b": _arm(("x", ["Avery"]), ("y", ["kNN"]))}
    judgments = {("gold", "x"): {"valid": ["Avery"], "invalid": [], "missing": [],
                                 "unjudged": []},
                 ("gold", "y"): {"valid": ["kNN"], "invalid": [], "missing": [],
                                 "unjudged": []}}
    scores = score_arms(arms, judgments, {})
    assert scores["a"]["items_scored"] == 1 and scores["a"]["pooled_recall"] == 1.0
    assert scores["b"]["items_scored"] == 2


def test_gold_scores_are_absent_without_annotations():
    arms = {"a": _arm(("x", ["Avery"]))}
    judgments = {("gold", "x"): {"valid": ["Avery"], "invalid": [], "missing": [],
                                 "unjudged": []}}
    corpus = {("gold", "x"): {"gold_anchors": []}}
    assert "vs_gold" not in score_arms(arms, judgments, corpus)["a"]


def test_gold_scores_appear_once_annotated():
    arms = {"a": _arm(("x", ["Avery", "junk"]))}
    judgments = {("gold", "x"): {"valid": ["Avery"], "invalid": ["junk"], "missing": [],
                                 "unjudged": []}}
    corpus = {("gold", "x"): {"gold_anchors": [{"name": "Avery"}, {"name": "kNN"}]}}
    s = score_arms(arms, judgments, corpus)["a"]
    assert s["vs_gold"]["precision"] == 0.5 and s["vs_gold"]["recall"] == 0.5


# --- prompt construction ------------------------------------------------------------

def test_prompt_embeds_the_live_production_rubric():
    system, _ = build_prompts({}, ["Avery"], "RUBRIC TEXT HERE")
    assert "RUBRIC TEXT HERE" in system


def test_prompt_orders_reasoning_before_the_verdict():
    """Structured output is the judge's only channel — the schema is what forces reasoning."""
    system, _ = build_prompts({}, ["Avery"], "r")
    assert system.index("`reasoning` FIRST") < system.index("Then set `valid`")


def test_prompt_names_every_candidate_and_hides_their_source():
    _, user = build_prompts({"title": "T", "location": "L", "content": "C"},
                            ["Avery", "kNN"], "r")
    assert "- Avery" in user and "- kNN" in user
    for leak in ("gemma", "12b", "26b", "arm", "model a"):
        assert leak not in user.lower()
