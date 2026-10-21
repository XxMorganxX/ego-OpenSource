"""Pure-function tests: no DB, no MCP, no network. Run from KB_MCP's venv:
   cd KB_MCP && uv run pytest ../evals/retrieval/tests -q"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bench_metrics as bm


def _item(plan=None):
    return {"id": "x-1", "request": "the question", "archetype": "aboutness",
            "expected": {"memories": []}, "judgment": "j",
            **({"tool_plan": plan} if plan is not None else {})}


def test_substitute_search_memories_step_carries_filters():
    plan = [{"tool": "search_memories",
             "args": {"query": "q", "under": "Obsidian.Meetings", "limit": 20}}]
    out, subs = bm.substitute_plan(_item(plan), "cluster-crawl", "llm")
    assert out[0]["tool"] == "search_graph"
    assert out[0]["args"]["under"] == "Obsidian.Meetings"
    assert out[0]["args"]["strategy"] == "cluster-crawl"
    assert out[0]["args"]["judge"] == "llm"
    assert subs == [{"from": "search_memories", "to": "search_graph",
                     "wants_anchor": False}]


def test_substitute_hybrid_passes_no_judge():
    out, _ = bm.substitute_plan(_item(), "hybrid", "llm")   # default plan
    assert out[0]["tool"] == "search_graph"
    assert "judge" not in out[0]["args"]


def test_substitute_keeps_resolution_steps_and_flags_anchor_wanters():
    plan = [{"tool": "find_entity", "args": {"name": "Vandelay"}},
            {"tool": "entity_activity", "args": {"since": "2026-05-01"}}]
    out, subs = bm.substitute_plan(_item(plan), "cluster-crawl", "none")
    assert out[0] == plan[0]                                # resolution KEPT
    assert out[1]["tool"] == "search_graph"                 # producer SUBSTITUTED
    assert subs[-1]["wants_anchor"] is True                 # executor threads the id
    # the producer keeps no entity_activity-only args, but gains the query
    assert out[1]["args"]["query"] == "the question"


def test_precision_reports_returned_beside_it():
    got = bm.precision_at_k(["a", "b", "c"], must=["a"], may=["z"], k=20)
    assert got == {"precision": 0.05, "returned": 3}


def test_clean_agentic_full_and_fallback_and_nohop():
    ok = {"crawl_mode": "agentic", "llm": "ok",
          "hops": [{"agentic": True}], "degraded": {}}
    fell = {"crawl_mode": "mixed", "llm": "ok",
            "hops": [{"agentic": True}, {"agentic": False}], "degraded": {}}
    nohop = {"crawl_mode": "deterministic", "llm": "unused (no llm hop ran)",
             "hops": [], "degraded": {}}
    walled = {"crawl_mode": "deterministic", "llm": "unavailable (quota, resets 15:00)",
              "hops": [], "degraded": {"llm": "quota"}}
    assert bm.call_is_clean_agentic(ok)
    assert not bm.call_is_clean_agentic(fell)
    assert bm.call_is_clean_agentic(nohop)      # a no-hop crawl is not degraded
    assert not bm.call_is_clean_agentic(walled)
    assert bm.agentic_fraction([ok, fell]) == 0.5
    assert bm.cell_agentic_label(1.0) == "agentic"
    assert bm.cell_agentic_label(0.99) == "MIXED"


def test_degeneration_counts_by_key():
    traces = [{"degraded": {}}, {"degraded": {"embedder": "unavailable"}},
              {"degraded": {"embedder": "unavailable", "entry": "no route"}}]
    got = bm.degeneration(traces)
    assert got["rate"] == round(2 / 3, 3)
    assert got["by_key"] == {"embedder": 2, "entry": 1}


def test_flat_displaced_gold_is_cross_cell():
    hybrid_top = ["g1", "x", "g2"]
    crawl_top = ["x", "g2", "y"]
    assert bm.flat_displaced_gold(hybrid_top, crawl_top,
                                  must=["g1", "g2"], k=3) == ["g1"]


def test_byte_identical_reports_first_divergence():
    a = {"i1": ["a", "b"], "i2": ["c"]}
    b = {"i1": ["a", "b"], "i2": ["d"]}
    same, diffs = bm.byte_identical([a, dict(a)])
    assert same and diffs == []
    same, diffs = bm.byte_identical([a, b])
    assert not same and diffs == ["i2"]
