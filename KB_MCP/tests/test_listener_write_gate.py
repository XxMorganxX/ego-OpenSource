"""Per-listener write gate: the authenticated public listener serves no write tool
unless KB_MCP_PUBLIC_WRITE_TOOLS names it, and never deletes a whole subtree. The
local (unauthenticated, loopback/stdio) listener is unchanged."""

import json

import pytest

from kb_mcp.server import (DEFAULT_PUBLIC_WRITE_RATE, _check_size, MAX_CONTENT_BYTES,
                           LIBRARY_WRITE_TOOLS, WriteRate, WriteRateLimiter,
                           build_server, public_write_rate_from_env,
                           public_write_tools_from_env)

WRITE_TOOLS = {
    "add_memory", "update_memory", "delete_memory", "move_memory",
    "set_importance", "sync_source", "approve_merge", "reject_merge",
}


def _out(result):
    """call_tool returns (content, structured) or, for a dict-returning tool, content."""
    if isinstance(result, tuple):
        return result[1]
    return json.loads(result[0].text)


async def _tool_names(server):
    return {t.name for t in await server.list_tools()}


def _public(kb, **kw):
    return build_server(kb, host="127.0.0.1", port=3199,
                        public_hostname="kb-mcp.example.com", authenticated=True, **kw)


# ---- env parsing --------------------------------------------------------------------

@pytest.mark.parametrize("value", [None, "", "  ", "0", "off", "none", "false", "no"])
def test_unset_or_off_means_no_write_tools(value):
    env = {} if value is None else {"KB_MCP_PUBLIC_WRITE_TOOLS": value}
    assert public_write_tools_from_env(env) == frozenset()


@pytest.mark.parametrize("value", ["all", "ALL", "1", "on", "true", "yes"])
def test_all_means_every_write_tool(value):
    assert public_write_tools_from_env({"KB_MCP_PUBLIC_WRITE_TOOLS": value}) == "all"


def test_a_list_names_individual_write_tools():
    env = {"KB_MCP_PUBLIC_WRITE_TOOLS": " add_memory , update_memory,"}
    assert public_write_tools_from_env(env) == frozenset({"add_memory", "update_memory"})


# ---- the public listener ------------------------------------------------------------

@pytest.mark.anyio
async def test_public_listener_hides_and_refuses_every_write_tool_by_default(kb):
    server = _public(kb)
    names = await _tool_names(server)
    assert not names & WRITE_TOOLS
    assert "search_memories" in names and "get_memory" in names
    with pytest.raises(Exception) as exc:
        await server.call_tool("add_memory", {"content": "x", "title": "t"})
    assert "add_memory" in str(exc.value)
    assert "KB_MCP_PUBLIC_WRITE_TOOLS" in str(exc.value)
    assert kb.list(type="note") == []          # the body never ran
    # reads still work
    _c, structured = await server.call_tool("list_memories", {})
    assert structured["result"] == []


@pytest.mark.anyio
async def test_public_listener_serves_only_the_named_write_tools(kb):
    server = _public(kb, public_write_tools=frozenset({"add_memory"}))
    names = await _tool_names(server)
    assert names & WRITE_TOOLS == {"add_memory"}
    await server.call_tool("add_memory", {"content": "x", "title": "t"})
    with pytest.raises(Exception, match="KB_MCP_PUBLIC_WRITE_TOOLS"):
        await server.call_tool("set_importance", {"id": "00000000-0000-0000-0000-000000000000",
                                                  "value": 1.0})


@pytest.mark.anyio
async def test_public_listener_with_all_writes_still_refuses_a_recursive_delete(kb):
    server = _public(kb, public_write_tools="all")
    assert WRITE_TOOLS <= await _tool_names(server)
    folder = _out(await server.call_tool("add_memory", {"content": "f", "slug": "box",
                                                        "type": "folder"}))
    child = _out(await server.call_tool("add_memory", {"content": "c", "slug": "item",
                                                       "parent_path": "box"}))
    with pytest.raises(Exception, match="recursive"):
        await server.call_tool("delete_memory", {"id": folder["id"], "recursive": True})
    assert kb.get_by_path("box.item") is not None       # nothing was deleted
    out = _out(await server.call_tool("delete_memory", {"id": child["id"]}))
    assert out["deleted"]


def test_an_unknown_write_tool_name_is_refused_at_startup(kb):
    with pytest.raises(ValueError, match="search_memories|no_such_tool"):
        _public(kb, public_write_tools=frozenset({"no_such_tool"}))
    with pytest.raises(ValueError, match="not a write tool"):
        _public(kb, public_write_tools=frozenset({"search_memories"}))


# ---- the local listener is unchanged --------------------------------------------------

@pytest.mark.anyio
async def test_local_listener_keeps_every_write_tool_and_recursive_delete(kb):
    server = build_server(kb)
    assert WRITE_TOOLS <= await _tool_names(server)
    folder = _out(await server.call_tool("add_memory", {"content": "f", "slug": "box",
                                                        "type": "folder"}))
    await server.call_tool("add_memory", {"content": "c", "slug": "item",
                                          "parent_path": "box"})
    out = _out(await server.call_tool("delete_memory", {"id": folder["id"],
                                                        "recursive": True}))
    assert out["deleted"]
    assert kb.get_by_path("box.item") is None


# ---- `all` excludes the library's writes ----------------------------------------

@pytest.mark.anyio
async def test_all_does_not_serve_the_skill_registry_writes(kb):
    server = _public(kb, public_write_tools="all")
    names = await _tool_names(server)
    assert WRITE_TOOLS <= names
    assert not names & LIBRARY_WRITE_TOOLS
    assert {"search_skills", "get_skill"} <= names        # reads stay served
    with pytest.raises(Exception, match="KB_MCP_PUBLIC_LIBRARY_WRITES"):
        await server.call_tool("upsert_skill", {
            "name": "demo", "expected_revision": None, "change_summary": "x",
            "files": {"SKILL.md": "---\nname: demo\ndescription: d\n---\nbody\n"}})


@pytest.mark.anyio
async def test_library_writes_are_served_with_their_own_flag(kb):
    server = _public(kb, public_library_writes=True)
    assert LIBRARY_WRITE_TOOLS <= await _tool_names(server)


def test_naming_library_writes_in_the_list_is_a_startup_error(kb):
    with pytest.raises(ValueError, match="KB_MCP_PUBLIC_LIBRARY_WRITES"):
        _public(kb, public_write_tools=frozenset({"upsert_skill", "delete_skill"}))


def test_the_library_writes_are_real_write_tools(kb):
    server = _public(kb, public_library_writes=True)
    kinds = {r["tool"]: r["kind"] for r in kb.tool_states()}
    assert all(kinds[n] == "write" for n in LIBRARY_WRITE_TOOLS)
    assert server is not None


# ---- per-caller write rate -------------------------------------------------------------

@pytest.mark.parametrize("value", [None, "", "  "])
def test_unset_rate_means_the_default(value):
    env = {} if value is None else {"KB_MCP_PUBLIC_WRITE_RATE": value}
    assert public_write_rate_from_env(env) == DEFAULT_PUBLIC_WRITE_RATE


@pytest.mark.parametrize("value", ["off", "none", "0"])
def test_an_off_word_means_no_rate_limit(value):
    assert public_write_rate_from_env({"KB_MCP_PUBLIC_WRITE_RATE": value}) is None


@pytest.mark.parametrize("value,expected", [
    ("60/hour", WriteRate(60, 3600.0)), ("10/min", WriteRate(10, 60.0)),
    ("5 / s", WriteRate(5, 1.0)), ("200/days", WriteRate(200, 86400.0)),
])
def test_a_rate_is_count_per_unit(value, expected):
    assert public_write_rate_from_env({"KB_MCP_PUBLIC_WRITE_RATE": value}) == expected


@pytest.mark.parametrize("value", ["60", "60/fortnight", "0/hour", "-1/hour", "x/hour"])
def test_a_malformed_rate_fails_startup(value):
    with pytest.raises(ValueError, match="KB_MCP_PUBLIC_WRITE_RATE"):
        public_write_rate_from_env({"KB_MCP_PUBLIC_WRITE_RATE": value})


def test_limiter_refills_and_keeps_callers_apart():
    now = [0.0]
    lim = WriteRateLimiter(WriteRate(2, 60.0), clock=lambda: now[0])
    assert lim.take("a") == 0.0 and lim.take("a") == 0.0
    assert lim.take("a") == pytest.approx(30.0)     # one token every 30 s
    assert lim.take("b") == 0.0                     # another caller has its own bucket
    now[0] = 30.0
    assert lim.take("a") == 0.0
    assert lim.take("a") > 0.0
    now[0] = 1000.0                                  # refill caps at the burst size
    assert [lim.take("a") for _ in range(3)][-1] > 0.0


@pytest.mark.anyio
async def test_public_writes_past_the_rate_are_refused_before_the_body(kb):
    server = _public(kb, public_write_tools="all", public_write_rate=WriteRate(2, 3600.0))
    await server.call_tool("add_memory", {"content": "a", "title": "one"})
    await server.call_tool("add_memory", {"content": "b", "title": "two"})
    with pytest.raises(Exception, match="rate limit"):
        await server.call_tool("add_memory", {"content": "c", "title": "three"})
    assert len(kb.list(type="note")) == 2
    _c, structured = await server.call_tool("list_memories", {})   # reads unaffected
    assert len(structured["result"]) == 2


@pytest.mark.anyio
async def test_refused_writes_spend_no_tokens(kb):
    server = _public(kb, public_write_tools=frozenset({"add_memory"}),
                     public_write_rate=WriteRate(1, 3600.0))
    with pytest.raises(Exception, match="KB_MCP_PUBLIC_WRITE_TOOLS"):
        await server.call_tool("set_importance", {
            "id": "00000000-0000-0000-0000-000000000000", "value": 1.0})
    await server.call_tool("add_memory", {"content": "a", "title": "one"})


@pytest.mark.anyio
async def test_local_listener_ignores_the_rate(kb):
    server = build_server(kb, public_write_rate=WriteRate(1, 3600.0))
    for i in range(3):
        await server.call_tool("add_memory", {"content": "x", "title": f"n{i}"})
    assert len(kb.list(type="note")) == 3


# ---- content cap -----------------------------------------------------------------------

@pytest.mark.anyio
@pytest.mark.parametrize("public", [True, False])
async def test_oversized_content_is_refused_on_both_listeners(kb, public):
    server = _public(kb, public_write_tools="all") if public else build_server(kb)
    big = "x" * (MAX_CONTENT_BYTES + 1)
    with pytest.raises(Exception, match="larger than"):
        await server.call_tool("add_memory", {"content": big, "title": "big"})
    with pytest.raises(Exception, match="larger than"):
        await server.call_tool("add_memory", {"content": "ok", "title": "s", "summary": big})
    assert kb.list(type="note") == []
    node = _out(await server.call_tool("add_memory", {"content": "ok", "title": "small"}))
    with pytest.raises(Exception, match="larger than"):
        await server.call_tool("update_memory", {"id": node["id"], "content": big})
    assert kb.get_by_path("small").content == "ok"


def test_the_cap_counts_utf8_bytes():
    _check_size("content", "é" * (MAX_CONTENT_BYTES // 2))     # exactly at the cap
    _check_size("content", None)
    with pytest.raises(ValueError, match="larger than"):
        _check_size("content", "é" * (MAX_CONTENT_BYTES // 2) + "x")
