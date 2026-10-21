"""mcp_trace primitives: the retrieval trace store for the dashboard and offline evals."""

from uuid import UUID, uuid4

from kb_core.domain import NodeRecord

RID = UUID("00000000-0000-0000-0000-00000000abcd")


def _trace(kb, **over):
    kw = dict(tool="search_graph", args={"query": "why was the judge deleted"},
              query="why was the judge deleted", latency_ms=190,
              request_id=RID, session_id="proc-abc", caller=None,
              results=[{"id": "11111111-1111-1111-1111-111111111111",
                        "kind": "memory", "rank": 1, "score": 0.031,
                        "similarity": 0.64, "via": "concept"}],
              result_count=1, confidence_level="strong", best_similarity=0.6425,
              strategy="cluster-crawl", judge="none", crawl_mode="deterministic",
              trace={"stop_reason": "budget", "applied_filters": []},
              llm_calls=0, embedder_ok=True)
    kw.update(over)
    kb.log_trace(**kw)


def test_log_trace_round_trips_scalar_fields(kb):
    _trace(kb)
    rows = kb.trace_recent()
    assert len(rows) == 1
    r = rows[0]
    assert r["tool"] == "search_graph"
    assert r["query"] == "why was the judge deleted"
    assert r["request_id"] == RID
    assert r["session_id"] == "proc-abc"
    assert r["latency_ms"] == 190
    assert r["result_count"] == 1
    assert r["confidence_level"] == "strong"
    assert r["strategy"] == "cluster-crawl"
    assert r["crawl_mode"] == "deterministic"
    assert r["error"] is None
    assert r["judgment"] is None          # unjudged
    assert "results" not in r             # payloads stay behind trace_get
    assert "args" not in r


def test_nullable_fields_default_to_none(kb):
    kb.log_trace(tool="get_memory", args={"id": "x"}, latency_ms=5)
    r = kb.trace_recent()[0]
    assert r["query"] is None and r["target_id"] is None
    assert r["confidence_level"] is None and r["strategy"] is None
    assert r["result_count"] == 0 and r["llm_calls"] == 0
    assert r["embedder_ok"] is True and r["caller"] is None
    assert r["request_id"] is not None    # server-side gen_random_uuid fallback


def test_recent_filters_tool_confidence_and_evals(kb):
    _trace(kb, query="q1")
    _trace(kb, query="q2", tool="search_memories", strategy=None,
           confidence_level="weak")
    _trace(kb, query="q3", caller="eval")
    assert [r["query"] for r in kb.trace_recent()] == ["q2", "q1"]   # evals hidden
    assert [r["query"] for r in kb.trace_recent(include_evals=True)] == \
        ["q3", "q2", "q1"]
    assert [r["query"] for r in kb.trace_recent(tool="search_memories")] == ["q2"]
    assert [r["query"] for r in kb.trace_recent(confidence="weak")] == ["q2"]
    assert [r["query"] for r in kb.trace_recent(limit=1)] == ["q2"]


def test_summary_percentiles_errors_and_distribution(kb):
    for ms, level in [(100, "strong"), (200, "strong"), (300, "weak"), (400, "none")]:
        kb.log_trace(tool="search_memories", args={"query": "q"}, query="q",
                     latency_ms=ms, result_count=3, confidence_level=level,
                     embedder_ok=(level != "none"))
    kb.log_trace(tool="get_memory", args={"id": "bad"}, latency_ms=1,
                 error="badly formed hexadecimal UUID string")
    s = kb.trace_summary(days=7)
    assert s["calls"] == 5                # all traced calls, search + get_memory
    # latency/weak-or-none are search-scoped (tool IN search_memories/search_graph):
    # percentiles over just the four search rows (100/200/300/400), not the 1ms
    # get_memory error row, which would otherwise drag both toward zero.
    assert s["latency_p50_ms"] == 250
    assert s["latency_p90_ms"] == 370
    assert s["weak_or_none"] == 2
    assert s["weak_or_none_rate"] == 0.5   # 2 of the 4 SEARCH calls, not of 5
    assert s["embedder_failures"] == 1
    assert s["errors"] == 1
    assert s["confidence_levels"] == {"strong": 2, "weak": 1, "none": 1}
    # sessionless searches are one-call turns that can never be followed, so they are excluded from the denominator: None, not 0.0
    assert s["followup_rate"] is None


def test_summary_excludes_eval_traffic_by_default(kb):
    _trace(kb)
    _trace(kb, caller="eval")
    assert kb.trace_summary(days=7)["calls"] == 1
    assert kb.trace_summary(days=7, include_evals=True)["calls"] == 2


def test_summary_followup_rate_links_search_to_later_open(kb):
    opened = uuid4()
    ignored = uuid4()
    # search 1: its session later opens a returned id -> followed
    _trace(kb, session_id="s1",
           results=[{"id": str(opened), "kind": "memory", "rank": 1}])
    kb.log_trace(tool="get_memory", args={"id": str(opened)}, latency_ms=5,
                 session_id="s1", target_id=opened, result_count=1,
                 results=[{"id": str(opened), "kind": "memory"}])
    # search 2: its session opens something the search never returned -> not followed
    _trace(kb, session_id="s2",
           results=[{"id": str(uuid4()), "kind": "memory", "rank": 1}])
    kb.log_trace(tool="get_memory", args={"id": str(ignored)}, latency_ms=5,
                 session_id="s2", target_id=ignored, result_count=1)
    s = kb.trace_summary(days=7)
    assert s["followup_rate"] == 0.5


def test_summary_on_empty_window_returns_zeroes_not_errors(kb):
    s = kb.trace_summary(days=7)
    assert s["calls"] == 0
    assert s["latency_p50_ms"] is None
    assert s["followup_rate"] is None
    assert s["errors"] == 0
    assert s["confidence_levels"] == {}
    assert s["turns"] == 0
    assert s["calls_per_turn_median"] is None
    assert s["tokens_per_turn_median"] is None
    assert s["turn_gap_s"] == 90


def test_trace_get_resolves_labels_and_flags_deleted(kb):
    from kb_core.domain import NodeRecord
    node = kb.upsert(NodeRecord(source="agent", external_id="lbl", type="note",
                                content="body", slug="lbl", parent_path="notes",
                                title="Labelled note"))
    ghost = uuid4()
    kb.log_trace(tool="search_memories", args={"query": "q"}, query="q",
                 latency_ms=10, result_count=2,
                 results=[{"id": str(node.id), "kind": "memory", "rank": 1},
                          {"id": str(ghost), "kind": "memory", "rank": 2}])
    tid = kb.trace_recent()[0]["id"]
    t = kb.trace_get(tid)
    assert t["args"] == {"query": "q"}
    assert t["turn_id"] == tid            # a sessionless row is its own opener
    labels = {r["id"]: r["label"] for r in t["results"]}
    assert labels[str(node.id)] == "Labelled note"
    assert labels[str(ghost)] == "(deleted)"
    assert t["judgments"] == []
    assert t["judgment"] is None


def test_trace_get_unknown_id_returns_none(kb):
    assert kb.trace_get(999_999) is None


def test_add_judgment_appends_and_recent_shows_latest(kb):
    _trace(kb)
    tid = kb.trace_recent()[0]["id"]
    kb.add_judgment(tid, verdict="bad", judged_by="avery")
    row = kb.add_judgment(tid, verdict="good", judged_by="avery", notes="re-read")
    assert row["verdict"] == "good" and row["notes"] == "re-read"
    assert kb.trace_recent()[0]["judgment"] == "good"      # latest wins
    detail = kb.trace_get(tid)
    assert detail["judgment"] == "good"                    # same "latest wins" on trace_get
    assert [j["verdict"] for j in detail["judgments"]] == ["good", "bad"]
    assert [r["query"] for r in kb.trace_recent(judged=True)] == \
        ["why was the judge deleted"]
    assert kb.trace_recent(judged=False) == []


def test_add_judgment_validates(kb):
    _trace(kb)
    tid = kb.trace_recent()[0]["id"]
    import pytest
    with pytest.raises(ValueError):
        kb.add_judgment(tid, verdict="meh", judged_by="avery")
    with pytest.raises(LookupError):
        kb.add_judgment(999_999, verdict="good", judged_by="avery")


def test_result_bytes_round_trip_and_derive_tokens(kb):
    _trace(kb, result_bytes=3500)
    r = kb.trace_recent()[0]
    assert r["result_bytes"] == 3500
    assert r["result_tokens"] == 1000            # 3500 bytes / 3.5
    detail = kb.trace_get(r["id"])
    assert detail["result_bytes"] == 3500
    assert detail["result_tokens"] == 1000


def test_unmeasured_payload_reads_as_none_not_zero(kb):
    # Every row written before 0042 looks like this. 0 would be a lie: it means
    # "this call returned an empty payload", and it would poison every median.
    kb.log_trace(tool="get_memory", args={"id": "x"}, latency_ms=5)
    r = kb.trace_recent()[0]
    assert r["result_bytes"] is None
    assert r["result_tokens"] is None
    assert kb.trace_get(r["id"])["result_tokens"] is None


def test_summary_token_aggregates_ignore_unmeasured_rows(kb):
    _trace(kb, result_bytes=3500, result_count=2)
    _trace(kb, result_bytes=10500, result_count=8)
    _trace(kb, result_bytes=None, result_count=5)      # a pre-0042 row
    s = kb.trace_summary(days=7)
    assert s["calls"] == 3                # every call still counts as a call
    assert s["tokens_measured"] == 2      # ...but only two carry a measurement
    assert s["tokens_median"] == 2000     # median(3500, 10500) = 7000 B / 3.5
    assert s["tokens_total"] == 4000      # (3500 + 10500) / 3.5
    assert s["tokens_per_result"] == 400  # 14000 B / (2 + 8) rows / 3.5
    assert s["bytes_per_token"] == 3.5


def test_zero_result_calls_push_tokens_per_result_up(kb):
    # A search that found nothing still spent its envelope. It belongs in the
    # numerator and not the denominator -- this metric prices wasted retrieval,
    # not just verbose retrieval.
    _trace(kb, result_bytes=700, result_count=0)
    _trace(kb, result_bytes=700, result_count=1)
    s = kb.trace_summary(days=7)
    assert s["tokens_per_result"] == 400   # 1400 B over ONE returned row / 3.5


def test_summary_tokens_by_tool_orders_fattest_first(kb):
    _trace(kb, tool="search_graph", result_bytes=10500)
    _trace(kb, tool="get_memory", result_bytes=700, strategy=None,
           confidence_level=None)
    _trace(kb, tool="find_entity", result_bytes=None, strategy=None,
           confidence_level=None)                   # unmeasured -> no row at all
    s = kb.trace_summary(days=7)
    assert [t["tool"] for t in s["tokens_by_tool"]] == ["search_graph", "get_memory"]
    assert s["tokens_by_tool"][0] == {"tool": "search_graph", "calls": 1,
                                      "tokens_median": 3000, "tokens_total": 3000}


def test_summary_token_fields_are_none_on_an_empty_window(kb):
    s = kb.trace_summary(days=7)
    assert s["tokens_measured"] == 0
    assert s["tokens_median"] is None
    assert s["tokens_total"] is None
    assert s["tokens_per_result"] is None      # no division by zero
    assert s["tokens_by_tool"] == []


def _mem(kb, ext, title):
    return kb.upsert(NodeRecord(source="agent", external_id=ext, type="note",
                                content=f"{title} body", slug=ext, title=title))


FULL = {"v": 1, "limit": 2,
        "flat": {"fts_query": "alpha", "or_fallback": False,
                 "fts": [], "knn": [], "fused": [], "embedder": "ok"}}


def test_log_trace_round_trips_replay_and_flags_it(kb):
    a = _mem(kb, "a", "Alpha")
    rep = {**FULL, "flat": {**FULL["flat"], "fused": [{"id": str(a.id), "rrf": 0.03}]}}
    _trace(kb, tool="search_memories", replay=rep)
    row = kb.trace_recent()[0]
    assert row["has_replay"] is True
    out = kb.trace_replay(row["id"])
    assert out["completeness"] == "full"
    assert out["replay"] == rep
    assert out["labels"][str(a.id)] == {"title": "Alpha", "path": a.path,
                                        "doc_kind": None, "deleted": False}


def test_trace_get_never_ships_the_replay_payload(kb):
    _trace(kb, replay=FULL)
    detail = kb.trace_get(kb.trace_recent()[0]["id"])
    assert "replay" not in detail
    assert detail["has_replay"] is True


def test_trace_replay_labels_a_deleted_memory(kb):
    gone = "22222222-2222-2222-2222-222222222222"
    rep = {**FULL, "flat": {**FULL["flat"], "fused": [{"id": gone, "rrf": 0.01}]}}
    _trace(kb, tool="search_memories", replay=rep)
    out = kb.trace_replay(kb.trace_recent()[0]["id"])
    assert out["labels"][gone] == {"title": "(deleted)", "path": None,
                                   "doc_kind": None, "deleted": True}


def test_trace_replay_partial_for_a_pre_capture_search_memories(kb):
    a = _mem(kb, "a", "Alpha")
    _trace(kb, tool="search_memories", args={"query": "alpha", "limit": 7},
           results=[{"id": str(a.id), "kind": "memory", "rank": 1, "similarity": 0.8}],
           trace={"query_vector": "qwen3-v1"})
    out = kb.trace_replay(kb.trace_recent()[0]["id"])
    assert out["completeness"] == "partial"
    assert out["replay"] == {"v": 1, "limit": 7,
                             "flat": {"fused": [{"id": str(a.id), "sim": 0.8}]}}
    assert "crawl" not in out["replay"]


def test_trace_replay_is_none_when_no_results_were_ever_recorded(kb):
    # 0039-migrated retrieval_log rows have results NULL: synthesizing fused=[] would
    # claim "recorded as empty", which is a lie. results=[] (really empty) stays partial.
    _trace(kb, tool="search_memories", args={"query": "alpha"}, results=None)
    assert kb.trace_replay(kb.trace_recent()[0]["id"]) is None
    _trace(kb, tool="search_memories", args={"query": "alpha"}, results=[], result_count=0)
    assert kb.trace_replay(kb.trace_recent()[0]["id"])["completeness"] == "partial"


def test_trace_replay_partial_search_graph_keeps_entry_and_hop_counts(kb):
    entry = [{"kind": "concept", "id": "c1", "via": "label-cosine",
              "label": "Postgres", "rank": 1}]
    _trace(kb, trace={"entry": entry, "stop_reason": "judge-stop",
                      "hops": [{"hop": 1, "by": "none", "note": "first 12 of 40",
                                "offered": 40, "admitted": 12, "agentic": False}]})
    crawl = kb.trace_replay(kb.trace_recent()[0]["id"])["replay"]["crawl"]
    assert crawl == {"entry": entry, "stop_reason": "judge-stop",
                     "hops": [{"hop": 1, "by": "none", "note": "first 12 of 40",
                               "offered_count": 40, "admitted_count": 12}]}


def test_trace_replay_partial_search_graph_hybrid_strategy_has_no_crawl(kb):
    # search_graph(strategy="hybrid") records an empty crawl trace: no "entry" key
    # means no crawl ran, so none may be invented.
    _trace(kb, trace={"hops": [], "applied_filters": {}})
    assert "crawl" not in kb.trace_replay(kb.trace_recent()[0]["id"])["replay"]


def test_trace_replay_labels_unlabelled_bridge_parents_from_nodes(kb):
    ent = _mem(kb, "e", "Northwind Chess Club")
    m = _mem(kb, "m", "Speaker list")
    rep = {**FULL, "crawl": {"entry": [], "stop_reason": "max-hops", "hops": [
        {"hop": 2, "by": "none", "note": None, "offered_count": 1, "admitted_count": 1,
         "offered": [{"id": str(m.id), "family": "bridge", "admitted": True,
                      "parents": [{"kind": "entity", "id": str(ent.id), "label": None},
                                  {"kind": "entity",
                                   "id": "33333333-3333-3333-3333-333333333333",
                                   "label": None}]}]}]}}
    _trace(kb, replay=rep)
    out = kb.trace_replay(kb.trace_recent()[0]["id"])
    parents = out["replay"]["crawl"]["hops"][0]["offered"][0]["parents"]
    assert [p["label"] for p in parents] == ["Northwind Chess Club", "(deleted)"]
    assert out["labels"][str(m.id)]["title"] == "Speaker list"


def test_trace_replay_is_none_for_unknown_errored_and_non_search_rows(kb):
    assert kb.trace_replay(999_999) is None
    _trace(kb, tool="search_memories", error="boom")
    _trace(kb, tool="get_memory")
    errored, other = sorted(kb.trace_recent(), key=lambda r: r["tool"], reverse=True)
    assert kb.trace_replay(errored["id"]) is None
    assert kb.trace_replay(other["id"]) is None


def _shift(kb, trace_id, seconds):
    """Move a seeded row's END time `seconds` into the past so a later write sees a gap."""
    with kb._pool.connection() as conn:
        conn.execute(
            "UPDATE mcp_trace SET at = now() - %(s)s * interval '1 second' WHERE id = %(id)s",
            {"s": seconds, "id": trace_id})


def _newest_id(kb):
    return kb.trace_recent(include_evals=True)[0]["id"]


def test_a_call_inside_the_gap_joins_the_sessions_open_turn(kb):
    _trace(kb, session_id="s1")
    opener = _newest_id(kb)
    _shift(kb, opener, 30)                        # ended 30 s ago, inside TURN_GAP_S
    kb.log_trace(tool="get_memory", args={"id": "x"}, latency_ms=5, session_id="s1")
    second, first = kb.trace_recent()
    assert first["id"] == opener and first["turn_id"] == opener    # opener points at itself
    assert second["turn_id"] == opener


def test_a_call_past_the_gap_opens_a_new_turn(kb):
    _trace(kb, session_id="s1")
    opener = _newest_id(kb)
    _shift(kb, opener, 200)                       # ended 200 s ago, past TURN_GAP_S
    kb.log_trace(tool="get_memory", args={"id": "x"}, latency_ms=5, session_id="s1")
    second, first = kb.trace_recent()
    assert second["turn_id"] == second["id"]
    assert second["turn_id"] != first["turn_id"]


def test_the_gap_is_measured_from_previous_end_to_next_start(kb):
    _trace(kb, session_id="s1")
    opener = _newest_id(kb)
    _shift(kb, opener, 100)                       # ended 100 s ago
    # A slow call that STARTED 80 s ago (latency 20 s) is inside the gap even though
    # its end is 100 s after the opener's end.
    kb.log_trace(tool="search_memories", args={"query": "q"}, query="q",
                 latency_ms=20_000, session_id="s1")
    assert kb.trace_recent()[0]["turn_id"] == opener


def test_sessions_never_share_a_turn(kb):
    _trace(kb, session_id="s1")
    _trace(kb, session_id="s2")
    b, a = kb.trace_recent()
    assert a["turn_id"] == a["id"] and b["turn_id"] == b["id"]
    assert a["turn_id"] != b["turn_id"]


def test_sessionless_rows_each_stand_alone(kb):
    kb.log_trace(tool="get_memory", args={"id": "x"}, latency_ms=5)
    kb.log_trace(tool="get_memory", args={"id": "y"}, latency_ms=5)
    b, a = kb.trace_recent()
    assert a["turn_id"] == a["id"] and b["turn_id"] == b["id"]


def test_a_concurrent_burst_in_one_session_lands_in_one_turn(kb):
    from concurrent.futures import ThreadPoolExecutor

    def write(i):
        kb.log_trace(tool="search_memories", args={"query": str(i)}, query=str(i),
                     latency_ms=1, session_id="burst")

    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(write, range(8)))
    rows = kb.trace_recent(limit=20)
    assert len(rows) == 8
    assert len({r["turn_id"] for r in rows}) == 1
    assert rows[-1]["turn_id"] == rows[-1]["id"]     # the oldest row is the opener


def test_concurrent_sessions_get_their_own_turns(kb):
    from concurrent.futures import ThreadPoolExecutor

    def write(i):
        kb.log_trace(tool="search_memories", args={"query": str(i)}, query=str(i),
                     latency_ms=1, session_id=f"sess-{i % 2}")

    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(write, range(8)))
    rows = kb.trace_recent(limit=20)
    by_session = {}
    for r in rows:
        by_session.setdefault(r["session_id"], set()).add(r["turn_id"])
    assert len(by_session) == 2
    assert all(len(turns) == 1 for turns in by_session.values())
    assert by_session["sess-0"] != by_session["sess-1"]


def test_rebuild_turns_agrees_with_the_write_path_and_reassigns_edited_history(kb):
    _trace(kb, session_id="s1")
    a = _newest_id(kb)
    _shift(kb, a, 200)                 # a ended 200 s ago
    _trace(kb, session_id="s1")        # b: 200 s after a -> opens
    b = _newest_id(kb)
    _shift(kb, b, 30)                  # b ended 30 s ago
    _trace(kb, session_id="s1")        # c: 30 s after b -> joins b
    c = _newest_id(kb)
    kb.log_trace(tool="get_memory", args={"id": "x"}, latency_ms=5)   # sessionless
    before = {r["id"]: r["turn_id"] for r in kb.trace_recent()}
    assert before[a] == a and before[b] == b and before[c] == b
    assert kb.rebuild_turns() == 0     # the batch rule reproduces the incremental one
    assert {r["id"]: r["turn_id"] for r in kb.trace_recent()} == before
    # Push c past the gap after the fact: only the batch rule can see that now.
    with kb._pool.connection() as conn:
        conn.execute("UPDATE mcp_trace SET at = at + interval '10 minutes' WHERE id = %(id)s",
                     {"id": c})
    assert kb.rebuild_turns() == 1
    assert {r["id"]: r["turn_id"] for r in kb.trace_recent()}[c] == c
    assert kb.rebuild_turns() == 0     # idempotent


def test_trace_recent_exposes_and_filters_by_turn(kb):
    _trace(kb, session_id="s1")
    opener = _newest_id(kb)
    _trace(kb, session_id="s1")
    _trace(kb, session_id="s2")
    rows = kb.trace_recent(turn_id=opener)
    assert len(rows) == 2 and all(r["turn_id"] == opener for r in rows)
    assert len(kb.trace_recent()) == 3


def test_trace_turns_rolls_a_session_burst_into_one_row(kb):
    opened = uuid4()
    _trace(kb, session_id="s1", query="first question",
           results=[{"id": str(opened), "kind": "memory", "rank": 1}], result_bytes=3500)
    _trace(kb, session_id="s1", tool="search_memories", strategy=None, query="second",
           result_bytes=700)
    kb.log_trace(tool="get_memory", args={"id": str(opened)}, latency_ms=5,
                 session_id="s1", target_id=opened, result_count=1,
                 results=[{"id": str(opened), "kind": "memory"}])
    turns = kb.trace_turns()
    assert len(turns) == 1
    t = turns[0]
    assert t["id"] == kb.trace_recent()[-1]["id"]          # the opener (oldest row)
    assert t["session_id"] == "s1" and t["caller"] is None
    assert t["calls"] == 3
    assert t["tools"] == {"search_graph": 1, "search_memories": 1, "get_memory": 1}
    assert t["first_query"] == "first question"
    assert t["result_bytes"] == 4200 and t["result_tokens"] == 1200   # 4200 / 3.5
    assert t["errors"] == 0
    assert t["outcome"] == "opened"
    assert t["judgment"] is None
    assert t["started_at"] <= t["ended_at"]


def test_trace_turns_outcome_precedence(kb):
    # returned: results came back but nothing was opened
    _trace(kb, session_id="returned")
    # empty: a search with no rows and no error
    _trace(kb, session_id="empty", results=[], result_count=0, confidence_level="none")
    # error: every call raised
    kb.log_trace(tool="get_memory", args={"id": "bad"}, latency_ms=1,
                 session_id="error", error="boom")
    # mixed: one error, then a successful call with no rows -> not ALL errored -> empty
    kb.log_trace(tool="get_memory", args={"id": "bad"}, latency_ms=1,
                 session_id="mixed", error="boom")
    kb.log_trace(tool="list_memories", args={}, latency_ms=1, session_id="mixed",
                 result_count=0)
    # lone: a get_memory split off by a long think still grades on its own call. It
    # returns its own target, so only self-exclusion keeps it from grading `opened`.
    lone_id = uuid4()
    kb.log_trace(tool="get_memory", args={"id": str(lone_id)}, latency_ms=1,
                 session_id="lone", target_id=lone_id, result_count=1,
                 results=[{"id": str(lone_id), "kind": "memory"}])
    by_session = {t["session_id"]: (t["outcome"], t["errors"]) for t in kb.trace_turns()}
    assert by_session == {"returned": ("returned", 0), "empty": ("empty", 0),
                          "error": ("error", 1), "mixed": ("empty", 1),
                          "lone": ("returned", 0)}


def test_trace_turns_filters_by_contained_call_and_hides_evals(kb):
    _trace(kb, session_id="s1", query="graph first")        # opener: search_graph / strong
    _trace(kb, session_id="s1", tool="search_memories", strategy=None,
           query="then memories", confidence_level="weak")
    _trace(kb, session_id="s2", query="only graph")
    _trace(kb, session_id="e1", query="bench", caller="eval")
    # newest ended_at first; eval turn hidden by default
    assert [t["first_query"] for t in kb.trace_turns()] == ["only graph", "graph first"]
    assert [t["first_query"] for t in kb.trace_turns(include_evals=True)][0] == "bench"
    # the opener is search_graph; the turn still matches a filter its FOLLOWER satisfies
    assert [t["first_query"] for t in kb.trace_turns(tool="search_memories")] == ["graph first"]
    assert [t["first_query"] for t in kb.trace_turns(confidence="weak")] == ["graph first"]
    assert [t["first_query"] for t in kb.trace_turns(limit=1)] == ["only graph"]


def test_trace_turns_tokens_are_none_when_no_call_was_measured(kb):
    _trace(kb, session_id="s1")                               # _trace sets no result_bytes
    kb.log_trace(tool="get_memory", args={"id": "x"}, latency_ms=1, session_id="s1")
    t = kb.trace_turns()[0]
    assert t["result_bytes"] is None and t["result_tokens"] is None


def test_trace_turn_returns_calls_chronologically_and_latest_verdict(kb):
    _trace(kb, session_id="s1", query="one")
    opener = _newest_id(kb)
    _trace(kb, session_id="s1", query="two")
    _trace(kb, session_id="s2", query="unjudged")
    s2_opener = _newest_id(kb)
    kb.add_turn_judgment(opener, verdict="bad", judged_by="avery")
    kb.add_turn_judgment(opener, verdict="good", judged_by="blake")
    row = kb.add_turn_judgment(opener, verdict="partial", judged_by="avery", notes="meh")
    assert row["turn_id"] == opener and row["verdict"] == "partial" and row["notes"] == "meh"
    d = kb.trace_turn(opener)
    assert d["id"] == opener and d["calls"] == 2
    assert [c["query"] for c in d["traces"]] == ["one", "two"]          # chronological
    assert all(c["turn_id"] == opener for c in d["traces"])
    assert set(d["traces"][0]) >= {"tool", "latency_ms", "judgment", "has_replay",
                                   "result_tokens"}                   # trace_recent shape
    # latest ACROSS judges, not latest per judge: blake's "good" is not the answer
    assert d["judgment"] == "partial"
    assert [j["verdict"] for j in d["judgments"]] == ["partial", "good", "bad"]
    assert [t["id"] for t in kb.trace_turns(judged=True)] == [opener]
    assert [t["id"] for t in kb.trace_turns(judged=False)] == [s2_opener]
    assert {t["id"]: t["judgment"] for t in kb.trace_turns()}[opener] == "partial"


def test_trace_turn_is_none_for_a_non_opener_or_unknown_id(kb):
    _trace(kb, session_id="s1")
    _trace(kb, session_id="s1")
    follower = _newest_id(kb)
    assert kb.trace_turn(follower) is None
    assert kb.trace_turn(999_999) is None


def test_trace_turn_resolves_an_eval_turn_too(kb):
    _trace(kb, session_id="e1", caller="eval")
    opener = _newest_id(kb)
    assert kb.trace_turn(opener)["caller"] == "eval"


def test_add_turn_judgment_validates(kb):
    import pytest
    _trace(kb, session_id="s1")
    opener = _newest_id(kb)
    _trace(kb, session_id="s1")
    follower = _newest_id(kb)
    with pytest.raises(ValueError):
        kb.add_turn_judgment(opener, verdict="meh", judged_by="avery")
    with pytest.raises(LookupError):
        kb.add_turn_judgment(follower, verdict="good", judged_by="avery")
    with pytest.raises(LookupError):
        kb.add_turn_judgment(999_999, verdict="good", judged_by="avery")


def test_summary_counts_turns_and_per_turn_medians(kb):
    _trace(kb, session_id="s1", result_bytes=3500)
    _trace(kb, session_id="s1", result_bytes=700)
    _trace(kb, session_id="s1")                       # unmeasured call, still counts
    _trace(kb, session_id="s2", result_bytes=7000)
    s = kb.trace_summary(days=7)
    assert s["turns"] == 2
    assert s["calls_per_turn_median"] == 2            # median of (3, 1)
    assert s["tokens_per_turn_median"] == 1600        # median of (4200, 7000) B = 5600 / 3.5
    assert s["turn_gap_s"] == 90


def test_summary_followup_rate_is_per_turn_and_search_sourced(kb):
    opened, other = uuid4(), uuid4()
    # turn 1: a search returned `opened` and the turn later fetched it -> followed
    _trace(kb, session_id="s1", results=[{"id": str(opened), "kind": "memory", "rank": 1}])
    kb.log_trace(tool="get_memory", args={"id": str(opened)}, latency_ms=5,
                 session_id="s1", target_id=opened, result_count=1,
                 results=[{"id": str(opened), "kind": "memory"}])
    # turn 2: an expand (not a search) returned `other`, which was then fetched; the
    # turn's search returned something else -> in the denominator, NOT followed
    kb.log_trace(tool="expand", args={"id": "x"}, latency_ms=5, session_id="s2",
                 result_count=1, results=[{"id": str(other), "kind": "memory", "via": "similar"}])
    _trace(kb, session_id="s2")
    kb.log_trace(tool="get_memory", args={"id": str(other)}, latency_ms=5,
                 session_id="s2", target_id=other, result_count=1)
    # turn 3: no search at all -> not in the denominator
    kb.log_trace(tool="get_memory", args={"id": "y"}, latency_ms=5, session_id="s3")
    # turns 4 + 5: same session, DIFFERENT turns -- the case that tells per-turn scope
    # from per-session scope. The first s4 search returned `late` and ended 200 s ago,
    # past TURN_GAP_S, so the second s4 search opens a new turn; that turn's own search
    # never returned `late`, yet it fetches `late`.
    late = uuid4()
    _trace(kb, session_id="s4", results=[{"id": str(late), "kind": "memory", "rank": 1}])
    _shift(kb, _newest_id(kb), 200)
    _trace(kb, session_id="s4")                       # new turn, returns the default id
    kb.log_trace(tool="get_memory", args={"id": str(late)}, latency_ms=5,
                 session_id="s4", target_id=late, result_count=1)
    # Turns containing a search: s1, s2, s4-first, s4-second (4). Followed: s1 only (1).
    # The s4 fetch sits in the second s4 turn, whose own search never returned `late`;
    # the search that did is in an earlier turn, so per-turn scope says not followed
    # while per-session scope would say followed (2 of 4).
    assert kb.trace_summary(days=7)["followup_rate"] == 0.25
