"""Multi-tenancy release gate: with one tenant's credentials, nothing of another tenant is
reachable through the dashboard or the agent gateway, and a tenant's writes land only in
its own database.

Everything here runs the PRODUCTION auth path against two live databases: dashboard
requests carry RS256 Access JWTs checked by `_dashboard_verifier` (only the JWKS fetch is
replaced by a local key), the tenant is resolved from the verified email by the real
registry and `_TenantMiddleware`, and gateway requests are HMAC-signed with the real
`signed_headers`. No tenant resolution is monkeypatched.

A failure here is a release blocker: fix the surface, never the test.

Coverage is enforced, not hoped for: every dashboard route must be classified below, so a
new route fails `test_every_dashboard_route_is_classified` until someone decides how it is
isolation-tested."""
import contextlib
import faulthandler
import http.server
import io
import json
import os
import sys
import tarfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import jwt
import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from dashboard.api.app import _dashboard_verifier, create_app
from kb_core import llm_jobs as J
from kb_core.domain import NodeRecord
from kb_core.gateway.app import create_gateway
from kb_core.gateway.auth import signed_headers
from kb_core.ingest.inbox import Inbox
from kb_core.tenancy import TenantPools
from kb_core.tenants import Registry, Tenant
from tests.conftest import TEST_DATABASE_URL, tenant_login

URL_B = os.environ.get("KB_TEST_DATABASE_URL_B", TEST_DATABASE_URL + "2")
SA, SB = "a" * 64, "b" * 64
AUD = "aud-isolation"
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)

OWNER, TENANT_A, TENANT_B = "m@x.test", "a@x.test", "b@x.test"
# Tags are distinctive on purpose: every seeded name, title, body, query and reason
# carries its tenant's tag, so one case-insensitive substring check finds any leak.
TAG_A, TAG_B = "alphaz", "bravoz"


# --------------------------------------------------------------------------- bounds
# Every blocking wait in this gate is bounded: a hang must FAIL the gate, never stall it.

HANG_S = 240        # backstop: no single test may run this long


@pytest.fixture(autouse=True)
def _hang_backstop():
    """Anything unbounded that slips past the explicit timeouts below: dump every
    thread's stack and abort the run instead of sleeping forever."""
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


@contextlib.contextmanager
def _lifespan_client(app):
    """A TestClient with lifespan (one event loop for every request), whose startup and
    shutdown are both bounded -- a shutdown that hangs fails here, it does not stall."""
    client = TestClient(app)
    _bounded(client.__enter__, 30, "lifespan startup")
    try:
        yield client
    finally:
        _bounded(lambda: client.__exit__(None, None, None), 30, "lifespan shutdown")


def _map_bounded(fn, items, seconds: float = 60, workers: int = 8) -> list:
    ex = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = [ex.submit(fn, i) for i in items]
        return [f.result(timeout=seconds) for f in futures]
    finally:
        ex.shutdown(wait=False, cancel_futures=True)


# --------------------------------------------------------------------------- helpers

def _jwt(email: str) -> str:
    now = int(time.time())
    return jwt.encode({"email": email, "aud": AUD, "iat": now, "exp": now + 600,
                       "iss": "https://iso.cloudflareaccess.com"},
                      _KEY, algorithm="RS256")


def _h(email: str, **extra) -> dict:
    return {"Cf-Access-Jwt-Assertion": _jwt(email), "X-DB-Password": "pw", **extra}


def _svc_h(client_id: str) -> dict:
    """A device's Access service token: no email, the Client ID in `common_name`."""
    now = int(time.time())
    return {"Cf-Access-Jwt-Assertion": jwt.encode(
        {"common_name": client_id, "sub": "", "aud": AUD, "iat": now, "exp": now + 600,
         "iss": "https://iso.cloudflareaccess.com"}, _KEY, algorithm="RS256")}


_ARCHIVES = ("/api/library/skills", "/api/library/agents")


def _body_text(path: str, r) -> str:
    """What a response says: a library archive's decoded member text (its gzip bytes
    would hide a leak from the substring check), any other response's text."""
    if path in _ARCHIVES:
        with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tf:
            return "\n".join(tf.extractfile(m).read().decode() for m in tf.getmembers())
    return r.text


def _wipe(pool) -> None:
    """Tenant B's database gets the same per-test reset the shared `kb` fixture gives A."""
    with pool.connection() as c:
        c.execute("TRUNCATE nodes CASCADE")
        c.execute("TRUNCATE ingest_log, ingest_inbox, mcp_tool_state, entity_instance_blocks, "
                  "provider_pause, sync_state, llm_jobs")
        c.execute("TRUNCATE mcp_trace CASCADE")
        c.execute("TRUNCATE concept_runs, concept_run_requests, concepts, concept_members, "
                  "concept_constraints, concept_member_overrides, concept_separations")
        c.execute("UPDATE agent_state SET last_seen_at = NULL, agent_version = NULL, "
                  "hostname = NULL")
        c.execute("UPDATE age_decay_state SET enabled = true")


def _seed(kb, tag: str) -> dict:
    """One of everything a tenant can own and a surface can address, every label tagged."""
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
    klass = f"{tag}class"
    e1 = kb.upsert_entity(f"{t}Ent", categories=[klass])
    e2 = kb.upsert_entity(f"{t}Entb", categories=[klass])
    kb.link_entity(note.id, e1.node.id, "mentions")
    kb.link_entity(two.id, e2.node.id, "mentions")
    left, right = sorted([e1.node.id, e2.node.id], key=str)
    cand = kb.stage_merge_candidate(left, right, "instance", 0.1, f"{tag} same", left)
    run = kb.begin_concept_run({})
    kb.apply_concept_run(run, [
        {"key": "c0", "prev_id": None, "parent_key": None, "level": 0,
         "name": f"{t}Concept", "kind": "project", "description": f"{tag} work",
         "members": [(str(note.id), "memory", 1.0), (str(e1.node.id), "entity", 0.9)],
         "embedding": None},
        {"key": "c1", "prev_id": None, "parent_key": None, "level": 0,
         "name": f"{t}Other", "kind": "topic", "description": f"{tag} other",
         "members": [(str(two.id), "memory", 1.0)], "embedding": None},
    ], [])
    kb.finish_concept_run(run, status="done", stats={})
    concepts = {c["name"]: c["id"] for c in kb.active_concepts()}
    c1, c2 = concepts[f"{t}Concept"], concepts[f"{t}Other"]
    constraint = kb.add_concept_constraint("must_link", e1.node.id, e2.node.id, note=f"{tag} rule")
    separation = kb.add_concept_separation(c1, c2, note=f"{tag} apart")
    kb.log_trace(tool="search_memories", args={"query": f"{tag} secret"}, latency_ms=1,
                 query=f"{tag} secret", result_count=1,
                 results=[{"id": str(note.id), "title": note.title, "rank": 1}])
    with kb._pool.connection() as c:
        trace_id = c.execute("SELECT max(id) AS id FROM mcp_trace").fetchone()["id"]
        J.enqueue(c, task="doc_kind", system_prompt=f"{tag} sys", user_prompt=f"{tag} prompt")
    kb.add_judgment(trace_id, verdict="good", judged_by=f"{tag}@judge", notes=f"{tag} judged")
    kb.publish_tool_inventory([("search_memories", "read")])     # so the gate flip has a row
    skill = f"{tag}-skill"
    with kb._pool.connection() as c:
        # skills is not reset per test; CASCADE reaches only skill_revisions / skill_revision_files.
        # One table holds both kinds, so this clears agents too.
        c.execute("TRUNCATE skills CASCADE")
    kb.upsert_skill(skill, None, {"SKILL.md": f"---\nname: {skill}\ndescription: {tag} skill.\n"
                                              f"---\n\n{tag} body\n"}, f"{tag} seed")
    # approved, so the library download carries the tagged skill (pending items are not served)
    kb.review_skill(skill, 1, kb.get_skill(skill, 1)["package_hash"], True, f"{tag} review")
    agent = f"{tag}-agent"
    kb.upsert_agent(agent, None, f"---\nname: {agent}\ndescription: {tag} agent.\n---\n\n"
                                 f"{tag} body\n", f"{tag} seed")
    kb.review_agent(agent, 1, kb.get_agent(agent, 1)["package_hash"], True, f"{tag} review")
    # usage traces in the shape KB_MCP writes them, each carrying the tag, so a cross-tenant
    # leak of a count, a missed query or a missing-skill name is caught by the leak check
    kb.log_trace(tool="get_skill", args={"name": skill, "revision": None}, latency_ms=1,
                 result_count=1, trace={"revision": 1})
    kb.log_trace(tool="search_skills", args={"query": f"{tag} nothing", "cursor": None},
                 latency_ms=1, query=f"{tag} nothing", result_count=0, trace={"skills": []})
    kb.log_trace(tool="get_skill", args={"name": f"{tag}-absent", "revision": None},
                 latency_ms=1, error=f"not_found: skill '{tag}-absent' does not exist")
    ids = [note.id, two.id, e1.node.id, e2.node.id, c1, c2]
    return {"tag": tag, "note": note, "two": two, "e1": e1.node, "e2": e2.node,
            "klass": klass, "kind": f"{tag}kind", "concept": c1, "concept2": c2,
            "cand": cand, "trace": trace_id, "constraint": constraint["id"],
            "separation": separation["id"], "skill": skill, "agent": agent, "ids": {str(i).lower() for i in ids}}


def _leaks(text: str, other: dict, sent=()) -> list[str]:
    """The other tenant's markers in a response. What the caller itself sent (an id, a
    path, a name echoed back in a 404 detail) is cut out first: echoing the request is
    not a leak, but anything the server adds from the other database still is. Only
    strings of 6+ characters are cut (a short one like "1003" or "db" could carve a hole
    in a UUID marker), and never the bare tag."""
    low = text.lower()
    cut = {str(x).lower() for x in sent if x} - {other["tag"]}
    for s in sorted((c for c in cut if len(c) >= 6), key=len, reverse=True):
        low = low.replace(s, "")
    return [m for m in (other["tag"], *other["ids"]) if m in low]


def _sent(url: str, payload=None) -> list[str]:
    """Every value a request carried: its path segments, query values and body strings."""
    path, _, query = url.partition("?")
    out = [seg for seg in path.split("/") if seg]
    out += [kv.partition("=")[2] for kv in query.split("&") if kv]
    stack = [payload]
    while stack:
        v = stack.pop()
        if isinstance(v, dict):
            stack.extend(v.values())
        elif isinstance(v, list):
            stack.extend(v)
        elif isinstance(v, str):
            out.append(v)
    return out


def _snapshot(pool) -> dict[str, str]:
    """A digest of EVERY row of EVERY table: the other tenant's database must come out of a
    test byte-identical, whatever the surface tried."""
    with pool.connection() as c:
        tables = [r["tablename"] for r in c.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "AND tablename <> 'schema_migrations' ORDER BY 1")]
        return {t: c.execute(f"SELECT md5(coalesce(string_agg(x::text, '|' ORDER BY x::text), '')) "
                             f"AS h FROM {t} x").fetchone()["h"] for t in tables}


def _diff(before: dict, after: dict) -> list[str]:
    return sorted(t for t in before.keys() | after.keys() if before.get(t) != after.get(t))


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


@pytest.fixture(autouse=True)
def _inference_up(monkeypatch):
    """A stand-in inference server for `/healthz`, which the read sweep calls like every
    other parameter-free GET. Without one, `/healthz` reports the inference server down
    (503, as designed) wherever none runs (CI), or probes the developer's live one. Only
    inference reachability is faked; tenant resolution is not touched."""
    class _Health(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200 if self.path == "/health" else 404)
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Health)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("KB_INFERENCE_URL", f"http://127.0.0.1:{server.server_port}")
    yield
    server.shutdown()
    server.server_close()


@pytest.fixture
def world(kb):
    _require_db_b()
    db_a, db_b = TEST_DATABASE_URL.rsplit("/", 1)[1], URL_B.rsplit("/", 1)[1]
    # owner and "a" share database A (a second agent-bearing tenant on it, so the gateway
    # has a non-owner signer there); "b" is alone on database B.
    reg = Registry(Tenant("owner", frozenset({OWNER}), database=db_a),
                   {"a": Tenant("a", frozenset({TENANT_A}), database=db_a, agent_secret=SA,
                                library_tokens=frozenset({"iso-a.access"}), **tenant_login()),
                    "b": Tenant("b", frozenset({TENANT_B}), database=db_b, agent_secret=SB,
                                library_tokens=frozenset({"iso-b.access"}), **tenant_login())},
                   env={"KB_DATABASE_URL": TEST_DATABASE_URL})
    pools = TenantPools(reg, max_size=4, env={"KB_DATABASE_URL": TEST_DATABASE_URL})
    b = pools.get_by_slug("b")
    _wipe(b.pool)
    A, B = _seed(kb, TAG_A), _seed(b.kb, TAG_B)
    verifier = _dashboard_verifier(
        {"KB_DASHBOARD_AUTH": "cf-access", "KB_CF_ACCESS_TEAM_DOMAIN": "iso.cloudflareaccess.com",
         "KB_CF_ACCESS_AUD_DASHBOARD": AUD},
        key_for_token=lambda _tok: _KEY.public_key())
    dash = create_app(pools, registry=reg, verifier=verifier, db_viewer_password="pw")
    gw = create_gateway(pools, reg, rate_per_minute=10_000, poll_s=0.05)
    yield {"dash": dash, "gw": gw, "pools": pools, "A": A, "B": B,
           "pool_a": kb._pool, "pool_b": b.pool, "kb_a": kb, "kb_b": b.kb,
           # identity -> (its own seed, the other tenant's seed, the other tenant's pool)
           "who": {OWNER: (A, B, b.pool), TENANT_A: (A, B, b.pool),
                   TENANT_B: (B, A, kb._pool)}}
    pools.close()


# --------------------------------------------------------------------------- dashboard

def _get_routes(app, *, params: bool) -> set[str]:
    return {r.path for r in app.routes
            if "GET" in (getattr(r, "methods", None) or ()) and ("{" in r.path) == params}


def _write_routes(app) -> set[tuple[str, str]]:
    return {(m, r.path) for r in app.routes for m in (getattr(r, "methods", None) or ())
            if m in ("POST", "PATCH", "PUT", "DELETE")}


# Query strings for parameter-free GETs whose useful answer needs one; the value always
# names the OTHER tenant's data (filled in per identity).
_QUERY = {"/api/subtree": "?path={other_note_path}", "/api/doc-kinds": "?all=true"}

# GET routes with path parameters, each pointed at the OTHER tenant's record.
# (path template, status rule) -- "404": a node/concept UUID the caller's database cannot
# hold, so anything but 404 is a leak; "4xx": refused one way or another (a path where an
# id is expected); "any": a name or integer id that may legitimately exist in both
# databases, so only the body is checked.
_PARAM_GETS = {
    "/api/classes/{name}": [("/api/classes/{klass}", "any")],
    "/api/classes/{name}/voters": [("/api/classes/{klass}/voters", "any")],
    "/api/doc-kinds/{kind}/memories": [("/api/doc-kinds/{kind}/memories", "any")],
    "/api/expand/{any_id}": [("/api/expand/{note}", "404"), ("/api/expand/{e1}", "404"),
                             ("/api/expand/{concept}", "404")],
    "/api/node/{node_id}": [("/api/node/{note}", "404"), ("/api/node/{note_path}", "4xx"),
                            ("/api/node/{e1}", "404")],
    # an id this database lacks answers an empty page, so only the body is checked
    "/api/node/{node_id}/memories": [("/api/node/{e1}/memories", "any")],
    "/api/retrieval/traces/{trace_id}": [("/api/retrieval/traces/{trace}", "any")],
    "/api/retrieval/traces/{trace_id}/replay": [("/api/retrieval/traces/{trace}/replay", "any")],
    # a turn id is its opener trace id; a foreign one is a 404 in this database
    "/api/retrieval/turns/{turn_id}": [("/api/retrieval/turns/{turn}", "any")],
    "/api/db/table/{schema}/{name}": [("/api/db/table/public/{table}", "any")],
    # a skill name the caller's registry lacks is a 404; usage of one answers zeros
    "/api/skills/{name}": [("/api/skills/{skill}", "404")],
    "/api/skills/{name}/files/{path:path}": [("/api/skills/{skill}/files/SKILL.md?revision=1", "404")],
    "/api/skills/{name}/usage": [("/api/skills/{skill}/usage", "any")],
    "/api/agents/{name}": [("/api/agents/{agent}", "404")],
}

# Write routes, each aimed at the OTHER tenant's records. UUID/path-addressed ones must
# not succeed; every one must leave the other database byte-identical.
_WRITES = [
    ("POST", "/api/node", {"content": "w", "title": "IsoWrite", "parent_path": ""}, False),
    ("PATCH", "/api/node/{note}", {"title": "pwned", "content": "pwned"}, True),
    ("PATCH", "/api/node/{note_path}", {"title": "pwned"}, True),
    # DELETE is idempotent: an id this database lacks answers {"deleted": 0}
    ("DELETE", "/api/node/{note}?recursive=true", None, False),
    ("DELETE", "/api/node/{note_path}", None, False),
    ("POST", "/api/node/{note}/move", {"new_parent_path": "elsewhere"}, True),
    ("POST", "/api/node/{note}/retire", {"reason": "pwned"}, True),
    ("POST", "/api/node/{note}/revive", None, True),
    ("POST", "/api/node/{note}/analysis-exclude", {"retract": True}, True),
    ("POST", "/api/node/{note}/analysis-include", None, True),
    ("POST", "/api/node/{note}/significance", {"value": 3.0}, True),
    ("POST", "/api/node/{note}/reanalyze", None, True),
    ("POST", "/api/entity/{e1}/reparent", {"new_category": "pwnedclass"}, True),
    ("POST", "/api/concepts/{concept}/label", {"name": "pwned"}, True),
    ("POST", "/api/concepts/{concept}/dismiss", None, True),
    ("POST", "/api/concepts/{concept}/restore", None, True),
    ("POST", "/api/concepts/{concept}/members/{note}/remove", None, True),
    ("POST", "/api/concepts/{concept}/members/{note}/pin", None, True),
    ("DELETE", "/api/concepts/{concept}/members/{note}/override", None, False),  # idempotent
    ("POST", "/api/concepts/constraints", {"kind": "cannot_link", "left_id": "{e1}",
                                           "right_id": "{e2}"}, True),
    ("DELETE", "/api/concepts/constraints/{constraint}", None, False),
    ("POST", "/api/concepts/separations", {"left_id": "{concept}",
                                           "right_id": "{concept2}"}, True),
    ("DELETE", "/api/concepts/separations/{separation}", None, False),
    ("POST", "/api/concepts/run", None, False),
    ("POST", "/api/concepts/curate/apply", {"ops": [{"op": "dismiss", "concept_id": "{concept}"},
                                                    {"op": "label", "concept_id": "{concept}",
                                                     "name": "pwned"}]}, False),
    ("PATCH", "/api/classes/{klass}", {"description": "pwned"}, False),
    ("POST", "/api/classes/{klass}/block", {"purge_instances": True}, False),
    ("POST", "/api/classes/{klass}/unblock", None, False),
    ("POST", "/api/classes/{klass}/merge", {"target": "pwnedclass"}, False),
    ("POST", "/api/merge-suggestions/{cand}/approve", {"target_id": "{e1}"}, False),
    ("POST", "/api/merge-suggestions/{cand}/reject", {"reason": "pwned"}, False),
    ("POST", "/api/retrieval/traces/{trace}/judgment", {"verdict": "bad"}, False),
    ("POST", "/api/retrieval/turns/{turn}/judgment", {"verdict": "bad"}, False),
    ("POST", "/api/ingest/retry-failed", {"node_ids": ["{note}"]}, False),
    ("POST", "/api/ingest/stuck/dismiss", {"source": "agent", "external_id": "{tag}1"}, False),
    ("POST", "/api/aging", {"enabled": False}, False),
    ("POST", "/api/mcp/tools/{tool}", {"enabled": False}, False),
    ("POST", "/api/db/auth", None, False),
    ("POST", "/api/curation/{action}/preview", "curation", False),
    ("POST", "/api/curation/{action}", "curation", False),
]
# Owner-only routes that drive the HOST, not tenant data: never fired from a test.
_NOT_FIRED = {("POST", "/api/auth/claude/relogin"),        # re-runs the host's claude login
              ("POST", "/api/concepts/curate/parse")}      # an LLM call; parses text, no ids


def _fill(template: str, other: dict) -> str:
    return template.format(
        note=other["note"].id, note_path=other["note"].path, e1=other["e1"].id,
        e2=other["e2"].id, concept=other["concept"], concept2=other["concept2"],
        klass=other["klass"], kind=other["kind"], trace=other["trace"],
        turn=other["trace"], skill=other["skill"], agent=other["agent"],
        cand=other["cand"], constraint=other["constraint"],
        separation=other["separation"], tag=other["tag"], table="nodes",
        tool="search_memories", other_note_path=other["note"].path, action="{action}")


def _fill_body(body, other: dict):
    if isinstance(body, str):
        return _fill(body, other)
    if isinstance(body, list):
        return [_fill_body(b, other) for b in body]
    if isinstance(body, dict):
        return {k: _fill_body(v, other) for k, v in body.items()}
    return body


# test placeholder -> the route's own parameter name, to map _WRITES back onto app.routes
_ROUTE_PARAM = {"{note}": "{node_id}", "{note_path}": "{node_id}", "{e1}": "{entity_id}",
                "{klass}": "{name}", "{concept}": "{concept_id}", "{constraint}": "{cid}",
                "{separation}": "{sid}", "{cand}": "{cid}", "{trace}": "{trace_id}",
                "{turn}": "{turn_id}", "{agent}": "{name}"}


def _route_of(path: str) -> str:
    path = path.split("?")[0]
    for placeholder, param in _ROUTE_PARAM.items():
        path = path.replace(placeholder, param)
    return path


def test_every_dashboard_route_is_classified(world):
    app = world["dash"]
    assert set(_PARAM_GETS) == _get_routes(app, params=True)
    classified = {(m, _route_of(p)) for m, p, _b, _s in _WRITES} | _NOT_FIRED
    assert _write_routes(app) == classified, _write_routes(app) ^ classified
    assert {r.path for r in app.routes if not getattr(r, "methods", None)} == {"/api/ws"}


def test_dashboard_refuses_unauthenticated_and_unmapped_callers(world):
    client = TestClient(world["dash"])
    assert client.get("/api/tree").status_code == 401
    assert client.get("/api/tree", headers={"Cf-Access-Jwt-Assertion": "garbage"}).status_code == 401
    assert client.get("/api/tree", headers=_h("stranger@x.test")).status_code == 403


def test_dashboard_reads_are_siloed(world):
    """Every parameter-free GET, as every identity: no byte of the other tenant appears,
    while the caller's own data does (so an empty answer cannot pass for isolation)."""
    client = TestClient(world["dash"])
    sweep = sorted(_get_routes(world["dash"], params=False))
    assert len(sweep) >= 25, sweep
    for email, (own, other, _pool) in world["who"].items():
        seen_own = set()
        for path in sweep:
            url = path + _fill(_QUERY.get(path, ""), other)
            r = client.get(url, headers=_h(email))
            assert r.status_code < 500, (email, url, r.status_code, r.text[:300])
            text = _body_text(path, r)
            assert not _leaks(text, other, _sent(url)), (email, url, text[:300])
            if own["tag"] in text.lower():
                seen_own.add(path)
        # positive control: the caller's own seeded data is visible on its core views
        assert {"/api/tree", "/api/entities", "/api/concepts", "/api/retrieval/traces",
                "/api/merge-suggestions", "/api/provenance", "/api/agents",
                *_ARCHIVES} <= seen_own, (email, seen_own)
        assert "/api/library/client.py" in sweep


def test_device_tokens_download_only_their_tenants_library(world):
    """A device credential (Access service token) maps to the tenant that lists it, reads
    only that tenant's library, and is refused every other route."""
    client = TestClient(world["dash"])
    for cid, own, other in (("iso-a.access", world["A"], world["B"]),
                            ("iso-b.access", world["B"], world["A"])):
        for path in _ARCHIVES:
            r = client.get(path, headers=_svc_h(cid))
            assert r.status_code == 200, (cid, path, r.status_code)
            text = _body_text(path, r)
            assert own["tag"] in text.lower() and not _leaks(text, other), (cid, path, text[:300])
        for path in ("/api/tree", "/api/skills"):
            assert client.get(path, headers=_svc_h(cid)).status_code == 403, (cid, path)
    assert client.get("/api/library/skills", headers=_svc_h("iso-z.access")).status_code == 403


def test_dashboard_cannot_read_the_other_tenants_records_by_id(world):
    client = TestClient(world["dash"])
    for email, (own, other, _pool) in world["who"].items():
        for templates in _PARAM_GETS.values():
            for template, rule in templates:
                url = _fill(template, other)
                r = client.get(url, headers=_h(email))
                assert r.status_code < 500, (email, url, r.status_code, r.text[:300])
                assert not _leaks(r.text, other, _sent(url)), (email, url, r.text[:300])
                if rule == "404":
                    assert r.status_code == 404, (email, url, r.status_code, r.text[:300])
                elif rule == "4xx":
                    assert 400 <= r.status_code < 500, (email, url, r.status_code)
        # positive control: the same routes answer for the caller's OWN records
        assert client.get(_fill("/api/node/{note}", own), headers=_h(email)).status_code == 200
        assert client.get(_fill("/api/expand/{e1}", own), headers=_h(email)).status_code == 200
        assert client.get(_fill("/api/skills/{skill}", own), headers=_h(email)).status_code == 200
        assert client.get(_fill("/api/agents/{agent}", own), headers=_h(email)).status_code == 200
        assert client.get(_fill("/api/skills/{skill}/files/SKILL.md?revision=1", own),
                          headers=_h(email)).status_code == 200
        assert client.get(_fill("/api/skills/{skill}/usage", own),
                          headers=_h(email)).json()["loads"] >= 1
        assert client.get(_fill("/api/skills/{skill}/usage", other),
                          headers=_h(email)).json()["loads"] == 0


def _fire_writes(client, email: str, other: dict) -> list[tuple]:
    out = []
    for method, path, body, must_fail in _WRITES:
        if body == "curation":
            cmds = client.get("/api/curation/commands", headers=_h(email)).json()["commands"]
            calls = [(_fill(path, other).replace("{action}", c["action"]),
                      {a: (other["klass"] if a != "description" else "pwned")
                       for a in c["args"]}) for c in cmds]
        else:
            calls = [(_fill(path, other), _fill_body(body, other))]
        for url, payload in calls:
            r = client.request(method, url, headers=_h(email),
                               **({"json": payload} if payload is not None else {}))
            out.append((method, url, r.status_code))
            assert r.status_code < 500, (email, method, url, r.status_code, r.text[:300])
            if must_fail:
                assert r.status_code >= 400, (email, method, url, r.status_code, r.text[:300])
            assert not _leaks(r.text, other, _sent(url, payload)), (email, method, url,
                                                                    r.text[:300])
    return out


@pytest.mark.parametrize("email", [OWNER, TENANT_A, TENANT_B])
def test_dashboard_writes_cannot_touch_the_other_tenant(world, email):
    """Every write route, aimed at the other tenant's ids, paths and names: the other
    database is byte-identical afterwards, and the caller's create lands in its own."""
    client = TestClient(world["dash"], raise_server_exceptions=False)
    own, other, other_pool = world["who"][email]
    own_kb = world["kb_b"] if email == TENANT_B else world["kb_a"]
    other_kb = world["kb_a"] if email == TENANT_B else world["kb_b"]
    before = _snapshot(other_pool)
    fired = _fire_writes(client, email, other)
    assert len(fired) >= len(_WRITES)
    assert _diff(before, _snapshot(other_pool)) == []
    assert own_kb.get_by_path("IsoWrite") is not None
    assert other_kb.get_by_path("IsoWrite") is None


def test_dashboard_websocket_relays_only_its_tenants_changes(world):
    """Writes land in BOTH databases, alternately; each identity's socket must carry its
    own database's node ids and never one of the other's."""
    # lifespan: the LISTEN hubs stop at exit, within a bound
    with _lifespan_client(world["dash"]) as client:
        _bounded(lambda: _websocket_round(world, client), 90, "websocket round")


def _websocket_round(world, client) -> None:
    kbs = {"a": world["kb_a"], "b": world["kb_b"]}
    written: dict[str, set[str]] = {"a": set(), "b": set()}
    stop = threading.Event()

    def _writer():
        i = 0
        while not stop.wait(0.1) and i < 300:
            for side in ("b", "a"):
                n = kbs[side].upsert(NodeRecord(source="agent", external_id=f"ws{side}{i}",
                                                type="note", content=f"w{i}",
                                                slug=f"ws_{side}_{i}", title=f"Ws{side}{i}"))
                written[side].add(str(n.id))
            i += 1

    def _drain(ws, side: str, other: str, got: list[str]) -> None:
        """Read until this side has seen 4 of its own writes (so at least three full
        rounds in which the other side wrote while this listener was up), or until a
        foreign id arrives -- a leak ends the wait at once instead of hanging it."""
        while sum(i in written[side] for i in got) < 4:
            got.append(str(json.loads(ws.receive_text()).get("id")))
            if got[-1] in written[other]:
                return

    t = threading.Thread(target=_writer, daemon=True)
    t.start()
    try:
        for email, side, other in ((OWNER, "a", "b"), (TENANT_B, "b", "a")):
            got: list[str] = []
            with client.websocket_connect(
                    "/api/ws", headers={"Cookie": f"CF_Authorization={_jwt(email)}"}) as ws:
                d = threading.Thread(target=_drain, args=(ws, side, other, got), daemon=True)
                d.start()
                d.join(timeout=15)
                leaked = set(got) & written[other]
                assert not leaked, (email, "relayed the other tenant's writes", leaked)
                assert not d.is_alive(), (email, "relayed none of its own writes in 15 s", got)
    finally:
        stop.set()
        t.join(timeout=10)


def test_dashboard_concurrent_identities_never_cross(world):
    """Interleaved requests from both tenants on ONE event loop: the tenant binding is a
    contextvar, so a binding that leaked across tasks would show up here."""
    own_note = {OWNER: world["A"]["note"].id, TENANT_B: world["B"]["note"].id}
    with _lifespan_client(world["dash"]) as client:
        def call(i: int):
            email = (OWNER, TENANT_B)[i % 2]
            path = ("/api/tree", f"/api/node/{own_note[email]}", "/api/entities")[i % 3]
            r = client.get(path, headers=_h(email))
            return email, path, r.status_code, r.text
        results = _map_bounded(call, range(60))
    for email, path, status, text in results:
        own, other, _ = world["who"][email]
        assert status == 200, (email, path, status)
        assert own["tag"] in text.lower() and not _leaks(text, other), (email, path)


# --------------------------------------------------------------------------- gateway

def _gpost(client, secret, slug, path, body: dict):
    raw = json.dumps(body).encode()
    return client.post(path, content=raw, headers=signed_headers(secret, slug, "POST", path, raw))


def _gget(client, secret, slug, path, query=""):
    return client.get(path + query, headers=signed_headers(secret, slug, "GET", path, b"",
                                                           query=query.lstrip("?")))


_GATEWAY_ROUTES = {("POST", "/v1/notes"), ("POST", "/v1/manifest"), ("GET", "/v1/jobs"),
                   ("POST", "/v1/jobs/{job_id}"), ("GET", "/v1/status")}
# FastAPI's own schema/docs pages carry no tenant data; create_gateway disables them all
# (test_gateway_app pins that), so this is only the ceiling for non-API routes.
_GATEWAY_DOCS = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}


def test_gateway_route_inventory_is_all_authed(world):
    """Every gateway API route is one of the five known ones AND depends on `authed`: a
    new route (or one that forgot the dependency) fails here until it is gated."""
    from fastapi.routing import APIRoute
    app = world["gw"]
    api = [r for r in app.routes if isinstance(r, APIRoute)]
    assert {(m, r.path) for r in api for m in r.methods} == _GATEWAY_ROUTES
    for r in api:
        names = {d.call.__name__ for d in r.dependant.dependencies}
        assert "authed" in names, (r.path, names)
    assert {r.path for r in app.routes if not isinstance(r, APIRoute)} <= _GATEWAY_DOCS


def test_gateway_credentials_cannot_name_another_tenant(world):
    """Every route, both directions: one tenant's key under the other's name is a 401."""
    client = TestClient(world["gw"])
    before = {"a": _snapshot(world["pool_a"]), "b": _snapshot(world["pool_b"])}
    note = {"type": "upsert", "external_id": "x", "rel_path": "x.md", "text": "# X",
            "mtime": "2026-01-01T00:00:00+00:00"}
    job = {"claim_token": "t", "status": "done", "result": "pwned"}
    tried = set()
    for key, name in ((SA, "b"), (SB, "a")):
        for path, body in (("/v1/notes", {"events": [note]}),
                           ("/v1/manifest", {"ids": [], "generation": 9, "allow_empty": True}),
                           ("/v1/jobs/1", job)):
            assert _gpost(client, key, name, path, body).status_code == 401, (name, path)
            tried.add(("POST", path.replace("/1", "/{job_id}")))
        for path, query in (("/v1/jobs", "?wait=0"), ("/v1/status", "")):
            assert _gget(client, key, name, path, query).status_code == 401, (name, path)
            tried.add(("GET", path))
    assert tried == _GATEWAY_ROUTES
    assert _gget(client, SA, "owner", "/v1/status").status_code == 401      # owner has no secret
    assert _diff(before["a"], _snapshot(world["pool_a"])) == []
    assert _diff(before["b"], _snapshot(world["pool_b"])) == []


def test_gateway_writes_land_only_in_the_signers_database(world):
    client = TestClient(world["gw"])
    before_b = _snapshot(world["pool_b"])
    note = {"type": "upsert", "external_id": "ga", "rel_path": "ga.md", "text": "# GA",
            "mtime": "2026-01-01T00:00:00+00:00"}
    assert _gpost(client, SA, "a", "/v1/notes", {"events": [note]}).status_code == 202
    assert _gpost(client, SA, "a", "/v1/manifest", {"ids": ["ga"], "generation": 1}).status_code == 202
    status = _gget(client, SA, "a", "/v1/status").json()
    assert status["tenant"] == "a" and status["inbox_pending"] == 2
    # b's database: untouched (not even agent_state -- a's heartbeat is a's)
    assert _diff(before_b, _snapshot(world["pool_b"])) == []
    assert _gget(client, SB, "b", "/v1/status").json()["inbox_pending"] == 0
    assert [e.external_id for e in Inbox(world["pool_a"]).claim_pending("vault")] == ["ga", None]
    assert Inbox(world["pool_b"]).claim_pending("vault") == []


def test_gateway_jobs_are_siloed(world):
    """a never sees b's queue, and cannot complete b's job even holding b's claim token."""
    client = TestClient(world["gw"])
    pool_a, pool_b = world["pool_a"], world["pool_b"]
    # each seed enqueued one job; a's queue holds only a's
    [ja] = _gget(client, SA, "a", "/v1/jobs", "?max=16&wait=0").json()["jobs"]
    assert TAG_A in ja["user_prompt"] and TAG_B not in json.dumps(ja)
    assert _gget(client, SA, "a", "/v1/jobs", "?max=16&wait=0").json()["jobs"] == []
    [jb] = _gget(client, SB, "b", "/v1/jobs", "?max=16&wait=0").json()["jobs"]
    assert TAG_B in jb["user_prompt"] and TAG_A not in json.dumps(jb)
    # a posts b's job id with b's VALID claim token: must not land anywhere
    body = {"claim_token": jb["claim_token"], "status": "done", "result": "pwned"}
    assert _gpost(client, SA, "a", f"/v1/jobs/{jb['id']}", body).status_code == 409
    fail = {"claim_token": jb["claim_token"], "status": "failed", "error": "pwned"}
    assert _gpost(client, SA, "a", f"/v1/jobs/{jb['id']}", fail).status_code == 409
    with pool_b.connection() as c:
        assert J.get_outcome(c, jb["id"]).status == "claimed"
    with pool_a.connection() as c:
        assert c.execute("SELECT count(*) AS n FROM llm_jobs WHERE result = 'pwned' "
                         "OR error = 'pwned'").fetchone()["n"] == 0
    # positive control: the rightful tenant completes it with the same token
    ok = {"claim_token": jb["claim_token"], "status": "done", "result": "fine"}
    assert _gpost(client, SB, "b", f"/v1/jobs/{jb['id']}", ok).status_code == 200
    with pool_b.connection() as c:
        assert J.get_outcome(c, jb["id"]).status == "done"


def test_gateway_inflight_cap_is_per_tenant(world):
    """a parking all four of its long-poll slots does not cost b a single request."""
    results: list[tuple[int, list]] = []
    with _lifespan_client(world["gw"]) as c:
        # take a's one seeded job first, so all four pollers below park for the full wait
        [mine] = _gget(c, SA, "a", "/v1/jobs", "?max=16&wait=0").json()["jobs"]
        assert TAG_A in mine["user_prompt"]

        def poll():
            r = _gget(c, SA, "a", "/v1/jobs", "?max=16&wait=2")
            results.append((r.status_code, r.json().get("jobs")))
        threads = [threading.Thread(target=poll) for _ in range(4)]
        for th in threads:
            th.start()
        time.sleep(0.5)
        assert _gget(c, SA, "a", "/v1/status").status_code == 429       # a really is full
        assert [_gget(c, SB, "b", "/v1/status").status_code for _ in range(4)] == [200] * 4
        for th in threads:
            th.join(timeout=30)
            assert not th.is_alive(), "a parked long-poll never returned"
    assert results == [(200, [])] * 4                    # b's pending job never offered to a
    with world["pool_b"].connection() as c:
        assert c.execute("SELECT count(*) AS n FROM llm_jobs WHERE status = 'pending'"
                         ).fetchone()["n"] == 1


def test_seeds_are_distinct_databases(world):
    """Guard the guard: the two seeds really live in two databases, or every assertion
    above would be vacuous."""
    with world["pool_a"].connection() as c:
        db_a = c.execute("SELECT current_database() AS d").fetchone()["d"]
    with world["pool_b"].connection() as c:
        db_b = c.execute("SELECT current_database() AS d").fetchone()["d"]
    assert db_a != db_b
    assert world["kb_a"].get(UUID(str(world["B"]["note"].id))) is None
    assert world["kb_b"].get(UUID(str(world["A"]["note"].id))) is None
