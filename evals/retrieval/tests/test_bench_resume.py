"""Pure tests for bench.py's resume-safety helpers: no DB, no MCP, no network.
Run from KB_MCP's venv:
   cd KB_MCP && uv run pytest ../evals/retrieval/tests -q"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bench


def test_consolidated_values_empty_dict_is_empty_set():
    assert bench.consolidated_values({}) == set()
    assert bench.consolidated_values([]) == set()


def test_consolidated_values_excludes_none_and_missing():
    items = [
        {"calls": [{"trace_lite": {"consolidated_at": None}}]},  # explicit None
        {"calls": [{"trace_lite": {}}]},                          # key missing
        {"calls": [{}]},                                          # trace_lite missing
        {"calls": []},                                            # no calls at all
        {},                                                       # no "calls" key
    ]
    assert bench.consolidated_values(items) == set()


def test_consolidated_values_two_distinct_values_both_present_stringified():
    items = [
        {"calls": [{"trace_lite": {"consolidated_at": "2026-07-26T21:44:02+00:00"}}]},
        {"calls": [{"trace_lite": {"consolidated_at": "2026-07-27T00:00:00+00:00"}}]},
    ]
    got = bench.consolidated_values(items)
    assert got == {"2026-07-26T21:44:02+00:00", "2026-07-27T00:00:00+00:00"}
    assert all(isinstance(v, str) for v in got)


def test_consolidated_values_over_done_dot_values_like_run_repeat_seeds():
    # run_repeat re-seeds via consolidated_values(done.values()), done being
    # {item_id: rec} loaded from a resume state file.
    done = {"ga-1": {"calls": [{"trace_lite": {"consolidated_at": "t1"}}]},
            "ga-2": {"calls": [{"trace_lite": {"consolidated_at": "t1"}}]},
            "ga-3": {"calls": [{"trace_lite": {"consolidated_at": "t2"}}]}}
    assert bench.consolidated_values(done.values()) == {"t1", "t2"}


def test_write_state_round_trips_and_leaves_no_tmp_file(tmp_path):
    path = tmp_path / "cell1-r1.state.json"
    obj = {"items": {"ga-1": {"top_ids": ["a", "b"]}}}
    bench.write_state(path, obj)
    assert path.exists()
    assert json.loads(path.read_text()) == obj
    assert not path.with_suffix(".tmp").exists()


def test_write_state_replaces_an_existing_file(tmp_path):
    path = tmp_path / "cell1-r1.state.json"
    path.write_text(json.dumps({"items": {"stale": True}}))
    bench.write_state(path, {"items": {"fresh": True}})
    assert json.loads(path.read_text()) == {"items": {"fresh": True}}
    assert not path.with_suffix(".tmp").exists()


def test_ensure_not_error_raises_on_is_error_with_content():
    res = SimpleNamespace(isError=True,
                          content=[SimpleNamespace(text="StrictLLMAbort: boom")])
    with pytest.raises(RuntimeError, match="MCP error"):
        bench.ensure_not_error(res, "search_graph", "ga-1")


def test_ensure_not_error_raises_on_is_error_with_empty_content():
    res = SimpleNamespace(isError=True, content=[])
    with pytest.raises(RuntimeError, match="MCP error"):
        bench.ensure_not_error(res, "search_graph", "ga-1")


def test_ensure_not_error_does_not_raise_when_is_error_false_or_absent():
    bench.ensure_not_error(SimpleNamespace(isError=False, content=[]),
                           "search_graph", "ga-1")
    bench.ensure_not_error(SimpleNamespace(content=[]), "search_graph", "ga-1")
