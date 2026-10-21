import asyncio
import json
import os
import threading
import time
import types

import psycopg
import pytest

from kb_core.domain import NodeRecord
from kb_core.tenancy import TenantPools
from kb_core.tenants import Registry, Tenant
from kb_mcp import server as S
from tests.conftest import TEST_DATABASE_URL, tenant_login

URL_B = os.environ.get("KB_TEST_DATABASE_URL_B", TEST_DATABASE_URL + "2")


def _registry() -> Registry:
    db_a, db_b = TEST_DATABASE_URL.rsplit("/", 1)[1], URL_B.rsplit("/", 1)[1]
    owner = Tenant("owner", frozenset({"owner@example.com"}), database=db_a)
    b = Tenant("b", frozenset({"b@example.com"}), database=db_b, agent_secret="b" * 64, **tenant_login())
    return Registry(owner, {"b": b}, env={"KB_DATABASE_URL": TEST_DATABASE_URL})


@pytest.fixture
def pools(kb):
    try:
        psycopg.connect(URL_B, connect_timeout=3).close()
    except Exception:
        pytest.skip("KB_TEST_DATABASE_URL_B unreachable")
    p = TenantPools(_registry(), max_size=2, env={"KB_DATABASE_URL": TEST_DATABASE_URL})
    with p.get_by_slug("b").pool.connection() as c:
        c.execute("TRUNCATE nodes CASCADE")
        c.execute("TRUNCATE mcp_trace CASCADE")
        c.execute("TRUNCATE mcp_tool_state")
        c.execute("TRUNCATE skills CASCADE")
    yield p
    p.close()


def _call(server, tool, args):
    out = asyncio.run(server.call_tool(tool, args))
    if isinstance(out, tuple):
        return out[1]
    # A bare `-> dict` tool (add_memory) emits no structuredContent, only its JSON text.
    return json.loads(out[0].text) if out else None


def _principal(monkeypatch, email):
    """Stand in for the Starlette request state the auth wall fills in over HTTP."""
    monkeypatch.setattr(S, "resolve_principal", lambda mcp: email)


def _titles(server) -> set[str]:
    rows = _call(server, "list_memories", {})
    if isinstance(rows, dict):
        rows = rows.get("result") or rows.get("results") or []
    return {r["title"] for r in rows}


def test_single_kb_mode_is_unchanged(kb):
    server = S.build_server(kb)
    out = _call(server, "add_memory", {"content": "x", "title": "Solo"})
    assert kb.get(__import__("uuid").UUID(out["id"])).title == "Solo"


def test_default_tenant_serves_the_owner_without_a_principal(pools, kb, monkeypatch):
    monkeypatch.delenv("KB_TENANT", raising=False)
    server = S.build_server(pools)
    _call(server, "add_memory", {"content": "owner note", "title": "Own"})
    assert kb.get_by_path("Own") is not None                    # kb == owner database
    assert pools.get_by_slug("b").kb.get_by_path("Own") is None


def test_principal_selects_the_tenant_and_isolates_reads_and_writes(pools, kb, monkeypatch):
    b = pools.get_by_slug("b").kb
    kb.upsert(NodeRecord(source="agent", external_id="oa", type="note", content="owner only",
                         slug="oa", title="OwnerOnly"))
    b.upsert(NodeRecord(source="agent", external_id="bb", type="note", content="b only",
                        slug="bb", title="BOnly"))
    server = S.build_server(pools)
    _principal(monkeypatch, "b@example.com")
    titles = _titles(server)
    assert "BOnly" in titles and "OwnerOnly" not in titles
    _call(server, "add_memory", {"content": "from b", "title": "FromB"})
    assert b.get_by_path("FromB") is not None and kb.get_by_path("FromB") is None
    _principal(monkeypatch, "owner@example.com")
    titles = _titles(server)
    assert "OwnerOnly" in titles and "BOnly" not in titles
    got = _call(server, "get_memory", {"path": "FromB"})
    assert got is None or got.get("result") is None


def test_unknown_principal_is_refused(pools, monkeypatch):
    server = S.build_server(pools)
    _principal(monkeypatch, "stranger@example.com")
    with pytest.raises(Exception, match="no tenant"):
        _call(server, "list_memories", {})


def test_traces_land_in_the_tenants_database(pools, kb, monkeypatch):
    server = S.build_server(pools)
    _principal(monkeypatch, "b@example.com")
    _call(server, "search_memories", {"query": "anything"})
    with pools.get_by_slug("b").pool.connection() as c:
        assert c.execute("SELECT count(*) AS n FROM mcp_trace").fetchone()["n"] == 1
    with kb._pool.connection() as c:
        assert c.execute("SELECT count(*) AS n FROM mcp_trace").fetchone()["n"] == 0


def test_tool_inventory_is_published_per_tenant(pools, kb):
    S.build_server(pools)
    for ctx in (pools.get_by_slug("owner"), pools.get_by_slug("b")):
        assert {r["tool"] for r in ctx.kb.tool_states()} >= {"add_memory", "search_graph"}


class _StubAdapter:
    """Counts fetches: proves whether sync_source got past the adapter lookup."""
    source = "stub"

    def __init__(self):
        self.errors = []
        self.fetched = 0

    def fetch(self):
        self.fetched += 1
        return iter(())


def test_sync_source_is_owner_only(pools, monkeypatch):
    stub = _StubAdapter()
    server = S.build_server(pools, adapters={"obsidian": stub})
    _principal(monkeypatch, "b@example.com")
    with pytest.raises(Exception, match="not configured"):
        _call(server, "sync_source", {"source": "obsidian", "dry_run": True})
    assert stub.fetched == 0
    _principal(monkeypatch, "owner@example.com")
    out = _call(server, "sync_source", {"source": "obsidian", "dry_run": True})
    assert stub.fetched == 1 and out["source"] == "stub"


def _http_tool(client, tool, args) -> dict:
    """One MCP tools/call over streamable-http: initialize, initialized, call."""
    h = {"Accept": "application/json, text/event-stream",
         "Content-Type": "application/json"}
    r = client.post("/mcp", headers=h, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "t", "version": "1"}}})
    h = {**h, "mcp-session-id": r.headers["mcp-session-id"],
         "mcp-protocol-version": "2025-06-18"}
    client.post("/mcp", headers=h, json={"jsonrpc": "2.0",
                                         "method": "notifications/initialized"})
    r = client.post("/mcp", headers=h, json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": tool, "arguments": args}})
    for line in r.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:])["result"]
    return r.json()["result"]


@pytest.mark.filterwarnings("ignore:Using `httpx` with `starlette.testclient`")
def test_http_principal_routes_a_write_to_its_tenant(pools, kb):
    """The REAL resolve_principal, through FastMCP's request context: the auth wall's
    scope["state"]["kb_principal"] picks the database a write lands in."""
    from starlette.testclient import TestClient

    # require_principal: had the principal not arrived, the call would be refused.
    server = S.build_server(pools, host="127.0.0.1", port=3199, require_principal=True)
    inner = server.streamable_http_app()

    async def app(scope, receive, send):
        if scope["type"] == "http":
            scope.setdefault("state", {})["kb_principal"] = "b@example.com"
        await inner(scope, receive, send)

    with TestClient(app, base_url="http://127.0.0.1:3199") as client:
        out = _http_tool(client, "add_memory", {"content": "over http", "title": "ViaHttp"})
    assert not out.get("isError"), out
    assert pools.get_by_slug("b").kb.get_by_path("ViaHttp") is not None
    assert kb.get_by_path("ViaHttp") is None


def test_resolve_principal_is_none_only_without_a_request(kb, monkeypatch):
    server = S.build_server(kb)
    assert S.resolve_principal(server) is None          # no active request context

    class _Ctx:
        request_context = types.SimpleNamespace(request=object())   # no .state

    monkeypatch.setattr(server, "get_context", lambda: _Ctx())
    with pytest.raises(AttributeError):
        S.resolve_principal(server)


def test_require_principal_refuses_a_call_without_one(pools):
    server = S.build_server(pools, require_principal=True)
    with pytest.raises(Exception, match="no principal on request"):
        _call(server, "list_memories", {})
    with pytest.raises(Exception, match="no principal on request"):
        asyncio.run(server.list_tools())


def test_unopened_tenant_opens_off_the_event_loop(pools, monkeypatch):
    server = S.build_server(pools)          # opens every tenant
    real_get, seen = pools.get, []

    def slow_get(t):
        seen.append(threading.get_ident())
        time.sleep(0.05)
        return real_get(t)

    monkeypatch.setattr(pools, "get", slow_get)
    monkeypatch.setattr(pools, "opened", lambda: ["owner"])     # b looks unopened
    _principal(monkeypatch, "b@example.com")
    loop_thread = threading.get_ident()     # asyncio.run drives the loop on this thread
    _call(server, "list_memories", {})
    assert seen and seen[0] != loop_thread


def test_tool_gate_is_per_tenant(pools, kb, monkeypatch):
    monkeypatch.setenv("KB_MCP_GATE_TTL", "0")
    server = S.build_server(pools)
    pools.get_by_slug("b").kb.set_tool_enabled("add_memory", False)

    def names():
        return {t.name for t in asyncio.run(server.list_tools())}

    _principal(monkeypatch, "b@example.com")
    assert "add_memory" not in names()
    with pytest.raises(Exception, match="disabled"):
        _call(server, "add_memory", {"content": "x", "title": "Blocked"})
    _principal(monkeypatch, "owner@example.com")
    assert "add_memory" in names()
    _call(server, "add_memory", {"content": "x", "title": "Allowed"})
    assert kb.get_by_path("Allowed") is not None


def test_registry_override_must_be_the_pools_own(pools):
    with pytest.raises(ValueError, match="registry"):
        S.build_server(pools, registry=_registry())
    S.build_server(pools, registry=pools.registry)


def test_main_pools_serve_the_owner_from_mains_own_pool(pools):
    from kb_core.db import open_pool
    owner_pool = open_pool(TEST_DATABASE_URL, max_size=2)
    p = S._OwnerSeededPools(_registry(), owner_pool, max_size=2,
                            env={"KB_DATABASE_URL": TEST_DATABASE_URL})
    try:
        owner = p.get_by_slug("owner")
        assert owner.pool is owner_pool and p.get_by_slug("owner") is owner
        assert p.get_by_slug("b").pool is not owner_pool
        assert p.opened() == ["b", "owner"]
    finally:
        p.close()
    assert owner_pool.closed


def test_main_reads_the_same_registry_from_the_kb_mcp_cwd(monkeypatch):
    """kb-svc runs mcp/mcp-public after `cd $KB_MCP_DIR`. main()'s load_registry() must
    find KB_Core's tenants.toml from there, not a never-existing KB_MCP/tenants.toml."""
    from pathlib import Path

    from kb_core.tenants import load_registry
    mcp_dir = Path(S.__file__).resolve().parents[2]
    core = mcp_dir.parent / "KB_Core"
    monkeypatch.delenv("KB_TENANTS_CONFIG", raising=False)
    monkeypatch.chdir(core)
    from_core = load_registry().path
    monkeypatch.chdir(mcp_dir)
    assert load_registry().path == from_core == core / "tenants.toml"


_FILES = {"SKILL.md": "---\nname: shared-skill\ndescription: Owner procedure.\n---\nowner\n"}


def test_skill_registry_follows_the_tenant_resolution_of_every_other_tool(pools, kb, monkeypatch):
    """The skill registry defines no access rule of its own -- the owner's
    loopback agents are served the owner's library exactly as they are served memories,
    a principal selects its own library, a stranger is refused."""
    monkeypatch.delenv("KB_TENANT", raising=False)
    server = S.build_server(pools)
    b = pools.get_by_slug("b").kb
    # No principal (loopback / stdio / internal-secret): the default tenant, the owner.
    out = _call(server, "upsert_skill", {"name": "shared-skill", "expected_revision": None,
                                         "files": _FILES, "change_summary": "create"})
    assert out["revision"] == 1
    assert kb.get_skill("shared-skill")["history"][0]["actor"] is None
    with pytest.raises(Exception, match="not_found"):
        b.get_skill("shared-skill")
    # Tenant b's principal: its own, empty library; its writes land in b only.
    _principal(monkeypatch, "b@example.com")
    assert _call(server, "search_skills", {})["skills"] == []
    with pytest.raises(Exception, match="not_found"):
        _call(server, "get_skill", {"name": "shared-skill"})
    _call(server, "upsert_skill", {
        "name": "shared-skill", "expected_revision": None,
        "files": {"SKILL.md": "---\nname: shared-skill\ndescription: B procedure.\n---\nb\n"},
        "change_summary": "b create"})
    assert b.get_skill("shared-skill")["history"][0]["actor"] == "b@example.com"
    assert kb.get_skill("shared-skill")["description"] == "Owner procedure."      # untouched
    # The owner's principal reaches the same library the loopback call wrote to.
    _principal(monkeypatch, "owner@example.com")
    assert _call(server, "get_skill", {"name": "shared-skill",
                                       "revision": 1})["description"] == "Owner procedure."
    # An authenticated email the registry does not know is refused, never served as the owner.
    _principal(monkeypatch, "stranger@example.com")
    with pytest.raises(Exception, match="no tenant"):
        _call(server, "search_skills", {})


def test_require_principal_refuses_registry_calls_without_one(pools):
    server = S.build_server(pools, require_principal=True)
    with pytest.raises(Exception, match="no principal"):
        _call(server, "search_skills", {})
    with pytest.raises(Exception, match="no principal"):
        _call(server, "upsert_skill", {"name": "shared-skill", "expected_revision": None,
                                       "files": _FILES, "change_summary": "create"})
