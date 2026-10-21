"""retrieval_heatmap: per-memory surfaced/opened counts per time bucket, aggregated
from mcp_trace.results. Read-only over the trace log."""

from datetime import date, datetime, timedelta, timezone

import pytest

from kb_core.domain import NodeRecord


def _note(kb, ext, parent="projects", title=None):
    return kb.upsert(NodeRecord(source="agent", external_id=ext, type="note",
                                content=f"{ext} body", slug=ext, parent_path=parent,
                                title=title if title is not None else ext.title()))


def _hit(kb, pool, tool, ids, *, at=None, caller=None, kind="memory"):
    """Log one trace returning `ids`; optionally backdate it to `at`."""
    kb.log_trace(tool=tool, args={}, latency_ms=5, caller=caller,
                 results=[{"id": str(i), "kind": kind, "rank": n + 1}
                          for n, i in enumerate(ids)],
                 result_count=len(ids))
    if at is not None:
        with pool.connection() as conn:
            conn.execute("UPDATE mcp_trace SET at = %s WHERE id = (SELECT max(id) FROM mcp_trace)",
                         (at,))


def _row(hm, node):
    return next(r for r in hm["rows"] if r["id"] == str(node.id))


def test_splits_surfaced_and_opened(kb, pool):
    a = _note(kb, "alpha")
    _hit(kb, pool, "search_memories", [a.id])
    _hit(kb, pool, "search_graph", [a.id])
    _hit(kb, pool, "get_memory", [a.id])
    r = _row(kb.retrieval_heatmap(), a)
    assert (r["surfaced"], r["opened"]) == (2, 1)
    assert r["title"] == "Alpha"
    assert r["path"] == "projects.alpha"
    assert len(r["cells"]) == 1
    assert r["cells"][0]["surfaced"] == 2 and r["cells"][0]["opened"] == 1


def test_excludes_eval_traffic_by_default(kb, pool):
    a = _note(kb, "alpha")
    _hit(kb, pool, "search_memories", [a.id])
    _hit(kb, pool, "search_memories", [a.id], caller="eval")
    assert _row(kb.retrieval_heatmap(), a)["surfaced"] == 1
    assert _row(kb.retrieval_heatmap(include_evals=True), a)["surfaced"] == 2


def test_ignores_entity_and_concept_rows(kb, pool):
    a = _note(kb, "alpha")
    _hit(kb, pool, "expand", [a.id], kind="entity")
    _hit(kb, pool, "expand", [a.id], kind="concept")
    hm = kb.retrieval_heatmap()
    assert hm["rows"] == [] and hm["total_memories"] == 0 and hm["max_cell"] == 0


def test_a_memory_listed_twice_in_one_call_counts_once(kb, pool):
    a = _note(kb, "alpha")
    _hit(kb, pool, "expand", [a.id, a.id])
    assert _row(kb.retrieval_heatmap(), a)["surfaced"] == 1


def test_path_filter_scopes_to_folder(kb, pool):
    a = _note(kb, "alpha", parent="projects")
    b = _note(kb, "beta", parent="inbox")
    _hit(kb, pool, "search_memories", [a.id, b.id])
    ids = [r["id"] for r in kb.retrieval_heatmap(path="projects")["rows"]]
    assert ids == [str(a.id)]


def test_deleted_memory_drops_out(kb, pool):
    a = _note(kb, "alpha")
    _hit(kb, pool, "search_memories", [a.id])
    kb.delete(a.id)
    assert kb.retrieval_heatmap()["rows"] == []


def test_null_results_and_malformed_ids_are_tolerated(kb, pool):
    a = _note(kb, "alpha")
    kb.log_trace(tool="get_memory", args={}, latency_ms=1, error="boom")   # results NULL
    kb.log_trace(tool="search_memories", args={}, latency_ms=1,
                 results=[{"id": "not-a-uuid", "kind": "memory"}, {"kind": "memory"},
                          {"id": str(a.id), "kind": "memory"}])
    r = _row(kb.retrieval_heatmap(), a)
    assert r["surfaced"] == 1


def test_non_array_results_are_skipped(kb, pool):
    a = _note(kb, "alpha")
    _hit(kb, pool, "search_memories", [a.id])
    kb.log_trace(tool="search_memories", args={}, latency_ms=1,
                 results=[{"id": str(a.id), "kind": "memory"}])
    with pool.connection() as conn:
        conn.execute("UPDATE mcp_trace SET results = '{\"id\": \"x\"}'::jsonb "
                     "WHERE id = (SELECT max(id) FROM mcp_trace)")
    r = _row(kb.retrieval_heatmap(), a)
    assert r["surfaced"] == 1


def test_window_excludes_older_traces(kb, pool):
    a = _note(kb, "alpha")
    _hit(kb, pool, "search_memories", [a.id], at=datetime.now(timezone.utc) - timedelta(days=40))
    assert kb.retrieval_heatmap(days=30)["rows"] == []
    assert _row(kb.retrieval_heatmap(days=None), a)["surfaced"] == 1


def test_timezone_moves_a_late_utc_hit_to_the_next_local_day(kb, pool):
    a = _note(kb, "alpha")
    d = datetime.now(timezone.utc).date() - timedelta(days=3)
    at = datetime(d.year, d.month, d.day, 23, 30, tzinfo=timezone.utc)
    _hit(kb, pool, "search_memories", [a.id], at=at)
    utc = _row(kb.retrieval_heatmap(tz="UTC"), a)["cells"][0]["bucket"]
    tokyo = _row(kb.retrieval_heatmap(tz="Asia/Tokyo"), a)["cells"][0]["bucket"]
    assert utc == d.isoformat()
    assert tokyo == (d + timedelta(days=1)).isoformat()


def test_buckets_cover_window_and_end_today(kb):
    hm = kb.retrieval_heatmap(days=30, tz="UTC")
    assert len(hm["buckets"]) == 30
    assert hm["buckets"][-1] == datetime.now(timezone.utc).date().isoformat()
    assert hm["window"]["start"] == hm["buckets"][0]
    assert hm["window"]["end"] == hm["buckets"][-1]


def test_buckets_are_gap_free_across_dst(kb):
    hm = kb.retrieval_heatmap(days=365, tz="Europe/Berlin")   # any zone with DST
    days = [date.fromisoformat(b) for b in hm["buckets"]]
    assert len(days) == 365 and len(set(days)) == 365
    assert all(b - a == timedelta(days=1) for a, b in zip(days, days[1:]))


def test_week_buckets_start_on_monday(kb, pool):
    a = _note(kb, "alpha")
    _hit(kb, pool, "search_memories", [a.id])
    hm = kb.retrieval_heatmap(days=60, bucket="week", tz="UTC")
    assert all(date.fromisoformat(b).weekday() == 0 for b in hm["buckets"])
    assert _row(hm, a)["cells"][0]["bucket"] in hm["buckets"]


def test_all_history_with_no_traces_returns_current_bucket(kb):
    hm = kb.retrieval_heatmap(days=None, tz="UTC")
    assert hm["buckets"] == [datetime.now(timezone.utc).date().isoformat()]
    assert hm["rows"] == []


def test_limit_orders_by_total_and_reports_pre_limit_count(kb, pool):
    a, b, c = _note(kb, "alpha"), _note(kb, "beta"), _note(kb, "gamma")
    _hit(kb, pool, "search_memories", [a.id, b.id, c.id])
    _hit(kb, pool, "search_memories", [b.id, c.id])
    _hit(kb, pool, "get_memory", [c.id])
    hm = kb.retrieval_heatmap(limit=2)
    assert [r["id"] for r in hm["rows"]] == [str(c.id), str(b.id)]
    assert hm["total_memories"] == 3
    assert hm["max_cell"] == 3          # gamma: 2 surfaced + 1 opened, one day


@pytest.mark.parametrize("kwargs", [
    {"bucket": "month"},
    {"tz": "Not/AZone"},
    {"tz": "../etc/passwd"},
    {"days": 0},
    {"path": "projects; DROP TABLE nodes"},
])
def test_bad_parameters_raise_value_error(kb, kwargs):
    with pytest.raises(ValueError):
        kb.retrieval_heatmap(**kwargs)
