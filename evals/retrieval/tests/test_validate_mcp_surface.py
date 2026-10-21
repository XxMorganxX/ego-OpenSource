"""Pure-function tests for the mcp_surface set rules: no DB. Run from KB_MCP's venv:
   cd KB_MCP && uv run pytest ../evals/retrieval/tests/test_validate_mcp_surface.py -q"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import validate as v

OLD = datetime(2026, 7, 10, tzinfo=timezone.utc)    # before cutoff
NEW = datetime(2026, 7, 25, tzinfo=timezone.utc)    # after cutoff
DEAD = datetime(2026, 7, 28, tzinfo=timezone.utc)


def _meta(ingested, dead=None):
    return {"ingested_at": ingested, "dead_at": dead}


def _item(iid, must_ids, negatives=True, retired_ok=None):
    it = {"id": iid, "archetype": "person-facts", "request": f"q {iid}",
          "judgment": "j",
          "expected": {"memories": [{"id": m, "must": True} for m in must_ids]}}
    if negatives:
        it["negatives"] = [{"id": "00000000-0000-0000-0000-00000000dead",
                            "why": "distractor"}]
    if retired_ok is not None:
        it["retired_ok"] = retired_ok
    return it


def _passing_set():
    """5 items: 2 with post-cutoff must-gold (40%), 3 retired_ok with dead gold."""
    meta = {"m1": _meta(NEW), "m2": _meta(NEW), "m3": _meta(OLD, DEAD),
            "m4": _meta(OLD, DEAD), "m5": _meta(OLD, DEAD)}
    items = [_item("ms-001", ["m1"]), _item("ms-002", ["m2"]),
             _item("ms-003", ["m3"], retired_ok=True),
             _item("ms-004", ["m4"], retired_ok=True),
             _item("ms-005", ["m5"], retired_ok=True)]
    return items, meta


def test_passing_set_is_clean():
    errors, warnings = v.mcp_surface_checks(*_passing_set())
    assert errors == [] and warnings == []


def test_bad_id_prefix_is_error():
    items, meta = _passing_set()
    items[0]["id"] = "ga-001"
    errors, _ = v.mcp_surface_checks(items, meta)
    assert any("ga-001" in e and "ms-NNN" in e for e in errors)


def test_missing_negatives_is_error():
    items, meta = _passing_set()
    del items[0]["negatives"]
    errors, _ = v.mcp_surface_checks(items, meta)
    assert any("ms-001" in e and "negative" in e for e in errors)


def test_new_content_floor_enforced():
    items, meta = _passing_set()
    meta["m1"] = _meta(OLD)          # drops post-cutoff coverage to 1/5 = 20%
    errors, _ = v.mcp_surface_checks(items, meta)
    assert any("post-2026-07-19" in e for e in errors)


def test_retired_floor_enforced():
    items, meta = _passing_set()
    items.pop()                       # only 2 retired_ok items remain
    meta["m1"] = _meta(NEW)
    errors, _ = v.mcp_surface_checks(items, meta)
    assert any("retired-note items" in e for e in errors)


def test_retired_ok_without_dead_gold_is_error():
    items, meta = _passing_set()
    meta["m3"] = _meta(OLD)           # ms-003 claims retired_ok but gold is live
    errors, _ = v.mcp_surface_checks(items, meta)
    assert any("ms-003" in e and "retired_ok" in e for e in errors)


def test_unflagged_dead_gold_is_warning_not_error():
    items, meta = _passing_set()
    items.append(_item("ms-006", ["m6"]))
    meta["m6"] = _meta(OLD, DEAD)     # drifted: retired after authoring, no flag
    errors, warnings = v.mcp_surface_checks(items, meta)
    assert not any("ms-006" in e for e in errors)
    assert any("ms-006" in w for w in warnings)
