import json
import os
import threading
import time

import psycopg
import pytest
from fastapi.testclient import TestClient

from kb_core import llm_jobs as J
from kb_core.gateway import app as G
from kb_core.gateway.app import create_gateway
from kb_core.gateway.auth import signed_headers
from kb_core.gateway.limits import TokenBucket
from kb_core.ingest.inbox import Inbox
from kb_core.tenancy import TenantPools
from kb_core.tenants import Registry, Tenant
from tests.conftest import TEST_DATABASE_URL, tenant_login

URL_B = os.environ.get("KB_TEST_DATABASE_URL_B", TEST_DATABASE_URL + "2")
SA, SB = "a" * 64, "b" * 64


def _registry() -> Registry:
    db_a, db_b = TEST_DATABASE_URL.rsplit("/", 1)[1], URL_B.rsplit("/", 1)[1]
    owner = Tenant("owner", frozenset({"m@example.com"}), database=db_a)
    a = Tenant("a", frozenset({"a@example.com"}), database=db_a, agent_secret=SA, **tenant_login())
    b = Tenant("b", frozenset({"b@example.com"}), database=db_b, agent_secret=SB, **tenant_login())
    return Registry(owner, {"a": a, "b": b}, env={"KB_DATABASE_URL": TEST_DATABASE_URL})


@pytest.fixture
def gw(kb, pool):
    try:
        psycopg.connect(URL_B, connect_timeout=3).close()
    except Exception:
        pytest.skip("KB_TEST_DATABASE_URL_B unreachable")
    pools = TenantPools(_registry(), max_size=2, env={"KB_DATABASE_URL": TEST_DATABASE_URL})
    b = pools.get_by_slug("b")
    with b.pool.connection() as c:
        c.execute("TRUNCATE nodes CASCADE"); c.execute("TRUNCATE ingest_inbox"); c.execute("TRUNCATE llm_jobs")
    with pool.connection() as c:
        c.execute("TRUNCATE llm_jobs")
    app = create_gateway(pools, pools.registry, rate_per_minute=1000, poll_s=0.05)
    yield TestClient(app), pools
    pools.close()


def _post(client, secret, tenant, path, body: dict):
    raw = json.dumps(body).encode()
    return client.post(path, content=raw, headers=signed_headers(secret, tenant, "POST", path, raw))


def _get(client, secret, tenant, path, query=""):
    return client.get(path + query, headers=signed_headers(secret, tenant, "GET", path, b"",
                                                           query=query.lstrip("?")))


def test_token_bucket_refills_over_time():
    t = [0.0]
    b = TokenBucket(2, 60.0, clock=lambda: t[0])
    assert b.take() and b.take() and not b.take()
    t[0] = 30.0
    assert b.take() and not b.take()


def test_notes_are_staged_in_the_signing_tenants_inbox_only(gw, pool):
    client, pools = gw
    ev = {"type": "upsert", "external_id": "e1", "rel_path": "n.md", "text": "# N\n", "mtime": "2026-10-09T10:00:00+00:00"}
    r = _post(client, SA, "a", "/v1/notes", {"events": [ev, {"type": "delete", "external_id": "e2"},
                                                       {"type": "upsert", "external_id": "bad"}]})
    assert r.status_code == 202
    assert r.json()["queued"] == 2 and r.json()["rejected"][0]["external_id"] == "bad"
    a_events = Inbox(pool).claim_pending("vault")
    assert [(e.event_type, e.external_id) for e in a_events] == [("upsert", "e1"), ("delete", "e2")]
    assert a_events[0].payload["external_id"] == "e1" and a_events[0].payload["rel_path"] == "n.md"
    assert Inbox(pools.get_by_slug("b").pool).claim_pending("vault") == []


def test_tenant_mismatch_and_bad_signature_are_401(gw):
    client, _ = gw
    raw = json.dumps({"events": []}).encode()
    h = signed_headers(SB, "a", "POST", "/v1/notes", raw)            # b's key, a's name
    assert client.post("/v1/notes", content=raw, headers=h).status_code == 401
    h = signed_headers(SA, "a", "POST", "/v1/notes", raw); h["X-KB-Signature"] = "0" * 64
    assert client.post("/v1/notes", content=raw, headers=h).status_code == 401


def test_oversize_text_and_too_many_events(gw):
    client, _ = gw
    big = {"type": "upsert", "external_id": "x", "rel_path": "x.md", "text": "x" * (1024 * 1024 + 1), "mtime": "2026-01-01T00:00:00+00:00"}
    r = _post(client, SA, "a", "/v1/notes", {"events": [big]})
    assert r.status_code == 202 and r.json()["rejected"][0]["reason"].startswith("text over")
    r = _post(client, SA, "a", "/v1/notes", {"events": [{"type": "delete", "external_id": str(i)} for i in range(101)]})
    assert r.status_code == 400


def test_manifest_is_staged_with_generation_and_allow_empty(gw, pool):
    client, _ = gw
    assert _post(client, SA, "a", "/v1/manifest", {"ids": ["e1"], "generation": 3}).status_code == 202
    [ev] = Inbox(pool).claim_pending("vault")
    assert ev.event_type == "manifest" and ev.payload == {"ids": ["e1"], "generation": 3, "allow_empty": False}
    assert _post(client, SA, "a", "/v1/manifest", {"ids": "nope", "generation": 1}).status_code == 400
    r = _post(client, SA, "a", "/v1/manifest", {"ids": ["e1"], "generation": -1})
    assert r.status_code == 400 and r.json()["detail"] == "generation must be a non-negative integer"
    assert _post(client, SA, "a", "/v1/manifest", {"ids": [], "generation": 0}).status_code == 202   # 0 is fine
    assert [e.payload["generation"] for e in Inbox(pool).claim_pending("vault")] == [3, 0]           # -1 never staged


def test_stale_claim_token_is_409(gw, pool):
    client, _ = gw
    with pool.connection() as c:
        jid = J.enqueue(c, task="doc_kind", system_prompt="S", user_prompt="U", schema={"type": "object"})
    r = _get(client, SA, "a", "/v1/jobs", "?max=2&wait=1")
    [job] = r.json()["jobs"]
    assert job["id"] == jid and job["task"] == "doc_kind" and job["schema"] == {"type": "object"} and job["claim_token"]
    assert _get(client, SA, "a", "/v1/jobs", "?max=2&wait=0").json()["jobs"] == []
    r = _post(client, SA, "a", f"/v1/jobs/{jid}", {"claim_token": "stale", "status": "done", "result": "x"})
    assert r.status_code == 409                                         # stale claim token
    r = _post(client, SA, "a", f"/v1/jobs/{jid}", {"claim_token": job["claim_token"], "status": "done", "result": '{"kind":"meeting"}'})
    assert r.status_code == 200
    with pool.connection() as c:
        assert J.get_outcome(c, jid).status == "done"


def test_claim_payload_carries_the_producers_deadline(gw, pool):
    """The agent sizes each run to the producer's remaining window."""
    client, _ = gw
    with pool.connection() as c:
        jid = J.enqueue(c, task="t", system_prompt="S", user_prompt="U", timeout_s=600)
    [job] = _get(client, SA, "a", "/v1/jobs", "?max=1&wait=0").json()["jobs"]
    assert job["id"] == jid and 590 < job["deadline_in_s"] <= 600
    assert job["deadline"].endswith("+00:00")


def test_crashed_agents_claim_is_reclaimed_inside_the_producer_window(gw, pool):
    client, _ = gw
    with pool.connection() as c:
        jid = J.enqueue(c, task="t", system_prompt="S", user_prompt="U", timeout_s=600)
    [first] = _get(client, SA, "a", "/v1/jobs", "?max=1&wait=0").json()["jobs"]
    with pool.connection() as c:                       # just past the agent's run timeout + margin
        c.execute("UPDATE llm_jobs SET claimed_at = now() - make_interval(secs => %s) "
                  "WHERE id = %s", (J.STALE_CLAIM_S + 1, jid))
    [again] = _get(client, SA, "a", "/v1/jobs", "?max=1&wait=0").json()["jobs"]
    assert again["id"] == jid and again["claim_token"] != first["claim_token"]
    r = _post(client, SA, "a", f"/v1/jobs/{jid}", {"claim_token": first["claim_token"], "status": "done", "result": "late"})
    assert r.status_code == 409                                         # the crashed agent's late result


def test_jobs_are_per_tenant(gw, pool):
    client, pools = gw
    with pools.get_by_slug("b").pool.connection() as c:
        J.enqueue(c, task="t", system_prompt="S", user_prompt="U")
    assert _get(client, SA, "a", "/v1/jobs", "?max=4&wait=0").json()["jobs"] == []
    assert len(_get(client, SB, "b", "/v1/jobs", "?max=4&wait=0").json()["jobs"]) == 1


def test_failed_with_retry_after_round_trips(gw, pool):
    client, _ = gw
    with pool.connection() as c:
        jid = J.enqueue(c, task="t", system_prompt="S", user_prompt="U")
    [job] = _get(client, SA, "a", "/v1/jobs", "?max=1&wait=0").json()["jobs"]
    r = _post(client, SA, "a", f"/v1/jobs/{jid}", {"claim_token": job["claim_token"], "status": "failed",
                                                   "error": "quota", "retry_after": "2026-10-09T18:00:00+00:00"})
    assert r.status_code == 200
    with pool.connection() as c:
        out = J.get_outcome(c, jid)
    assert out.status == "failed" and out.retry_after.isoformat().startswith("2026-10-09T18:00:00")


def test_status_reports_counts_and_last_seen(gw, pool):
    client, _ = gw
    body = _get(client, SA, "a", "/v1/status").json()
    assert body["tenant"] == "a" and "server_time" in body and body["inbox_pending"] == 0
    assert body["agent_last_seen_at"] is not None                     # the call itself touched it


def test_rate_limit_returns_429(kb, pool):
    pools = TenantPools(_registry(), max_size=2, env={"KB_DATABASE_URL": TEST_DATABASE_URL})
    client = TestClient(create_gateway(pools, pools.registry, rate_per_minute=2))
    try:
        codes = [_post(client, SA, "a", "/v1/manifest", {"ids": ["x"], "generation": i}).status_code for i in range(3)]
        assert codes == [202, 202, 429]
    finally:
        pools.close()


@pytest.mark.parametrize("field, value, reason", [
    ("mtime", "yesterday", "mtime must be ISO 8601"),
    ("mtime", 1700000000, "mtime required (ISO 8601)"),
    ("birthtime", "not-a-date", "birthtime must be ISO 8601"),
    ("birthtime", 1700000000, "birthtime must be ISO 8601"),
])
def test_unparseable_timestamps_are_rejected_per_event(gw, pool, field, value, reason):
    client, _ = gw
    ok = {"type": "upsert", "external_id": "ok", "rel_path": "ok.md", "text": "t",
          "mtime": "2026-10-09T10:00:00Z", "birthtime": "2026-10-01T09:00:00Z"}
    bad = {**ok, "external_id": "bad", "rel_path": "bad.md", field: value}
    r = _post(client, SA, "a", "/v1/notes", {"events": [ok, bad]})
    assert r.status_code == 202
    assert r.json() == {"queued": 1, "rejected": [{"external_id": "bad", "reason": reason}]}
    [ev] = Inbox(pool).claim_pending("vault")
    assert ev.external_id == "ok" and ev.payload["birthtime"] == "2026-10-01T09:00:00Z"


def test_oversize_body_is_413_before_auth(gw):
    client, _ = gw
    raw = b"x" * (8 * 1024 * 1024 + 1)
    h = signed_headers(SA, "a", "POST", "/v1/notes", raw)
    assert client.post("/v1/notes", content=raw, headers=h).status_code == 413
    h["X-KB-Signature"] = "0" * 64                                      # unsigned junk: still 413, not 401
    assert client.post("/v1/notes", content=raw, headers=h).status_code == 413
    chunks = (raw[i:i + 65536] for i in range(0, len(raw), 65536))     # chunked: no Content-Length to trust
    assert client.post("/v1/notes", content=chunks, headers=h).status_code == 413


def test_headers_are_checked_before_the_body_is_read(gw, monkeypatch):
    """The gateway must not read up to 8 MiB before looking at who is asking. A request
    naming no known tenant, or carrying a stale timestamp or no nonce, is refused from
    its headers alone."""
    client, _ = gw
    reads = []
    real = G._read_capped

    async def spy(request, max_body=G.MAX_BODY):
        reads.append(max_body)
        return await real(request, max_body)
    monkeypatch.setattr(G, "_read_capped", spy)
    raw = b"x" * 1024
    h = signed_headers(SA, "nobody", "POST", "/v1/notes", raw)
    assert client.post("/v1/notes", content=raw, headers=h).status_code == 401
    h = signed_headers(SA, "a", "POST", "/v1/notes", raw, ts=int(time.time()) - 3600)
    assert client.post("/v1/notes", content=raw, headers=h).status_code == 401
    h = signed_headers(SA, "a", "POST", "/v1/notes", raw); del h["X-KB-Nonce"]
    assert client.post("/v1/notes", content=raw, headers=h).status_code == 401
    assert reads == []
    assert _post(client, SA, "a", "/v1/notes", {"events": []}).status_code == 202
    assert reads == [G.MAX_BODY]


def test_a_get_with_a_body_is_413_before_auth(gw):
    client, _ = gw
    h = signed_headers(SA, "a", "GET", "/v1/status", b"x")
    assert client.request("GET", "/v1/status", content=b"x", headers=h).status_code == 413


def test_a_replayed_request_is_401(gw):
    """Without a nonce, a captured request could be resent for five minutes."""
    client, _ = gw
    raw = json.dumps({"events": []}).encode()
    h = signed_headers(SA, "a", "POST", "/v1/notes", raw)
    assert client.post("/v1/notes", content=raw, headers=h).status_code == 202
    assert client.post("/v1/notes", content=raw, headers=h).status_code == 401
    h = signed_headers(SA, "a", "GET", "/v1/status", b"")
    assert client.get("/v1/status", headers=h).status_code == 200
    assert client.get("/v1/status", headers=h).status_code == 401


def test_a_rewritten_query_is_401(gw):
    """The query string is signed, so ?max= and ?wait= cannot be changed."""
    client, _ = gw
    h = signed_headers(SA, "a", "GET", "/v1/jobs", b"", query="max=1&wait=0")
    assert client.get("/v1/jobs?max=16&wait=0", headers=h).status_code == 401
    h = signed_headers(SA, "a", "GET", "/v1/jobs", b"", query="max=1&wait=0")
    assert client.get("/v1/jobs?max=1&wait=0", headers=h).status_code == 200


def test_heartbeat_stores_the_agent_id_and_drops_junk(gw, pool):
    client, _ = gw
    with pool.connection() as c:
        c.execute("UPDATE agent_state SET last_seen_at = NULL, agent_version = NULL, hostname = NULL")
    h = signed_headers(SA, "a", "GET", "/v1/status", b"", version="0.1.0")
    h["X-KB-Agent-Id"] = "3f2a9c0d1e4b4f5a8b6c7d8e9f0a1b2c"
    assert client.get("/v1/status", headers=h).status_code == 200
    with pool.connection() as c:
        row = c.execute("SELECT agent_version, hostname FROM agent_state").fetchone()
    assert row["agent_version"] == "0.1.0" and row["hostname"] == "3f2a9c0d1e4b4f5a8b6c7d8e9f0a1b2c"
    assert G._agent_field("x" * 65) is None and G._agent_field("a b") is None and G._agent_field(None) is None


def test_bad_job_result_bodies_are_400(gw, pool):
    client, _ = gw
    with pool.connection() as c:
        jid = J.enqueue(c, task="t", system_prompt="S", user_prompt="U")
    [job] = _get(client, SA, "a", "/v1/jobs", "?max=1&wait=0").json()["jobs"]
    tok = job["claim_token"]
    assert _post(client, SA, "a", f"/v1/jobs/{jid}", {"claim_token": tok, "status": "maybe"}).status_code == 400
    r = _post(client, SA, "a", f"/v1/jobs/{jid}", {"claim_token": tok, "status": "failed", "retry_after": "soon"})
    assert r.status_code == 400
    with pool.connection() as c:
        assert J.get_outcome(c, jid).status == "claimed"                # neither touched the claim


def test_agent_state_is_touched_at_most_once_per_interval(gw, pool):
    client, _ = gw
    with pool.connection() as c:
        c.execute("UPDATE agent_state SET last_seen_at = NULL")
    _get(client, SA, "a", "/v1/status")
    with pool.connection() as c:
        assert J.agent_last_seen(c) is not None
        c.execute("UPDATE agent_state SET last_seen_at = NULL")
    _get(client, SA, "a", "/v1/status")                                 # within 30 s: no second write
    with pool.connection() as c:
        assert J.agent_last_seen(c) is None


def test_long_poll_returns_a_job_enqueued_while_waiting(gw, pool):
    client, _ = gw

    def _later():
        time.sleep(0.3)
        with pool.connection() as c:
            J.enqueue(c, task="late", system_prompt="S", user_prompt="U")
    th = threading.Thread(target=_later); th.start()
    t0 = time.monotonic()
    [job] = _get(client, SA, "a", "/v1/jobs", "?max=1&wait=5").json()["jobs"]
    th.join()
    assert job["task"] == "late" and time.monotonic() - t0 < 4


def test_one_tenant_cannot_hold_more_than_its_inflight_cap(gw):
    """Five concurrent long-polls from a: four hold a slot, one is refused at once, and
    b is still served while a's four are parked (one tenant cannot drain the threadpool)."""
    client, _ = gw
    results: list = []
    with TestClient(client.app) as c:                                  # one event loop for every request
        def poll():
            r = _get(c, SA, "a", "/v1/jobs", "?max=1&wait=2")
            results.append((r.status_code, r.headers.get("retry-after"), r.json()))
        threads = [threading.Thread(target=poll) for _ in range(5)]
        for th in threads:
            th.start()
        time.sleep(0.5)
        assert _get(c, SB, "b", "/v1/status").status_code == 200
        for th in threads:
            th.join()
        assert sorted(s for s, _, _ in results) == [200, 200, 200, 200, 429]
        [(_, retry_after, body)] = [x for x in results if x[0] == 429]
        assert retry_after == "1" and body["detail"] == "too many concurrent requests"
        assert _get(c, SA, "a", "/v1/jobs", "?max=1&wait=0").status_code == 200    # slots released


def test_failed_heartbeat_does_not_fail_the_request(gw, monkeypatch):
    client, _ = gw
    calls = []

    def boom(conn, **kw):
        calls.append(kw)
        raise psycopg.OperationalError("agent_state unavailable")
    monkeypatch.setattr(J, "touch_agent", boom)
    assert _get(client, SA, "a", "/v1/status").status_code == 200
    assert _get(client, SA, "a", "/v1/status").status_code == 200
    assert len(calls) == 2                                              # a failed touch is retried next request


def test_gateway_serves_no_docs_or_schema():
    """/docs, /redoc and /openapi.json were once served unauthenticated on the tenants'
    only network surface. Nothing but the five signed routes answers."""
    pools = TenantPools(_registry(), max_size=2, env={"KB_DATABASE_URL": TEST_DATABASE_URL})
    try:
        app = create_gateway(pools, pools.registry)
        client = TestClient(app)
        for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
            assert client.get(path).status_code == 404, path
        from fastapi.routing import APIRoute
        assert [r.path for r in app.routes if not isinstance(r, APIRoute)] == []
    finally:
        pools.close()


def test_out_of_range_job_id_is_422(gw):
    client, _ = gw
    body = {"claim_token": "t", "status": "done", "result": "x"}
    assert _post(client, SA, "a", "/v1/jobs/0", body).status_code == 422
    assert _post(client, SA, "a", f"/v1/jobs/{2 ** 63}", body).status_code == 422


def test_non_string_external_id_is_rejected_per_note(gw, pool):
    client, _ = gw
    r = _post(client, SA, "a", "/v1/notes", {"events": [{"type": "delete", "external_id": ["x"]},
                                                       {"type": "delete", "external_id": {"k": 1}},
                                                       {"type": "delete", "external_id": 7},
                                                       {"type": "delete", "external_id": "ok"}]})
    assert r.status_code == 202 and r.json()["queued"] == 1
    assert r.json()["rejected"] == [{"external_id": "", "reason": "external_id must be a string"}] * 3
    assert [e.external_id for e in Inbox(pool).claim_pending("vault")] == ["ok"]
