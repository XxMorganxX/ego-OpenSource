"""Multi-tenancy release gate on the MCP surface: every tool answers only from the caller's
tenant, and no tool call -- read or write, aimed at the other tenant's ids, paths, names
or words -- changes a byte of the other tenant's database.

The stack is the one `main()` serves on the public listener: `build_server(pools,
require_principal=True)` -> `streamable_http_app()` -> `wrap_with_auth(cf-access)`, called
over streamable-http with RS256 Access JWTs. Only the JWKS fetch is replaced by a local
key; the JWT check, the email -> tenant mapping (`resolve_principal` reading the scope
the wall stamped) and the per-tenant KnowledgeBase binding are all real. No tenant
resolution is monkeypatched.

A failure here is a release blocker: fix the surface, never the test.

Coverage is enforced: every registered tool must have an entry in TOOL_CALLS, so a new
tool fails `test_every_tool_is_classified` until its isolation is decided."""
import faulthandler
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import jwt
import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from kb_core.adapters.obsidian.adapter import ObsidianAdapter
from kb_core.db import open_pool
from kb_core.domain import NodeRecord
from kb_core.tenants import Registry, Tenant
from kb_mcp import server as S
from kb_mcp.auth import CfAccessConfig, wrap_with_auth
from tests.conftest import TEST_DATABASE_URL, tenant_login

pytestmark = pytest.mark.filterwarnings("ignore:Using `httpx` with `starlette.testclient`")

URL_B = os.environ.get("KB_TEST_DATABASE_URL_B", TEST_DATABASE_URL + "2")
AUD = "aud-isolation-mcp"
BASE = "http://127.0.0.1:3199"
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)

OWNER, TENANT_B = "m@x.test", "b@x.test"
TAG_A, TAG_B = "alphaz", "bravoz"


# --------------------------------------------------------------------------- bounds
# Every blocking wait in this gate is bounded: a hang must FAIL the gate, never stall it.

HANG_S = 240        # backstop: no single test may run this long


@pytest.fixture(autouse=True)
def _hang_backstop():
    """Anything unbounded that slips past the explicit timeouts: dump every thread's
    stack and abort the run instead of sleeping forever."""
    def _abort():
        faulthandler.dump_traceback(all_threads=True)
        sys.stderr.write(f"\nisolation gate: a test exceeded {HANG_S} s -- hung; aborting\n")
        sys.stderr.flush()
        os._exit(70)
    timer = threading.Timer(HANG_S, _abort)
    timer.daemon = True
    timer.start()
    yield
    timer.cancel()


def _bounded(fn, seconds: float, what: str):
    """fn() in a daemon thread, failing the test if it has not returned in `seconds`."""
    out: dict = {}

    def run():
        try:
            out["value"] = fn()
        except BaseException as exc:            # re-raised on the test's thread
            out["error"] = exc
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        pytest.fail(f"{what} did not finish within {seconds:.0f} s (hung)")
    if "error" in out:
        raise out["error"]
    return out.get("value")


# --------------------------------------------------------------------------- helpers
# (_seed/_snapshot/_leaks/_sent mirror KB_Core/tests/test_isolation.py; the two suites
# cannot import each other's tests package.)

def _jwt(email: str) -> str:
    now = int(time.time())
    return jwt.encode({"email": email, "aud": AUD, "iat": now, "exp": now + 600,
                       "iss": "https://iso.cloudflareaccess.com"},
                      _KEY, algorithm="RS256")


def _wipe(pool) -> None:
    with pool.connection() as c:
        c.execute("TRUNCATE nodes CASCADE")
        c.execute("TRUNCATE ingest_log, ingest_inbox, mcp_tool_state")
        c.execute("TRUNCATE mcp_trace CASCADE")
        c.execute("TRUNCATE concept_runs, concept_run_requests, concepts, concept_members, "
                  "concept_constraints, concept_member_overrides, concept_separations")
        c.execute("TRUNCATE skills CASCADE")


def _seed(kb, tag: str) -> dict:
    t = tag.capitalize()
    note = kb.upsert(NodeRecord(source="agent", external_id=f"{tag}1", type="note",
                                content=f"{tag} secret memo", slug=f"{tag}_note",
                                title=f"{t}Note"))
    two = kb.upsert(NodeRecord(source="agent", external_id=f"{tag}2", type="note",
                               content=f"{tag} second memo", slug=f"{tag}_two",
                               title=f"{t}Two"))
    with kb._pool.connection() as c:
        c.execute("UPDATE nodes SET doc_kind = %s WHERE id = ANY(%s)",
                  (f"{tag}kind", [note.id, two.id]))
    e1 = kb.upsert_entity(f"{t}Ent", categories=[f"{tag}class"])
    e2 = kb.upsert_entity(f"{t}Entb", categories=[f"{tag}class"])
    kb.link_entity(note.id, e1.node.id, "mentions")
    kb.link_entity(two.id, e2.node.id, "mentions")
    left, right = sorted([e1.node.id, e2.node.id], key=str)
    cand = kb.stage_merge_candidate(left, right, "instance", 0.1, f"{tag} same", left)
    run = kb.begin_concept_run({})
    kb.apply_concept_run(run, [
        {"key": "c0", "prev_id": None, "parent_key": None, "level": 0,
         "name": f"{t}Concept", "kind": "project", "description": f"{tag} work",
         "members": [(str(note.id), "memory", 1.0), (str(e1.node.id), "entity", 0.9)],
         "embedding": None}], [])
    kb.finish_concept_run(run, status="done", stats={})
    concept = next(c["id"] for c in kb.active_concepts() if c["name"] == f"{t}Concept")
    kb.log_trace(tool="search_memories", args={"query": f"{tag} secret"}, latency_ms=1,
                 query=f"{tag} secret", result_count=1,
                 results=[{"id": str(note.id), "title": note.title, "rank": 1}])
    seeded = kb.upsert_skill(f"{tag}-skill", None, {
        "SKILL.md": f"---\nname: {tag}-skill\ndescription: {tag} secret skill\n---\n\n"
                    f"{tag} secret memo\n"}, f"{tag} seed")
    # MCP reads serve approved revisions only; approve so the positive control is served.
    kb.review_skill(f"{tag}-skill", 1, seeded["package_hash"], True, "seed approved")
    # A pending skill revision and an approved agent with a pending one, every text
    # carrying the tag: the aimed get/search/list calls then have something to leak, and
    # the aimed review_* calls (real hash, approve) something to change.
    pend_skill = kb.upsert_skill(f"{tag}-skill", None, {
        "SKILL.md": f"---\nname: {tag}-skill\ndescription: {tag} secret skill\n---\n\n"
                    f"{tag} pending memo\n"}, f"{tag} pending skill edit")
    agent = kb.upsert_agent(f"{tag}-agent", None,
                            f"---\nname: {tag}-agent\ndescription: {tag} secret agent\n---\n\n"
                            f"{tag} secret prompt\n", f"{tag} seed agent")
    kb.review_agent(f"{tag}-agent", 1, agent["package_hash"], True, "seed approved")
    pend_agent = kb.upsert_agent(f"{tag}-agent", None,
                                 f"---\nname: {tag}-agent\ndescription: {tag} secret agent\n"
                                 f"---\n\n{tag} pending prompt\n", f"{tag} pending agent edit")
    ids = [note.id, two.id, e1.node.id, e2.node.id, concept]
    return {"tag": tag, "note": note, "e1": e1.node, "concept": concept, "cand": cand,
            "kind": f"{tag}kind", "ids": {str(i).lower() for i in ids},
            "skill_hash": pend_skill["package_hash"], "agent_hash": pend_agent["package_hash"]}


def _leaks(text: str, other: dict, sent=()) -> list[str]:
    """The other tenant's markers in a response, after cutting out what the caller itself
    sent (an echoed query or id is not a leak; anything added from the other database is)."""
    low = text.lower()
    # only 6+ characters (a short value could carve a hole in a UUID marker), never the tag
    cut = {str(x).lower() for x in sent if x} - {other["tag"]}
    for s in sorted((c for c in cut if len(c) >= 6), key=len, reverse=True):
        low = low.replace(s, "")
    return [m for m in (other["tag"], *other["ids"]) if m in low]


def _strings(v) -> list[str]:
    if isinstance(v, dict):
        return [s for x in v.values() for s in _strings(x)]
    if isinstance(v, list):
        return [s for x in v for s in _strings(x)]
    return [str(v)] if v is not None and not isinstance(v, bool) else []


def _snapshot(pool) -> dict[str, str]:
    """A digest of every row of every table of one database."""
    with pool.connection() as c:
        tables = [r["tablename"] for r in c.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "AND tablename <> 'schema_migrations' ORDER BY 1")]
        return {t: c.execute(f"SELECT md5(coalesce(string_agg(x::text, '|' ORDER BY x::text), '')) "
                             f"AS h FROM {t} x").fetchone()["h"] for t in tables}


def _diff(before: dict, after: dict) -> list[str]:
    return sorted(t for t in before.keys() | after.keys() if before.get(t) != after.get(t))


class _Session:
    """One MCP streamable-http session, every request carrying `email`'s Access JWT."""

    def __init__(self, client: TestClient, email: str, session_id: str | None = None):
        self.client, self.email = client, email
        self.h = {"Accept": "application/json, text/event-stream",
                  "Content-Type": "application/json"}
        if session_id is None:
            r = self._post({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                       "clientInfo": {"name": "iso", "version": "1"}}})
            assert r.status_code == 200, (email, r.status_code, r.text)
            session_id = r.headers["mcp-session-id"]
            self.h = {**self.h, "mcp-session-id": session_id,
                      "mcp-protocol-version": "2025-06-18"}
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        else:
            self.h = {**self.h, "mcp-session-id": session_id,
                      "mcp-protocol-version": "2025-06-18"}
        self.session_id = session_id
        self._n = 0

    def _post(self, body: dict):
        return self.client.post("/mcp", json=body,
                                headers={**self.h, "Cf-Access-Jwt-Assertion": _jwt(self.email)})

    def rpc(self, method: str, params: dict) -> dict:
        self._n += 1
        r = self._post({"jsonrpc": "2.0", "id": self._n, "method": method, "params": params})
        assert r.status_code == 200, (self.email, method, r.status_code, r.text[:300])
        for line in r.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:])
        return r.json()

    def call(self, tool: str, args: dict) -> tuple[bool, str]:
        """(isError, the whole JSON-RPC response as text) -- the text is what a client
        sees, so the whole of it is what must not leak."""
        msg = self.rpc("tools/call", {"name": tool, "arguments": args})
        res = msg.get("result") or {}
        return bool(res.get("isError") or "error" in msg), json.dumps(msg)

    def tools(self) -> set[str]:
        return {t["name"] for t in self.rpc("tools/list", {})["result"]["tools"]}


# --------------------------------------------------------------------------- fixture

def _require_db_b() -> None:
    """Skip only on a casual local run. When B was asked for (KB_TEST_DATABASE_URL_B set)
    or in CI, an unreachable B is a FAILURE: the release gate must never pass by skipping."""
    try:
        psycopg.connect(URL_B, connect_timeout=3).close()
    except Exception as exc:
        if os.environ.get("KB_TEST_DATABASE_URL_B") or os.environ.get("CI"):
            pytest.fail(f"KB_TEST_DATABASE_URL_B unreachable: {exc}")
        pytest.skip("KB_TEST_DATABASE_URL_B unreachable")


VAULT_MARK = f"{TAG_A}vault"


@pytest.fixture
def world(kb, monkeypatch, tmp_path):
    _require_db_b()
    monkeypatch.setenv("KB_MCP_GATE_TTL", "0")          # a gate flip is seen at once
    db_a, db_b = TEST_DATABASE_URL.rsplit("/", 1)[1], URL_B.rsplit("/", 1)[1]
    reg = Registry(Tenant("owner", frozenset({OWNER}), database=db_a),
                   {"b": Tenant("b", frozenset({TENANT_B}), database=db_b,
                                agent_secret="b" * 64, **tenant_login())},
                   env={"KB_DATABASE_URL": TEST_DATABASE_URL})
    # main()'s pools: the owner served from a pool main() opened itself (its own pool here,
    # since closing these pools closes it), every other tenant through TenantPools.
    pools = S._OwnerSeededPools(reg, open_pool(TEST_DATABASE_URL, max_size=4), max_size=4,
                                env={"KB_DATABASE_URL": TEST_DATABASE_URL})
    b = pools.get_by_slug("b")
    _wipe(b.pool)
    A, B = _seed(kb, TAG_A), _seed(b.kb, TAG_B)
    # The owner's vault, as main() wires it: one tag-marked note, read-only (no stamping).
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / f"{VAULT_MARK}.md").write_text(f"# {VAULT_MARK}\n\n{TAG_A} vault memo\n")
    adapters = {"obsidian": ObsidianAdapter(vault, root_label="Obsidian")}
    # main()'s public-listener stack, verbatim but for the JWKS key source
    server = S.build_server(pools, adapters=adapters, host="127.0.0.1", port=3199,
                            require_principal=True)
    app = wrap_with_auth(
        server.streamable_http_app(),
        CfAccessConfig(team_domain="iso.cloudflareaccess.com", audience=AUD,
                       emails=frozenset()),
        key_for_token=lambda _tok: _KEY.public_key(),
        allow=lambda e: reg.by_email(e) is not None)
    client = TestClient(app, base_url=BASE)
    _bounded(client.__enter__, 30, "MCP lifespan startup")      # session manager up
    try:
        yield {"client": client, "A": A, "B": B, "pool_a": kb._pool, "pool_b": b.pool,
               "kb_a": kb, "kb_b": b.kb,
               "who": {OWNER: (A, B, b.pool), TENANT_B: (B, A, kb._pool)}}
    finally:
        try:
            _bounded(lambda: client.__exit__(None, None, None), 30, "MCP lifespan shutdown")
        finally:
            pools.close()


# --------------------------------------------------------------------------- the table

# Every tool -> argument sets aimed at the OTHER tenant (`o` = its seed). "miss" marks a
# call naming one of the other tenant's UUIDs: it must come back as an error or an empty
# result, never as a hit.
def _calls(o: dict) -> dict[str, list[tuple[dict, bool]]]:
    t = o["tag"]
    T = t.capitalize()
    note, ent, path = str(o["note"].id), str(o["e1"].id), o["note"].path
    return {
        "add_memory": [({"content": "iso", "title": "IsoWrite"}, False)],
        "search_memories": [({"query": f"{t} secret"}, False), ({"query": "secret memo"}, False),
                            ({"query": "memo", "under": path}, False),
                            ({"query": "memo", "kind": o["kind"]}, False)],
        "search_graph": [({"query": f"{t} secret"}, False), ({"query": "secret memo"}, False)],
        "find_entity": [({"name": f"{T}Ent"}, False), ({"name": t}, False)],
        "get_entity": [({"id": ent}, True)],
        "entity_activity": [({"entity_id": ent}, False)],
        "list_concepts": [({}, False)],
        "concept": [({"concept_id": str(o["concept"])}, True)],
        "list_doc_kinds": [({"all": True}, False)],
        "expand": [({"id": note}, True), ({"id": ent}, True), ({"id": str(o["concept"])}, True)],
        "get_memory": [({"id": note}, True), ({"path": path}, True)],
        "list_memories": [({}, False), ({"type": None}, False), ({"kind": o["kind"]}, False),
                          ({"under": path}, False)],
        "list_merge_suggestions": [({}, False)],
        "move_memory": [({"id": note, "new_parent_path": "elsewhere"}, True)],
        "update_memory": [({"id": note, "title": "pwned", "content": "pwned"}, True)],
        "delete_memory": [({"id": note, "recursive": True}, False)],     # idempotent: deleted 0
        "set_importance": [({"id": note, "value": 4.0}, True)],
        "approve_merge": [({"candidate_id": o["cand"], "target_id": ent}, False)],
        "reject_merge": [({"candidate_id": o["cand"], "reason": "pwned"}, False)],
        # owner-only; see test_sync_source_is_owner_only for the guard itself
        "sync_source": [({"source": "obsidian", "dry_run": True}, False)],
        # skill registry: names are per tenant, so every aimed call must miss
        "search_skills": [({"query": f"{t} secret"}, False), ({}, False),
                          ({"include_archived": True}, False)],
        "get_skill": [({"name": f"{t}-skill"}, True)],
        "get_skill_file": [({"name": f"{t}-skill", "path": "SKILL.md", "revision": 1}, True)],
        "upsert_skill": [({"name": f"{t}-skill", "expected_revision": 1,
                           "files": {"SKILL.md": f"---\nname: {t}-skill\ndescription: pwned\n"
                                                 f"---\npwned\n"},
                           "change_summary": "pwned"}, True)],
        "delete_skill": [({"name": f"{t}-skill", "expected_revision": 1}, True)],
        "review_skill": [({"name": f"{t}-skill", "revision": 1, "package_hash": "f" * 64,
                           "approve": True, "notes": "pwned"}, True),
                         ({"name": f"{t}-skill", "revision": 2, "package_hash": o["skill_hash"],
                           "approve": True, "notes": "pwned"}, True)],
        # agents: the other tenant has an approved one and a pending revision; every
        # aimed call must miss
        "search_agents": [({}, False), ({"query": f"{t} secret"}, False),
                          ({"include_archived": True}, False)],
        "get_agent": [({"name": f"{t}-agent"}, True)],
        "upsert_agent": [({"name": f"{t}-agent", "expected_revision": 1,
                           "agent_md": f"---\nname: {t}-agent\ndescription: pwned\n---\npwned\n",
                           "change_summary": "pwned"}, True)],
        "delete_agent": [({"name": f"{t}-agent", "expected_revision": 1}, True)],
        "review_agent": [({"name": f"{t}-agent", "revision": 1, "package_hash": "f" * 64,
                           "approve": True, "notes": "pwned"}, True),
                         ({"name": f"{t}-agent", "revision": 2, "package_hash": o["agent_hash"],
                           "approve": True, "notes": "pwned"}, True)],
        "list_pending_reviews": [({}, False)],
    }


def _is_hit(text: str) -> bool:
    """A successful answer that carries something (not null / [] / {} / an empty page)."""
    res = json.loads(text).get("result") or {}
    sc = res.get("structuredContent")
    if sc is None:
        body = [c.get("text") for c in res.get("content", [])]
        return any(b not in (None, "", "null", "[]", "{}") for b in body)
    inner = sc.get("result", sc) if isinstance(sc, dict) else sc
    return inner not in (None, [], {})


def test_every_tool_is_classified(world):
    s = _Session(world["client"], OWNER)
    assert s.tools() == set(_calls(world["B"]))


@pytest.mark.parametrize("email", [OWNER, TENANT_B])
def test_every_tool_is_siloed(world, email):
    """Every tool, aimed at the other tenant: no marker of it in any answer, no hit on any
    of its ids, and its database byte-identical afterwards."""
    own, other, other_pool = world["who"][email]
    s = _Session(world["client"], email)
    before = _snapshot(other_pool)
    for tool, argsets in _calls(other).items():
        for args, must_miss in argsets:
            err, text = s.call(tool, args)
            assert not _leaks(text, other, _strings(args)), (email, tool, args, text[:400])
            if must_miss:
                assert err or not _is_hit(text), (email, tool, args, text[:400])
    assert _diff(before, _snapshot(other_pool)) == []
    own_kb = world["kb_b"] if email == TENANT_B else world["kb_a"]
    other_kb = world["kb_a"] if email == TENANT_B else world["kb_b"]
    assert own_kb.get_by_path("IsoWrite") is not None and other_kb.get_by_path("IsoWrite") is None


@pytest.mark.parametrize("email", [OWNER, TENANT_B])
def test_own_data_is_served(world, email):
    """Positive control for the sweep above: the same tools DO answer with the caller's
    own data, so an empty or erroring surface cannot pass for an isolated one."""
    own, other, _ = world["who"][email]
    s = _Session(world["client"], email)
    t = own["tag"]
    for tool, args in [("list_memories", {}), ("search_memories", {"query": "secret memo"}),
                       ("search_graph", {"query": "secret memo"}),
                       ("find_entity", {"name": f"{t.capitalize()}Ent"}),
                       ("list_concepts", {}), ("list_merge_suggestions", {}),
                       ("list_doc_kinds", {"all": True}),
                       ("get_memory", {"id": str(own["note"].id)}),
                       ("get_entity", {"id": str(own["e1"].id)}),
                       ("concept", {"concept_id": str(own["concept"])})]:
        err, text = s.call(tool, args)
        assert not err and t in text.lower(), (email, tool, text[:400])
        assert not _leaks(text, other), (email, tool, text[:400])
    # skill registry: the caller's own skill is served
    err, text = s.call("search_skills", {})
    assert not err, (email, "search_skills", text[:400])
    names = [r["name"] for r in json.loads(text)["result"]["structuredContent"]["skills"]]
    assert f"{t}-skill" in names, (email, names)
    assert not _leaks(text, other), (email, "search_skills", text[:400])
    err, text = s.call("get_skill", {"name": f"{t}-skill"})
    assert not err and t in text.lower(), (email, "get_skill", text[:400])
    assert not _leaks(text, other), (email, "get_skill", text[:400])
    # agents and the pending list: the caller's own seeds are served
    for tool, args, wants in [
            ("search_agents", {}, [f"{t} secret agent"]),
            ("get_agent", {"name": f"{t}-agent"}, [f"{t} secret prompt"]),
            ("list_pending_reviews", {}, [f"{t} pending skill edit", f"{t} pending agent edit"])]:
        err, text = s.call(tool, args)
        assert not err and all(w in text.lower() for w in wants), (email, tool, text[:400])
        assert not _leaks(text, other), (email, tool, text[:400])


def test_unauthenticated_and_unknown_callers_get_nothing(world):
    client = world["client"]
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "iso", "version": "1"}}}
    h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    for extra in ({}, {"Cf-Access-Jwt-Assertion": "garbage"},
                  {"Cf-Access-Jwt-Assertion": _jwt("stranger@x.test")},
                  {"Cf-Access-Jwt-Assertion": jwt.encode(
                      {"email": OWNER, "aud": "some-other-app", "iat": int(time.time()),
                       "exp": int(time.time()) + 600, "iss": "https://iso.cloudflareaccess.com"},
                      _KEY, algorithm="RS256")}):
        r = client.post("/mcp", json=body, headers={**h, **extra})
        assert r.status_code == 401 and r.text == "unauthorized", (extra, r.status_code)


def test_a_session_does_not_carry_its_openers_tenant(world):
    """The tenant is resolved per REQUEST from that request's JWT, never remembered by the
    session: a call riding another principal's session id is served as the caller."""
    client = world["client"]
    opened = {OWNER: _Session(client, OWNER), TENANT_B: _Session(client, TENANT_B)}
    for caller, opener in ((TENANT_B, OWNER), (OWNER, TENANT_B)):
        own, other, _ = world["who"][caller]
        riding = _Session(client, caller, session_id=opened[opener].session_id)
        err, text = riding.call("list_memories", {})
        assert not err and own["tag"] in text.lower(), (caller, text[:300])
        assert not _leaks(text, other), (caller, opener, text[:300])


def test_tool_gate_is_per_tenant(world):
    """b switching a tool off in ITS database hides it from b only."""
    world["kb_b"].set_tool_enabled("list_memories", False)
    owner, b = _Session(world["client"], OWNER), _Session(world["client"], TENANT_B)
    assert "list_memories" in owner.tools() and "list_memories" not in b.tools()
    err, text = owner.call("list_memories", {})
    assert not err and TAG_A in text.lower()
    err, text = b.call("list_memories", {})
    assert err and TAG_A not in text.lower() and TAG_B not in text.lower()


def test_concurrent_principals_never_cross(world):
    """Interleaved calls from both principals on ONE event loop: the tenant binding is a
    contextvar carried into worker threads, so any bleed between tasks shows up here."""
    client = world["client"]
    sessions = {OWNER: _Session(client, OWNER), TENANT_B: _Session(client, TENANT_B)}

    def call(i: int):
        email = (OWNER, TENANT_B)[i % 2]
        own = world["who"][email][0]
        tool, args = (("list_memories", {}), ("get_memory", {"id": str(own["note"].id)}),
                      ("search_memories", {"query": "secret memo"}))[i % 3]
        return email, tool, *sessions[email].call(tool, args)

    ex = ThreadPoolExecutor(max_workers=8)
    try:
        futures = [ex.submit(call, i) for i in range(60)]
        results = [f.result(timeout=60) for f in futures]
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    for email, tool, err, text in results:
        own, other, _ = world["who"][email]
        assert not err and own["tag"] in text.lower(), (email, tool, text[:300])
        assert not _leaks(text, other), (email, tool, text[:300])


def test_traces_land_only_in_the_callers_database(world):
    before_a = _snapshot(world["pool_a"])
    s = _Session(world["client"], TENANT_B)
    s.call("search_memories", {"query": "isolation probe"})
    with world["pool_b"].connection() as c:
        assert c.execute("SELECT count(*) AS n FROM mcp_trace WHERE query = 'isolation probe'"
                         ).fetchone()["n"] == 1
    assert _diff(before_a, _snapshot(world["pool_a"])) == []


def _vault_rows(pool) -> int:
    with pool.connection() as c:
        return c.execute("SELECT count(*) AS n FROM nodes WHERE source = 'obsidian' "
                         "OR content LIKE %s", (f"%{TAG_A} vault%",)).fetchone()["n"]


def _upserted(text: str) -> int:
    return json.loads(json.loads(text)["result"]["content"][0]["text"])["upserted"]


def test_sync_source_is_owner_only(world):
    """The vault adapter is the owner's: tenant b's sync is refused and b's database stays
    byte-identical with no vault note in it; the owner's own sync works (positive control)
    and puts the note only in the owner's database."""
    client, pool_a, pool_b = world["client"], world["pool_a"], world["pool_b"]
    b = _Session(client, TENANT_B)
    before_b = _snapshot(pool_b)
    for args in ({"source": "obsidian"}, {"source": "obsidian", "dry_run": True}):
        err, text = b.call("sync_source", args)
        assert err and "not configured" in text, (args, text[:300])
        assert VAULT_MARK not in text.lower(), text[:300]
    assert _diff(before_b, _snapshot(pool_b)) == [] and _vault_rows(pool_b) == 0

    owner = _Session(client, OWNER)
    err, text = owner.call("sync_source", {"source": "obsidian", "dry_run": True})
    assert not err and _upserted(text) == 1, text[:300]
    before_b = _snapshot(pool_b)
    err, text = owner.call("sync_source", {"source": "obsidian"})
    assert not err and _upserted(text) == 1, text[:300]
    assert _vault_rows(pool_a) == 1
    assert _diff(before_b, _snapshot(pool_b)) == [] and _vault_rows(pool_b) == 0
