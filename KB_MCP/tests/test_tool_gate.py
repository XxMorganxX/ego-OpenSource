"""The tool gate: server-side enable/disable enforced for every MCP client."""

import threading

import pytest

from kb_mcp.server import build_server

WRITE_TOOLS = {
    "add_memory", "update_memory", "delete_memory", "move_memory",
    "set_importance", "sync_source", "approve_merge", "reject_merge",
    "upsert_skill", "delete_skill", "review_skill",
    "upsert_agent", "delete_agent", "review_agent",
}


async def _tool_names(server):
    return {t.name for t in await server.list_tools()}


def test_startup_publishes_every_registered_tool(kb):
    build_server(kb)
    published = {r["tool"] for r in kb.tool_states()}
    assert len(published) == 32
    assert WRITE_TOOLS <= published
    assert "search_memories" in published


def test_write_tools_are_classified_as_writes(kb):
    build_server(kb)
    kinds = {r["tool"]: r["kind"] for r in kb.tool_states()}
    assert {t for t, k in kinds.items() if k == "write"} == WRITE_TOOLS
    # Everything else is a read; nothing is unclassified.
    assert set(kinds.values()) == {"read", "write"}


@pytest.mark.anyio
async def test_disabled_tool_is_hidden_from_list_tools(kb):
    server = build_server(kb)
    assert "add_memory" in await _tool_names(server)
    kb.set_tool_enabled("add_memory", False)
    server._gate.invalidate()
    names = await _tool_names(server)
    assert "add_memory" not in names
    assert "search_memories" in names


@pytest.mark.anyio
async def test_disabled_tool_refuses_when_called_anyway(kb):
    server = build_server(kb)
    kb.set_tool_enabled("add_memory", False)
    server._gate.invalidate()
    with pytest.raises(Exception) as exc:
        await server.call_tool("add_memory", {"content": "x", "title": "t"})
    # The message must name the tool and where to turn it back on.
    assert "add_memory" in str(exc.value)
    assert "disabled" in str(exc.value)


@pytest.mark.anyio
async def test_enabled_tool_still_works(kb):
    server = build_server(kb)
    _contents, structured = await server.call_tool("list_memories", {})
    # A clean DB (the `kb` fixture truncates nodes) means a real, specific result --
    # not just "truthy", which an error payload would also satisfy.
    assert structured["result"] == []


def test_gate_caches_within_its_ttl(kb, monkeypatch):
    # Pin the TTL explicitly: this test's whole premise is "one refresh, ten calls,"
    # which an ambient KB_MCP_GATE_TTL=0 in the environment would silently defeat.
    monkeypatch.setenv("KB_MCP_GATE_TTL", "2")
    server = build_server(kb)
    calls = {"n": 0}
    real = kb.tool_states

    def counted():
        calls["n"] += 1
        return real()

    monkeypatch.setattr(kb, "tool_states", counted)
    server._gate.invalidate()
    for _ in range(10):
        server._gate.enabled("search_memories")
    assert calls["n"] == 1, "the gate must not hit the DB once per tool call"


@pytest.mark.anyio
async def test_exactly_one_fetch_per_decision_at_zero_ttl(kb, monkeypatch):
    """Pins `_decide`'s one-fetch-per-decision invariant at KB_MCP_GATE_TTL=0,
    where a stray second `_refresh` is a real blocking Postgres round trip, not the
    no-op it would be at the default 2 s TTL. Reverting `enabled_async` to
    `return self.enabled(...)`, or `list_tools`'s refresh to a per-tool
    `enabled_async` call, both still ship 36/36 green at the default TTL -- only
    TTL=0 exposes the extra fetch, which is why it must be pinned explicitly.

    Also pins the OTHER axis of the same hazard, not just how many times the gate
    refreshes but WHERE: collapsing `enabled_async` to
    `return self.enabled(name, writes=writes)` (dropping the `await
    refresh_async()`) still costs exactly one fetch per decision -- `enabled`'s
    inline `_refresh` is one fetch too -- so the count assertions above pass
    unchanged while that fetch's blocking psycopg I/O runs on the event loop
    instead of a worker thread, reintroducing the exact hazard `_offload` exists
    to eliminate. `counted()` records the thread each fetch actually ran on;
    `loop_thread`, captured in this coroutine before either call, stands in for
    the event loop's thread the same way `test_a_tool_body_runs_in_a_worker_
    thread_not_on_the_event_loop` uses `seen["loop"]`."""
    monkeypatch.setenv("KB_MCP_GATE_TTL", "0")
    server = build_server(kb)
    loop_thread = threading.get_ident()
    calls = {"n": 0}
    fetch_threads: list[int] = []
    real = kb.tool_states

    def counted():
        calls["n"] += 1
        fetch_threads.append(threading.get_ident())
        return real()

    monkeypatch.setattr(kb, "tool_states", counted)

    await server.call_tool("list_memories", {})
    assert calls["n"] == 1, "one call_tool decision must cost exactly one fetch"
    assert loop_thread not in fetch_threads, (
        "the gate's Postgres fetch ran on the event loop's thread -- the "
        "hazard enabled_async exists to eliminate")

    calls["n"] = 0
    fetch_threads.clear()
    names = await _tool_names(server)
    assert len(names) == 32
    assert calls["n"] == 1, "one list_tools call must cost exactly one fetch"
    assert loop_thread not in fetch_threads, (
        "list_tools's refresh ran on the event loop's thread")


def test_cold_cache_db_failure_keeps_reads_open(kb, monkeypatch):
    server = build_server(kb)
    server._gate.invalidate()
    # build_server never calls .enabled()/.enabled_async(), so the gate is
    # genuinely cold here -- invalidate() alone can't produce this (it only zeroes
    # _fetched_at; _warm stays whatever it was). Assert it instead of poking it, so
    # a future pre-warm silently breaking this test's premise fails loudly.
    assert server._gate._warm is False

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(kb, "tool_states", boom)
    # A database outage must not brick retrieval -- this system's main job.
    assert server._gate.enabled("search_memories", writes=False) is True
    assert server._gate.enabled("get_memory", writes=False) is True


def test_cold_cache_db_failure_refuses_writes(kb, monkeypatch):
    server = build_server(kb)
    server._gate.invalidate()
    assert server._gate._warm is False

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(kb, "tool_states", boom)
    # An outage must not resurrect a deliberately disabled write tool for a remote
    # caller on the public authed listener.
    assert server._gate.enabled("add_memory", writes=True) is False
    assert server._gate.enabled("delete_memory", writes=True) is False


@pytest.mark.anyio
async def test_cold_cache_db_failure_at_the_real_seam(kb, monkeypatch):
    """The four ToolGate-level failure-rule tests above
    only ever call `ToolGate.enabled()` directly, so they never exercise whether
    `writes` actually threads from tool() registration through `_gate_call` and
    through `list_tools`'s `kinds.get(t.name) == "write"` lookup. A gate that
    hardcodes `writes=False` at either seam would pass every test above while
    failing OPEN for all 14 write tools on a cold cache -- exposing them on the
    public authenticated listener during exactly the outage the split rule exists
    to guard against. This test goes through call_tool/list_tools instead of
    ToolGate directly, so a regression at either seam fails here even if
    ToolGate.enabled() itself is untouched."""
    server = build_server(kb)
    # `_tool_manager.list_tools()` reads FastMCP's raw registry, bypassing the gate
    # entirely (unlike `server.list_tools()`/`_tool_names`, which would warm the
    # gate right here via `enabled_async` and defeat this test's cold-cache premise).
    all_names = {t.name for t in server._tool_manager.list_tools()}
    assert len(all_names) == 32

    server._gate.invalidate()
    assert server._gate._warm is False

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(kb, "tool_states", boom)

    # tools/list: only the 18 reads should be visible while the cache is cold and
    # the DB is down.
    visible = await _tool_names(server)
    assert visible == all_names - WRITE_TOOLS

    # call_tool: a write tool must refuse even though nothing disabled it -- the
    # cold-cache default for writes is closed, not open.
    with pytest.raises(Exception) as exc:
        await server.call_tool("add_memory", {"content": "x", "title": "t"})
    assert "add_memory" in str(exc.value)
    assert "disabled" in str(exc.value)


def test_warm_cache_survives_a_db_failure(kb, monkeypatch):
    server = build_server(kb)
    kb.set_tool_enabled("add_memory", False)
    server._gate.invalidate()
    assert server._gate.enabled("add_memory") is False  # warms

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(kb, "tool_states", boom)
    server._gate.invalidate()
    # Last-known-good, not a reset to open.
    assert server._gate.enabled("add_memory") is False


def test_unknown_tool_counts_as_enabled(kb):
    server = build_server(kb)
    assert server._gate.enabled("a_tool_added_after_this_row_set") is True


def test_publish_failure_does_not_prevent_startup(kb, monkeypatch):
    def boom(_tools):
        raise RuntimeError("db down")

    monkeypatch.setattr(kb, "publish_tool_inventory", boom)
    # Advisory: a server that cannot publish its inventory still serves.
    assert build_server(kb) is not None


# ---- gate x trace composition (the mcp_tool_toggle <- mcp_trace merge seam) ----
# Both branches wrapped `tool()`. The layers stack gate -> trace -> offload, so a
# refusal happens before either of the inner layers runs. These pin that order; they
# fail if a later edit nests the gate inside the trace wrapper instead.

@pytest.mark.anyio
async def test_a_disabled_traced_tool_refuses_without_writing_a_trace_row(kb):
    server = build_server(kb)
    kb.set_tool_enabled("search_memories", False)
    server._gate.invalidate()
    with pytest.raises(Exception) as exc:
        await server.call_tool("search_memories", {"query": "alpha"})
    assert "disabled" in str(exc.value)
    # A call that never ran must not appear in the retrieval telemetry -- otherwise
    # every blocked call inflates mcp_trace with a phantom row (and, worse, one
    # carrying a latency and an error that describe the gate, not the search).
    assert kb.trace_recent() == []


@pytest.mark.anyio
async def test_an_enabled_traced_tool_still_writes_exactly_one_trace_row(kb):
    # The complement of the test above: proof the gate layer is transparent when it
    # lets a call through, rather than the row being absent for some other reason.
    server = build_server(kb)
    await server.call_tool("search_memories", {"query": "alpha"})
    assert [r["tool"] for r in kb.trace_recent()] == ["search_memories"]


@pytest.mark.anyio
async def test_schemas_survive_the_gate_layer_on_both_kinds_of_tool(kb):
    # test_server.py's schema guards predate the gate; this one asserts the property
    # specifically across the wrapper chains the merge produced -- gate+offload for a
    # plain write tool, gate+trace+offload for a traced read tool. A broken
    # functools.wraps link collapses the schema to (*args, **kwargs) while the server
    # still starts and still lists the tool, so only the properties catch it.
    server = build_server(kb)
    tools = {t.name: t for t in await server.list_tools()}
    props = tools["add_memory"].inputSchema["properties"]        # writes=True
    assert "content" in props and "kwargs" not in props
    props = tools["search_memories"].inputSchema["properties"]   # trace=...
    assert "query" in props and "kwargs" not in props
