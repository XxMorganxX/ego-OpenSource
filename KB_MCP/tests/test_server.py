import asyncio

from kb_core.domain import NodeRecord
from kb_mcp.server import build_server, unframe


def _rec_server():
    return NodeRecord(source="agent", external_id="srv-imp", type="note",
                      content="importance via tool", slug="srv_imp")


def test_tools_registered(kb):
    server = build_server(kb)
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert names == {
        "add_memory", "search_memories", "search_graph", "get_memory",
        "list_memories", "move_memory", "update_memory", "delete_memory",
        "sync_source", "set_importance", "expand", "list_concepts", "concept",
        "find_entity", "get_entity", "entity_activity", "list_doc_kinds",
        "list_merge_suggestions", "approve_merge", "reject_merge",
        "search_skills", "get_skill", "get_skill_file", "upsert_skill", "delete_skill",
        "search_agents", "get_agent", "upsert_agent", "delete_agent",
        "review_skill", "review_agent", "list_pending_reviews",
    }


def test_find_entity_and_get_entity_answer_who_is_x(kb):
    """The 'who is Sam Rivera?' path: name -> entity hit with description -> full record."""
    server = build_server(kb)
    ent = kb.upsert_entity("Sam Rivera", categories=["person"],
                           aliases=["Sam R"])
    kb.set_entity_description(ent.node.id, "Orchard Labs data engineer.")
    _contents, structured = asyncio.run(
        server.call_tool("find_entity", {"name": "sam rivera"}))
    hit = structured["result"][0]
    assert hit["name"] == "Sam Rivera"
    assert unframe(hit["description"]) == "Orchard Labs data engineer."
    assert "person" in hit["classes"]

    _contents, out = asyncio.run(server.call_tool("get_entity", {"id": hit["id"]}))
    out = out["result"]
    assert out["name"] == "Sam Rivera" and out["aliases"] == ["Sam R"]


def test_search_memories_hybrid_via_injected_embedder(kb):
    """A semantic hit surfaces through the tool when an embedder is wired in, and
    an embedder returning None (inference down) degrades to FTS-only, not an error."""
    vec = [0.0] * 1024
    vec[9] = 1.0
    mem = kb.upsert(NodeRecord(source="agent", external_id="hs1", type="note",
                               content="pgvector rebuild after postgres upgrade",
                               slug="hs1"))
    kb.set_embedding(mem.id, vec, embedded_hash="h")
    server = build_server(kb, embedder=lambda q: vec)
    out = asyncio.run(server.call_tool(
        "search_memories", {"query": "embedding extension broken"}))[1]
    assert out["results"][0]["id"] == str(mem.id)
    assert out["results"][0]["similarity"] > 0.99
    # An exact-vector hit must read as an unambiguous match, not just a top rank.
    assert out["confidence"]["level"] == "strong"
    assert out["embedder"] == "ok"
    # Rows carry no note body; the length is advertised so the agent can choose.
    assert "content" not in out["results"][0]
    assert out["results"][0]["content_chars"] == len(
        "pgvector rebuild after postgres upgrade")
    down = build_server(kb, embedder=lambda q: None)
    out = asyncio.run(down.call_tool(
        "search_memories", {"query": "embedding extension broken"}))[1]
    assert out["results"] == []         # no lexical match, embedder absent -> FTS-only
    assert out["confidence"]["level"] == "none"
    assert out["embedder"] == "unavailable"


def test_entity_activity_tool_returns_timeline(kb):
    """The 'what has X been up to' path: find_entity -> entity_activity."""
    from datetime import datetime, timezone
    server = build_server(kb)
    ent = kb.upsert_entity("Supabase")
    mem = kb.upsert(NodeRecord(source="agent", external_id="ea1", type="note",
                               content="deployed supabase auth", slug="ea1",
                               title="Deploy note",
                               event_at=datetime(2026, 5, 31, tzinfo=timezone.utc)))
    kb.link_entity(mem.id, ent.node.id, "mentions", weight=1.5)
    _contents, structured = asyncio.run(server.call_tool(
        "entity_activity", {"entity_id": str(ent.node.id), "since": "2026-05-01"}))
    row = structured["results"][0]
    assert row["happened_at"].startswith("2026-05-31")
    assert row["time_basis"] == "event"
    assert row["link_type"] == "mentions" and row["weight"] == 1.5
    assert "excerpt" in row and "content" not in row   # timeline rows stay lean
    # the clock contract is stated up front, not left for the caller to derive
    assert (structured["n"], structured["dated"], structured["undated"]) == (1, 1, 0)
    # until is half-open: a window ending on the event day excludes it
    _c, empty = asyncio.run(server.call_tool(
        "entity_activity", {"entity_id": str(ent.node.id), "until": "2026-05-31"}))
    assert empty["results"] == []


def _seed_concept(kb):
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


def test_list_concepts_tool_answers_what_are_my_goals(kb):
    server = build_server(kb)
    c = _seed_concept(kb)
    # list[dict]-annotated tools return (contents, structured) on this MCP version.
    _contents, structured = asyncio.run(
        server.call_tool("list_concepts", {"window_days": 7}))
    mine = next(r for r in structured["result"] if r["name"] == "Report Builder")
    assert mine["kind"] == "project"
    assert mine["activity"]["changed"] == 1          # the seeded memory is fresh
    assert mine["goal"]["current"] is True           # fresh activity -> a current goal
    assert mine["goal"]["priority"] > 0
    assert mine["goal"]["trend"] == "rising"
    assert "members" not in mine                     # list stays light


def test_list_concepts_tool_defaults_window_from_the_policy(kb):
    """No window_days -> the tool must still resolve one from the policy (goal_window_days
    14, the `kb` fixture's Policy() default) rather than silently dropping the goal/activity
    block, which concepts_view's own no-args default (None = skip the facet) would do."""
    server = build_server(kb)
    _seed_concept(kb)
    _contents, structured = asyncio.run(server.call_tool("list_concepts", {}))
    mine = next(r for r in structured["result"] if r["name"] == "Report Builder")
    assert "goal" in mine and "activity" in mine
    assert mine["goal"]["current"] is True           # fresh activity -> a current goal


def test_concept_tool_returns_members_and_records_retrieval(kb):
    import json
    server = build_server(kb)
    c = _seed_concept(kb)
    raw = asyncio.run(server.call_tool("concept", {"concept_id": str(c["id"])}))
    out = json.loads(raw[0].text)
    assert out["name"] == "Report Builder"
    assert [m["label"] for m in out["members"]] == ["Harness note"]
    # the drill-in bumped its members' access -> retrieval recency is now non-null
    assert kb.concept_priority()[str(c["id"])]["last_retrieved_at"] is not None


def test_expand_tool_pivots_from_an_entity(kb):
    """Thin-wrapper check only — ranking/scoring behavior is pinned in KB_Core's
    test_expand.py. Here: the tool resolves the id space and returns the envelope."""
    import json
    from kb_core.entities import MENTIONS

    server = build_server(kb)
    mem = kb.upsert(NodeRecord(source="agent", external_id="ex1", type="note",
                               content="sam rivera deploys", slug="ex1", title="Deploy"))
    ent = kb.upsert_entity("Sam Rivera", categories=["person"])
    kb.link_entity(mem.id, ent.node.id, MENTIONS, weight=0.9)
    raw = asyncio.run(server.call_tool("expand", {"id": str(ent.node.id)}))
    out = json.loads(raw[0].text)
    assert out["kind"] == "entity" and out["label"] == "Sam Rivera"
    assert [m["label"] for m in out["groups"]["memories"]] == ["Deploy"]


def test_expand_tool_entity_kind_reaches_below_top_k(kb):
    """MCP-level regression for the entity_kind fix: entity_kind must reach entities below
    the unfiltered top-k, not just filter whatever survived it. KB_Core's
    test_entity_kind_read.py pins the KnowledgeBase-level behavior in depth; this
    only confirms the tool wrapper passes entity_kind through and the filter
    still finds the below-top-k persons end-to-end."""
    import json
    from kb_core.entities import MENTIONS

    server = build_server(kb)
    mem = kb.upsert(NodeRecord(source="agent", external_id="ex-ek1", type="note",
                               content="team sync", slug="ex-ek1", title="Sync"))
    kb.upsert_entity_class("person", active=True)
    kb.upsert_entity_class("tool", active=True)
    pivot = kb.upsert_entity("PivotTool", categories=["tool"])
    tools = [kb.upsert_entity(f"Tool{i}", categories=["tool"]) for i in range(5)]
    persons = [kb.upsert_entity(f"Person{i}", categories=["person"]) for i in range(2)]
    kb.derive_instance_kinds()
    kb.link_entity(mem.id, pivot.node.id, MENTIONS, weight=0.9)
    for t in tools:
        kb.link_entity(mem.id, t.node.id, MENTIONS, weight=0.9)
    for p in persons:
        kb.link_entity(mem.id, p.node.id, MENTIONS, weight=0.1)

    raw = asyncio.run(server.call_tool("expand", {"id": str(pivot.node.id), "k": 3}))
    out = json.loads(raw[0].text)
    co = out["groups"]["co_entities"]
    assert len(co) == 3 and all(r["entity_kind"] != "person" for r in co)

    raw = asyncio.run(server.call_tool(
        "expand", {"id": str(pivot.node.id), "k": 3, "entity_kind": "person"}))
    out = json.loads(raw[0].text)
    ids = {r["id"] for r in out["groups"]["co_entities"]}
    assert {str(p.node.id) for p in persons} <= ids


def test_set_importance_tool(kb):
    server = build_server(kb)
    node = kb.upsert(_rec_server())
    asyncio.run(server.call_tool("set_importance", {"id": str(node.id), "value": 1.0}))
    assert kb.get(node.id).importance == 2.0


def test_sync_source_tool_ingests_vault(tmp_path, kb, pool):
    from kb_core.adapters.obsidian.adapter import ObsidianAdapter
    vault = tmp_path / "v"
    vault.mkdir()
    (vault / "note.md").write_text("hello world", encoding="utf-8")
    server = build_server(kb, pool=pool, adapters={"obsidian": ObsidianAdapter(vault)})
    asyncio.run(server.call_tool("sync_source", {"source": "obsidian"}))
    assert kb.get_by_path("note") is not None


def test_merge_tools_registered(kb):
    """Three merge tools appear in the tool registry."""
    server = build_server(kb)
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert "list_merge_suggestions" in names
    assert "approve_merge" in names
    assert "reject_merge" in names


def test_reject_merge_tool_side_effect(kb):
    """reject_merge tombstones the candidate so list_merge_suggestions returns []."""
    a = kb.upsert_entity("Acme")
    b = kb.upsert_entity("Acme University")
    left_id, right_id = sorted([a.node.id, b.node.id], key=str)
    cid = kb.stage_merge_candidate(
        left_id, right_id, "instance", 0.1, "same uni", b.node.id
    )
    assert cid != 0, "candidate should have been staged"

    server = build_server(kb)
    asyncio.run(server.call_tool("reject_merge", {"candidate_id": cid, "reason": "no"}))
    assert kb.list_merge_suggestions() == []


def test_approve_merge_tool_side_effect(kb):
    """approve_merge folds source into target; source entity no longer exists."""
    a = kb.upsert_entity("Acme Uni")
    b = kb.upsert_entity("Acme University Full")
    left_id, right_id = sorted([a.node.id, b.node.id], key=str)
    cid = kb.stage_merge_candidate(
        left_id, right_id, "instance", 0.1, "same uni", b.node.id
    )
    assert cid != 0, "candidate should have been staged"
    source_id = left_id if b.node.id == right_id else right_id

    server = build_server(kb)
    result = asyncio.run(server.call_tool("approve_merge", {"candidate_id": cid}))
    # After merge the source entity node should be gone (merged into target)
    assert kb.get(source_id) is None


def test_add_memory_tool_persists(kb):
    server = build_server(kb)
    asyncio.run(server.call_tool(
        "add_memory",
        {"content": "tree structured memory", "title": "First", "parent_path": "projects"},
    ))
    results = kb.search("tree", under="projects")
    assert len(results) == 1
    assert results[0].node.title == "First"


def test_list_merge_suggestions_tool_returns_serializable(kb):
    """list_merge_suggestions must return JSON-safe dicts (no raw UUID objects).

    call_tool returns a list of TextContent objects; each .text is JSON.  We
    parse that JSON and assert every UUID field is a plain string so it can be
    forwarded over the MCP wire without a custom serializer.
    """
    import json
    a = kb.upsert_entity("Acme")
    b = kb.upsert_entity("Acme University")
    left_id, right_id = sorted([a.node.id, b.node.id], key=str)
    cid = kb.stage_merge_candidate(left_id, right_id, "instance", 0.1, "same uni", b.node.id)
    assert cid != 0, "candidate should have been staged"

    server = build_server(kb)
    # call_tool returns list[TextContent]; each item's .text is JSON-encoded suggestion
    raw_contents = asyncio.run(server.call_tool("list_merge_suggestions", {}))

    # Parse every TextContent block back to a Python dict
    suggestions = [json.loads(tc.text) for tc in raw_contents]

    # Find the candidate we staged
    matches = [s for s in suggestions if s["id"] == cid]
    assert len(matches) == 1, f"staged candidate {cid} not found in {suggestions}"
    suggestion = matches[0]

    # left/right ids must be plain strings (UUID stringified), not UUID objects
    assert isinstance(suggestion["left"]["id"], str), "left.id must be a string"
    assert isinstance(suggestion["right"]["id"], str), "right.id must be a string"
    # suggested_target_id must be a string or None (not a raw UUID)
    assert suggestion["suggested_target_id"] is None or isinstance(
        suggestion["suggested_target_id"], str
    ), "suggested_target_id must be a string or None"
    # Confirm the name fields are also present
    assert "name" in suggestion["left"]
    assert "name" in suggestion["right"]


def test_doc_kind_rollup_and_filters(kb):
    from kb_core.domain import NodePatch
    server = build_server(kb)
    for i in range(3):
        n = kb.upsert(NodeRecord(source="agent", external_id=f"mk{i}", type="note",
                                 content="standup sync notes", slug=f"mk{i}"))
        kb.update(n.id, NodePatch(doc_kind="meeting notes"))
    other = kb.upsert(NodeRecord(source="agent", external_id="mko", type="note",
                                 content="standup roadmap plan", slug="mko"))
    kb.update(other.id, NodePatch(doc_kind="implementation plan"))

    _c, out = asyncio.run(server.call_tool("list_doc_kinds", {}))
    assert out["result"] == [
        {"kind": "meeting notes", "count": 3, "established": True}]
    _c, out = asyncio.run(server.call_tool("list_doc_kinds", {"all": True}))
    assert {k["kind"] for k in out["result"]} == {"meeting notes",
                                                  "implementation plan"}

    _c, res = asyncio.run(server.call_tool("list_memories",
                                           {"kind": "meeting notes"}))
    assert len(res["result"]) == 3
    assert {m["doc_kind"] for m in res["result"]} == {"meeting notes"}

    res = asyncio.run(server.call_tool(
        "search_memories", {"query": "standup", "kind": "implementation plan"}))[1]
    assert [m["id"] for m in res["results"]] == [str(other.id)]


# ---- async transport -------------------------------------------------------
# FastMCP calls a SYNC tool body inline on the event loop -- there is no
# threadpool on that path (mcp 1.27.2, FuncMetadata.call_fn_with_arg_validation: `if fn_is_async:
# return await fn(...) else: return fn(...)`). Measured with a 1.0 s body and a
# 20 ms heartbeat: 0 heartbeat ticks fired during the sync call, 47 during the
# offloaded one. So the loop is not slowed by a sync tool, it is stopped, and a
# multi-second cluster-crawl would stop it for that whole duration.
#
# These four tests guard three separate properties, because breaking any one of
# them is silent: (1) nothing is registered sync, (2) the wrapper is transparent
# to everything FastMCP introspects -- asserted twice, once by comparison against
# the unwrapped function and once as a hard pin, so the failure is legible even
# when the comparison mechanism is what broke -- and (3) the body genuinely
# leaves the loop.


def test_every_registered_tool_is_async(kb):
    """A sync tool owns the event loop for its whole duration. There must not be
    one -- not "not many", none. `tool()` in build_server is what makes this
    structurally true; this test is what keeps it true when the next tool is
    added by someone who reaches for @mcp.tool() out of habit."""
    server = build_server(kb)
    blocking = [t.name for t in server._tool_manager.list_tools() if not t.is_async]
    assert blocking == []


def test_the_thread_offload_preserves_every_tools_generated_schema(kb):
    """THE high-risk claim. FastMCP builds a tool's JSON input schema from
    inspect.signature(fn) (Tool.from_function in mcp's tools/base.py) and its
    description from fn.__doc__ (same function). functools.wraps sets __wrapped__, which is what makes
    inspect.signature see THROUGH the coroutine wrapper to the real parameters.
    Without it every tool's schema silently collapses to (*args, **kwargs) and
    every agent client breaks while the server still starts, still lists 32
    tools, and still passes a smoke test.

    inspect.unwrap(t.fn) recovers the original sync function, so this compares
    the schema an agent actually receives against the schema the unwrapped
    function would have produced -- for all 32 tools, not a sample."""
    import inspect

    from mcp.server.fastmcp.tools.base import Tool

    server = build_server(kb)
    for t in server._tool_manager.list_tools():
        original = inspect.unwrap(t.fn)
        # If the wrapper is missing or opaque, unwrap returns t.fn itself.
        assert original is not t.fn, f"{t.name} is not wrapped"
        rebuilt = Tool.from_function(original)
        assert t.name == rebuilt.name
        assert t.parameters == rebuilt.parameters, f"{t.name} input schema changed"
        assert t.description == rebuilt.description, f"{t.name} description changed"
        assert t.output_schema == rebuilt.output_schema, f"{t.name} output schema changed"


def test_search_memories_schema_survives_the_wrapper_field_for_field(kb):
    """A hard pin, deliberately NOT expressed as "equal to the unwrapped one".
    The test above proves equality through inspect.unwrap; if the wrapper ever
    stops being transparent, that test's failure message is about unwrapping.
    This one says, in plain terms, what an agent client sees -- so the failure
    reads `assert [] == ['include_dead', ...]` and the diagnosis is immediate.

    search_memories is the pin because its `dict[str, Any]` return annotation is
    load-bearing (see the comment on search_memories in server.py): a bare `dict` makes FastMCP emit NO
    structuredContent. Verified against mcp 1.27.2 -- `dict` yields
    output_schema None, `dict[str, Any]` yields a DictOutput object schema."""
    server = build_server(kb)
    sm = next(t for t in server._tool_manager.list_tools()
              if t.name == "search_memories")
    assert sorted(sm.parameters["properties"]) == [
        "include_dead", "kind", "limit", "query", "since", "source", "tags",
        "type", "under", "until"]
    assert sm.parameters["required"] == ["query"]
    assert sm.parameters["properties"]["limit"]["default"] == 20
    assert sm.description.startswith("Hybrid memory search")
    # dict[str, Any] -> structuredContent. None here means the annotation was
    # "simplified" to `dict` and every client now has to re-parse the text block.
    assert sm.output_schema is not None
    assert sm.output_schema["type"] == "object"


def test_a_tool_body_runs_in_a_worker_thread_not_on_the_event_loop(kb):
    """Proves the offload is real, using a REAL tool body rather than a synthetic
    one: `embedder` is called on the first line of search_memories, so the thread
    it observes is the thread that tool body ran on."""
    import threading

    seen = {}

    def embedder(text):
        seen["worker"] = threading.get_ident()
        return None            # embedder down -> FTS-only, which is fine here

    server = build_server(kb, embedder=embedder)

    async def drive():
        seen["loop"] = threading.get_ident()
        return await server.call_tool("search_memories", {"query": "alpha"})

    asyncio.run(drive())
    assert "worker" in seen, "the embedder never ran -- the body did not execute"
    assert seen["worker"] != seen["loop"]


# ---- sync_source under offloaded tool bodies -------------------------------


def test_concurrent_sync_source_calls_do_not_cross_attribute_errors(tmp_path, kb, pool):
    """main() builds ONE ObsidianAdapter per source and build_server closes
    over that `adapters` dict, so two sync_source calls for the same source share
    one adapter instance. Before tool bodies were offloaded, FastMCP ran every sync
    tool body INLINE on the event loop, so two sync_source calls could never overlap
    in-process -- _offload runs them in two worker threads instead, making the overlap
    real for the first time.

    ObsidianAdapter.fetch() does `self.errors = []` then appends during
    iteration; SyncService.sync reads `adapter.errors` only after the generator
    is exhausted. Two interleaved fetches against the SAME adapter instance stomp
    each other's list and cross-attribute error counts. Reproduced here with the
    REAL ObsidianAdapter over a real temp vault of 40 unparseable notes -- not a
    fake, because the race lives in fetch()'s own list-reset-then-append shape,
    and a synthetic double is exactly the kind of thing that could accidentally
    serialise the two branches and hide the bug. Measured without the fix: 80
    and 79 errors reported where each call should see exactly 40."""
    import json

    from kb_core.adapters.obsidian.adapter import ObsidianAdapter

    vault = tmp_path / "v"
    vault.mkdir()
    n = 40
    for i in range(n):
        (vault / f"bad_{i:03d}.md").write_text(
            "---\ntitle: [unterminated\n---\nbody", encoding="utf-8")

    adapter = ObsidianAdapter(vault)
    server = build_server(kb, pool=pool, adapters={"obsidian": adapter})

    async def two_concurrent_calls():
        return await asyncio.gather(
            server.call_tool("sync_source", {"source": "obsidian"}),
            server.call_tool("sync_source", {"source": "obsidian"}),
        )

    results = asyncio.run(two_concurrent_calls())
    # sync_source's `-> dict` return annotation is bare, so (per
    # test_search_memories_schema_survives_the_wrapper_field_for_field's
    # docstring on dict vs dict[str, Any]) FastMCP emits NO structuredContent:
    # call_tool returns list[TextContent], each .text JSON-encoded -- the same
    # shape test_list_merge_suggestions_tool_returns_serializable parses.
    counts = [len(json.loads(r[0].text)["errors"]) for r in results]
    assert counts == [n, n], (
        f"expected each concurrent call to report its own {n} errors, got {counts}"
    )


def test_main_opens_the_pool_with_the_documented_default_ceiling(monkeypatch):
    """main()'s KB_MCP_POOL_MAX wiring needs its own coverage -- reverting
    `open_pool(..., max_size=...)` to `open_pool(KB_DATABASE_URL)` (dropping
    max_size entirely) left the full suite green, silently dropping the deployed
    server to max_size=4 while the docstring, the 17-line comment and the env
    var all claim 8. Mirrors KB_Core/tests/test_consolidate_daemon.py's pattern:
    monkeypatch every I/O-touching name main() calls and assert on what it was
    called WITH, never touching a real database or starting a real server."""
    import kb_mcp.server as server_mod

    seen = {}

    def fake_open_pool(dsn, **kwargs):
        seen["dsn"] = dsn
        seen["max_size"] = kwargs.get("max_size")
        return object()

    class _FakeServer:
        def run(self, transport=None):
            seen["ran_transport"] = transport

    monkeypatch.setenv("KB_DATABASE_URL", "postgresql://unused")
    monkeypatch.delenv("KB_MCP_POOL_MAX", raising=False)
    monkeypatch.delenv("KB_OBSIDIAN_VAULT", raising=False)
    monkeypatch.delenv("KB_INFERENCE_URL", raising=False)
    monkeypatch.setattr(server_mod, "open_pool", fake_open_pool)
    monkeypatch.setattr(server_mod, "migrate_pool", lambda pool: None)
    monkeypatch.setattr(server_mod, "KnowledgeBase", lambda pool, registry: object())
    monkeypatch.setattr(server_mod, "build_server", lambda *a, **kw: _FakeServer())

    server_mod.main()

    assert seen["dsn"] == "postgresql://unused"
    assert seen["max_size"] == 8
    assert "ran_transport" in seen


def test_main_honours_an_explicit_kb_mcp_pool_max_override(monkeypatch):
    """The other half of the pool-ceiling wiring: a non-default KB_MCP_POOL_MAX must reach
    open_pool unchanged, not just the documented default of 8."""
    import kb_mcp.server as server_mod

    seen = {}

    def fake_open_pool(dsn, **kwargs):
        seen["max_size"] = kwargs.get("max_size")
        return object()

    class _FakeServer:
        def run(self, transport=None):
            pass

    monkeypatch.setenv("KB_DATABASE_URL", "postgresql://unused")
    monkeypatch.setenv("KB_MCP_POOL_MAX", "3")
    monkeypatch.delenv("KB_OBSIDIAN_VAULT", raising=False)
    monkeypatch.delenv("KB_INFERENCE_URL", raising=False)
    monkeypatch.setattr(server_mod, "open_pool", fake_open_pool)
    monkeypatch.setattr(server_mod, "migrate_pool", lambda pool: None)
    monkeypatch.setattr(server_mod, "KnowledgeBase", lambda pool, registry: object())
    monkeypatch.setattr(server_mod, "build_server", lambda *a, **kw: _FakeServer())

    server_mod.main()

    assert seen["max_size"] == 3


def test_readme_tool_count_matches_the_registry(kb):
    """A count in prose next to a registry that grows is a drift machine -- nothing
    fails, because nothing reads it; this is the smallest thing that makes it not one.

    KB_MCP/README.md states the count, and test_server.py's exact-equality set is
    the source of truth, so this test and that set are the only two places a new
    tool has to be recorded."""
    import re
    from pathlib import Path

    doc_path = Path(__file__).resolve().parents[1] / "README.md"
    doc = doc_path.read_text()
    claimed = re.search(r"registers (\d+) tools", doc)
    assert claimed, "README.md must state the tool count as 'registers N tools'"
    server = build_server(kb)
    assert int(claimed.group(1)) == len(server._tool_manager.list_tools())
