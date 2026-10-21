"""The trace seam: every read tool call logs one mcp_trace row -- the
retrieval-shaped tools with a full extractor envelope, the plain read tools
(get_entity, concept, the list_* browsers) with payload cost alone -- including
error calls, and a failing logger never breaks a tool call."""

import asyncio
import json
from uuid import uuid4

import pytest

import kb_mcp.server as kb_mcp_server
from kb_core.domain import NodeRecord
from kb_mcp.server import build_server


def _note(kb, ext, content):
    return kb.upsert(NodeRecord(source="agent", external_id=ext, type="note",
                                content=content, slug=ext, parent_path="notes"))


def _call(server, tool, args):
    return asyncio.run(server.call_tool(tool, args))


def _structured(server, tool, args):
    return asyncio.run(server.call_tool(tool, args))[1]


def test_search_memories_traces_query_results_and_confidence(kb):
    node = _note(kb, "t1", "alpha trace body")
    server = build_server(kb)
    _call(server, "search_memories", {"query": "alpha"})
    rows = kb.trace_recent()
    assert [r["tool"] for r in rows] == ["search_memories"]
    r = rows[0]
    assert r["query"] == "alpha"
    assert r["result_count"] >= 1
    assert r["confidence_level"] == "keyword"   # FTS hit, no embedder in tests
    assert r["embedder_ok"] is False
    assert r["session_id"]                      # per-process fallback at minimum
    assert r["error"] is None
    detail = kb.trace_get(rows[0]["id"])        # payload columns round-trip
    assert detail["args"]["query"] == "alpha"
    assert detail["results"][0]["id"] == str(node.id)
    assert detail["results"][0]["kind"] == "memory"
    assert detail["results"][0]["rank"] == 1


def test_search_graph_traces_strategy_and_crawl_trace(kb):
    _note(kb, "t1", "alpha trace body")
    server = build_server(kb)
    _call(server, "search_graph", {"query": "alpha", "judge": "none"})
    r = kb.trace_recent()[0]
    assert r["tool"] == "search_graph"
    assert r["strategy"] == "cluster-crawl"
    assert r["judge"] == "none"
    assert r["crawl_mode"] == "deterministic"
    detail = kb.trace_get(r["id"])
    assert "applied_filters" in detail["trace"]
    assert detail["results"][0]["via"]          # flat/concept/bridge


def test_find_entity_expand_activity_and_get_memory_trace(kb):
    node = _note(kb, "t1", "alpha trace body")
    server = build_server(kb)
    _call(server, "find_entity", {"name": "alpha"})
    _call(server, "expand", {"id": str(node.id)})
    _call(server, "entity_activity", {"entity_id": str(node.id)})
    _call(server, "get_memory", {"id": str(node.id)})
    rows = kb.trace_recent()
    assert [r["tool"] for r in rows] == [
        "get_memory", "entity_activity", "expand", "find_entity"]
    gm, ea, ex, fe = rows
    assert gm["target_id"] == node.id and gm["query"] is None
    assert gm["result_count"] == 1
    assert ea["target_id"] == node.id
    assert ex["target_id"] == node.id
    assert fe["query"] == "alpha" and fe["target_id"] is None


def test_expand_traces_every_flattened_neighbor(kb):
    """Regression pin: _trace_expand once iterated expand()'s top-level envelope
    (id/kind/label/consolidated_at/groups) instead of result["groups"] -- since
    none of those top-level values is a list, the inner loop never ran and every
    expand call silently traced results=[], result_count=0 no matter what the
    response actually held. A single linked entity is enough to give the
    "entities" group a row; computing `expected` from the real response (rather
    than hard-coding 1) means a regression back to the old iteration fails this
    test instead of passing vacuously."""
    import json
    from kb_core.entities import MENTIONS

    node = _note(kb, "t1", "alpha trace body")
    ent = kb.upsert_entity("Alpha Person", categories=["person"])
    kb.link_entity(node.id, ent.node.id, MENTIONS, weight=0.9)
    server = build_server(kb)

    # expand's return annotation is a bare `dict` (no structuredContent), so the
    # response is parsed off the text content block -- same as
    # tests/test_server.py's test_expand_tool_pivots_from_an_entity.
    raw = _call(server, "expand", {"id": str(node.id)})
    resp = json.loads(raw[0].text)
    expected = sum(len(v) for v in resp["groups"].values() if isinstance(v, list))
    assert expected > 0                      # the linked entity gives this test teeth

    r = kb.trace_recent()[0]
    assert r["tool"] == "expand"
    assert r["result_count"] == expected


def _seed_concept(kb):
    # Same seed as tests/test_server.py's: one memory, one applied concept run.
    mem = kb.upsert(NodeRecord(source="agent", external_id="lc1", type="note",
                               content="report builder tuning", slug="lc1",
                               title="Harness note"))
    run = kb.begin_concept_run({})
    kb.apply_concept_run(run, [
        {"key": "c0", "prev_id": None, "parent_key": None, "level": 0,
         "name": "Report Builder", "kind": "project", "description": "reporting work",
         "members": [(str(mem.id), "memory", 1.0)], "embedding": None}], [])
    kb.finish_concept_run(run, status="done", stats={})
    return next(c for c in kb.active_concepts() if c["name"] == "Report Builder")


def test_every_plain_read_tool_traces_result_bytes(kb):
    """Coverage: the plain read tools -- no retrieval envelope to extract --
    still spend the caller's context, so each records a trace row whose
    result_bytes the seam measured. Replaces test_untraced_tools_write_no_rows,
    which pinned the pre-coverage world where only the six retrieval-shaped
    tools wrote rows and every list_*/get_entity/concept payload was invisible
    to the dashboard's token accounting."""
    _note(kb, "t1", "alpha trace body")
    ent = kb.upsert_entity("Alpha Person", categories=["person"])
    c = _seed_concept(kb)
    server = build_server(kb)
    calls = [
        ("list_memories", {}),
        ("list_doc_kinds", {}),
        ("list_concepts", {}),
        ("list_merge_suggestions", {}),
        ("get_entity", {"id": str(ent.node.id)}),
        ("concept", {"concept_id": str(c["id"])}),
    ]
    for tool, args in calls:
        _call(server, tool, args)
    rows = kb.trace_recent()
    assert [r["tool"] for r in rows] == [t for t, _ in reversed(calls)]
    # Nothing above seeds doc kinds or merge suggestions, so those two browsers
    # return [] -- zero content blocks on the wire, measured as 0 (0 = measured
    # and genuinely empty; NULL = unmeasured). The seeded tools must be > 0.
    empty_ok = {"list_doc_kinds", "list_merge_suggestions"}
    for r in rows:
        assert r["error"] is None
        assert r["result_bytes"] is not None
        assert r["result_tokens"] is not None
        if r["tool"] not in empty_ok:
            assert r["result_bytes"] > 0
            assert r["result_tokens"] > 0


def test_plain_read_rows_carry_an_honest_result_count(kb):
    """trace_summary's tokens_per_result divides window-total bytes by
    window-total result rows, so a plain read row that added bytes while always
    reporting result_count=0 would silently inflate that ratio. A list counts
    its rows, a single-object hit counts 1, a miss counts 0."""
    _note(kb, "t1", "alpha trace body")
    ent = kb.upsert_entity("Alpha Person", categories=["person"])
    server = build_server(kb)
    _contents, structured = _call(server, "list_memories", {})
    n = len(structured["result"])
    assert n >= 1                            # the seeded note gives this teeth
    _call(server, "get_entity", {"id": str(ent.node.id)})
    _call(server, "get_entity", {"id": str(uuid4())})    # a miss returns null
    miss, hit, listed = kb.trace_recent()
    assert listed["result_count"] == n
    assert hit["result_count"] == 1
    assert miss["result_count"] == 0


def test_error_call_writes_error_row_and_still_raises(kb):
    server = build_server(kb)
    with pytest.raises(Exception):
        _call(server, "get_memory", {"id": "not-a-uuid"})
    r = kb.trace_recent()[0]
    assert r["tool"] == "get_memory"
    assert r["error"]                       # UUID parse failure captured
    assert r["result_count"] == 0
    assert kb.trace_get(r["id"])["results"] == []


def test_trace_failure_never_breaks_a_search(kb, monkeypatch):
    _note(kb, "t1", "alpha trace body")
    server = build_server(kb)

    def boom(**_kw):
        raise RuntimeError("trace store down")

    monkeypatch.setattr(kb, "log_trace", boom)
    _content, structured = _call(server, "search_memories", {"query": "alpha"})
    assert structured["results"]            # the search still answered
    assert kb.trace_recent() == []          # and nothing was written


def test_extractor_failure_never_breaks_a_call_and_writes_the_base_row(kb, monkeypatch):
    """A raising extractor is a bug in the trace seam, not the tool body -- the
    tool already succeeded by the time `trace(arg_dict, result)` runs, so that
    call must be guarded exactly like the write itself. Patched BEFORE
    build_server: `@tool(trace=...)` closes over the module-level function
    object at registration time, so patching after build_server would leave the
    already-registered tool holding the original (non-raising) extractor."""
    _note(kb, "t1", "alpha trace body")

    def boom(args, result):
        raise RuntimeError("extractor blew up")

    monkeypatch.setattr(kb_mcp_server, "_trace_search_memories", boom)
    server = build_server(kb)
    _content, structured = _call(server, "search_memories", {"query": "alpha"})
    assert structured["results"]                # the search still answered

    rows = kb.trace_recent()
    assert len(rows) == 1                        # base row written despite the extractor
    r = rows[0]
    assert r["tool"] == "search_memories"
    assert r["query"] == "alpha"
    assert r["error"] is None
    assert r["result_count"] == 0                 # extractor's overrides never applied

    detail = kb.trace_get(r["id"])
    assert detail["results"] == []                # stored NULL, normalized at read time


def test_caller_env_tags_rows(kb, monkeypatch):
    _note(kb, "t1", "alpha trace body")
    monkeypatch.setenv("KB_MCP_CALLER", "eval")
    server = build_server(kb)
    _call(server, "search_memories", {"query": "alpha"})
    assert kb.trace_recent() == []                          # hidden by default
    assert kb.trace_recent(include_evals=True)[0]["caller"] == "eval"


def test_session_id_stable_within_one_server(kb):
    _note(kb, "t1", "alpha trace body")
    server = build_server(kb)
    _call(server, "search_memories", {"query": "alpha"})
    _call(server, "search_memories", {"query": "alpha"})
    a, b = kb.trace_recent()
    assert a["session_id"] == b["session_id"]


def test_calls_through_one_server_share_a_turn(kb):
    _note(kb, "t1", "alpha trace body")
    server = build_server(kb)
    _call(server, "search_memories", {"query": "alpha"})
    _call(server, "get_memory", {"id": str(_note(kb, "t2", "beta trace body").id)})
    newer, older = kb.trace_recent()
    assert older["turn_id"] == older["id"]            # the first call opened the turn
    assert newer["turn_id"] == older["id"]            # the second joined it


def test_payload_size_recorded_across_a_sample_of_response_shapes(kb):
    """Exercises 3 of the 6 traced tools, not all six -- the measurement lives
    in one shared seam (see _payload_bytes / the measurement guard tests
    below), so a search's result list, a single get, and expand's nested
    groups are enough to pin that the seam covers varied response shapes.
    Adding the other three tools would test the same seam again, not more of
    it."""
    node = _note(kb, "t1", "alpha trace body")
    server = build_server(kb)
    _call(server, "search_memories", {"query": "alpha"})
    _call(server, "get_memory", {"id": str(node.id)})
    _call(server, "expand", {"id": str(node.id)})
    rows = kb.trace_recent()
    assert len(rows) == 3
    for r in rows:
        assert r["result_bytes"] > 0
        assert r["result_tokens"] > 0


def test_recorded_size_is_the_text_block_mcp_actually_returned(kb):
    """Pin the serializer coupling (design SS0) against reality rather than by
    re-deriving it -- re-deriving is exactly what went wrong in the spec's first
    draft (+7% from \\uXXXX escaping json.dumps does and pydantic_core does not)."""
    node = _note(kb, "t1", "alpha trace body")
    server = build_server(kb)
    converted = _call(server, "get_memory", {"id": str(node.id)})
    # A tool with an output schema (ours all declare `-> dict[str, Any]`) converts
    # to a (unstructured_content, structured_content) tuple.
    blocks = converted[0] if isinstance(converted, tuple) else converted
    text = "".join(b.text for b in blocks if getattr(b, "type", None) == "text")
    assert text                                     # guard: the pin must have a subject
    assert kb.trace_recent()[0]["result_bytes"] == len(text.encode("utf-8"))


def test_recorded_size_sums_the_per_item_blocks_of_a_list_result(kb):
    """List-returning tools don't produce one text block: _convert_to_content
    flattens a list into one TextContent PER ITEM (each serialized on its own).
    Measuring to_json(whole_list) instead overcounts by the list framing plus
    two extra spaces of indent on every line -- this pins the recorded count to
    the sum of the blocks the caller actually receives."""
    _note(kb, "t1", "alpha trace body")
    _note(kb, "t2", "beta trace body")
    server = build_server(kb)
    converted = _call(server, "list_memories", {})
    blocks = converted[0] if isinstance(converted, tuple) else converted
    texts = [b.text for b in blocks if getattr(b, "type", None) == "text"]
    assert len(texts) >= 2                  # flattening actually happened
    joined = "".join(texts)
    assert kb.trace_recent()[0]["result_bytes"] == len(joined.encode("utf-8"))


def test_a_null_result_measures_zero_bytes(kb):
    """A miss (get_entity on an unknown id) returns None, which FastMCP
    converts to ZERO content blocks -- the caller receives no text at all, so
    the honest measurement is 0, not the 4 bytes of serialized "null". The
    0-vs-NULL distinction is load-bearing: NULL means unmeasured (excluded from
    every token aggregate), 0 means measured and genuinely empty."""
    server = build_server(kb)
    _call(server, "get_entity", {"id": str(uuid4())})
    r = kb.trace_recent()[0]
    assert r["error"] is None
    assert r["result_bytes"] == 0
    assert r["result_tokens"] == 0


def test_a_measurement_failure_keeps_the_call_and_the_extractor_fields(kb, monkeypatch):
    """Measurement has its OWN guard, deliberately not shared with the extractor's:
    a sizing bug must not cost us the quality fields on the same row."""
    _note(kb, "t1", "alpha trace body")
    server = build_server(kb)

    def _boom(*a, **kw):
        raise ValueError("cannot serialize")

    # Patch the SEAM's helper, never pydantic_core.to_json itself: that module
    # object is the very one FastMCP's _convert_to_content uses to build the
    # result's text block, so patching it would break the tool call this test
    # asserts still succeeds -- testing the opposite of the intended property.
    monkeypatch.setattr(kb_mcp_server, "_payload_bytes", _boom)
    _call(server, "search_memories", {"query": "alpha"})    # must NOT raise
    r = kb.trace_recent()[0]
    assert r["result_bytes"] is None
    assert r["result_tokens"] is None
    assert r["confidence_level"] == "keyword"    # extractor fields survived
    assert r["result_count"] >= 1
    assert r["error"] is None                    # the CALL succeeded


def test_an_erroring_tool_records_no_payload_size(kb):
    server = build_server(kb)
    with pytest.raises(Exception):
        _call(server, "get_memory", {"id": "not-a-uuid"})
    r = kb.trace_recent()[0]
    assert r["error"] is not None
    assert r["result_bytes"] is None      # no payload existed to measure


def test_search_graph_trace_carries_a_full_replay(kb):
    node = _note(kb, "t1", "alpha trace body")
    server = build_server(kb)
    _call(server, "search_graph", {"query": "alpha", "judge": "none"})
    out = kb.trace_replay(kb.trace_recent()[0]["id"])
    assert out["completeness"] == "full"
    assert "crawl" in out["replay"]
    assert str(node.id) in {r["id"] for r in out["replay"]["flat"]["fused"]}


def test_search_graph_wire_output_never_carries_the_replay(kb):
    _note(kb, "t1", "alpha trace body")
    out = _structured(build_server(kb), "search_graph", {"query": "alpha"})
    assert "replay" not in out
    assert "fts_query" not in json.dumps(out)


def test_search_memories_trace_carries_a_full_replay_and_unchanged_results(kb):
    node = _note(kb, "t1", "alpha trace body")
    _call(build_server(kb), "search_memories", {"query": "alpha"})
    tid = kb.trace_recent()[0]["id"]
    out = kb.trace_replay(tid)
    assert out["completeness"] == "full"
    assert out["replay"]["flat"]["fused"][0]["id"] == str(node.id)
    assert kb.trace_get(tid)["results"][0] == {
        "id": str(node.id), "kind": "memory", "rank": 1, "similarity": None,
        "label": node.title or node.path}   # trace_get's coalesce(title, path)


def test_search_memories_drops_a_replay_that_disagrees_with_the_rows(kb, monkeypatch):
    _note(kb, "t1", "alpha trace body")
    from kb_retrieval.hybrid import HybridStrategy
    monkeypatch.setattr(HybridStrategy, "capture_replay", lambda self, q: (
        {"v": 1, "limit": 20, "flat": {"fused": [
            {"id": "99999999-9999-9999-9999-999999999999", "rrf": 0.1}]}}, {}))
    _call(build_server(kb), "search_memories", {"query": "alpha"})
    assert kb.trace_replay(kb.trace_recent()[0]["id"])["completeness"] == "partial"


def test_search_memories_survives_a_failing_replay_capture(kb, monkeypatch):
    node = _note(kb, "t1", "alpha trace body")
    from kb_retrieval.hybrid import HybridStrategy

    def boom(self, q):
        raise RuntimeError("arm query failed")
    monkeypatch.setattr(HybridStrategy, "capture_replay", boom)
    out = _structured(build_server(kb), "search_memories", {"query": "alpha"})
    assert out["results"][0]["id"] == str(node.id)
    assert kb.trace_replay(kb.trace_recent()[0]["id"])["completeness"] == "partial"


def test_search_memories_logs_a_degraded_replay_builder(kb, monkeypatch, caplog):
    node = _note(kb, "t1", "alpha trace body")
    import kb_retrieval.replay as replay_mod

    def boom(**_kw):
        raise RuntimeError("builder bug")
    monkeypatch.setattr(replay_mod, "flat_stage", boom)
    with caplog.at_level("WARNING", logger="kb_mcp.server"):
        out = _structured(build_server(kb), "search_memories", {"query": "alpha"})
    assert out["results"][0]["id"] == str(node.id)
    assert "replay capture degraded: RuntimeError: builder bug" in caplog.text
    assert kb.trace_replay(kb.trace_recent()[0]["id"])["completeness"] == "partial"
