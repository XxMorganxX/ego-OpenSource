import os
import threading
import time
from types import SimpleNamespace

import jwt
import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from dashboard.api.app import _dashboard_verifier, create_app
from kb_core.cf_access import AuthRejected
from kb_core.domain import NodeRecord
from kb_core.tenancy import TenantPools
from kb_core.tenants import Registry, Tenant
from tests.conftest import TEST_DATABASE_URL, tenant_login

URL_B = os.environ.get("KB_TEST_DATABASE_URL_B", TEST_DATABASE_URL + "2")


class _FakeVerifier:
    """token == the email it stands for; anything else is rejected."""
    def verify(self, token):
        if not token or "@" not in token:
            raise AuthRejected(401, "nope")
        return token


class _BrokenVerifier:
    def verify(self, token):
        raise RuntimeError("verifier exploded")


def _registry() -> Registry:
    db_a, db_b = TEST_DATABASE_URL.rsplit("/", 1)[1], URL_B.rsplit("/", 1)[1]
    owner = Tenant("owner", frozenset({"m@example.com"}), database=db_a)
    b = Tenant("b", frozenset({"b@example.com"}), database=db_b, agent_secret="b" * 64, **tenant_login())
    return Registry(owner, {"b": b}, env={"KB_DATABASE_URL": TEST_DATABASE_URL})


@pytest.fixture
def two(kb):
    try:
        psycopg.connect(URL_B, connect_timeout=3).close()
    except Exception:
        pytest.skip("KB_TEST_DATABASE_URL_B unreachable")
    pools = TenantPools(_registry(), max_size=2, env={"KB_DATABASE_URL": TEST_DATABASE_URL})
    b = pools.get_by_slug("b")
    with b.pool.connection() as c:
        c.execute("TRUNCATE nodes CASCADE")
        c.execute("TRUNCATE mcp_tool_state")
    kb.upsert(NodeRecord(source="agent", external_id="o1", type="note", content="o",
                         slug="owner_note", title="OwnerNote"))
    b.kb.upsert(NodeRecord(source="agent", external_id="b1", type="note", content="b",
                           slug="b_note", title="BNote"))
    client = TestClient(create_app(pools, registry=pools.registry, verifier=_FakeVerifier(),
                                   db_viewer_password="pw"))
    yield client, pools, kb, b.kb
    pools.close()


def H(email):
    return {"Cf-Access-Jwt-Assertion": email}


def test_single_kb_mode_is_unchanged(kb):
    client = TestClient(create_app(kb))
    assert client.get("/api/tree").status_code == 200
    assert client.get("/api/me").json() == {"email": "owner", "tenant": "owner", "is_owner": True}


def test_missing_or_bad_token_is_401_and_unknown_email_is_403(two):
    client, *_ = two
    assert client.get("/api/tree").status_code == 401
    assert client.get("/api/tree", headers=H("garbage")).status_code == 401
    assert client.get("/api/tree", headers=H("stranger@example.com")).status_code == 403
    assert client.get("/healthz").status_code in (200, 503)        # never gated


def test_a_raising_verifier_fails_closed_with_500(kb):
    pools = TenantPools(_registry(), max_size=2, env={"KB_DATABASE_URL": TEST_DATABASE_URL})
    try:
        client = TestClient(create_app(pools, verifier=_BrokenVerifier()))
        r = client.get("/api/tree", headers=H("m@example.com"))
        assert r.status_code == 500
        assert r.json() == {"detail": "auth check failed"}
    finally:
        pools.close()


def test_each_identity_sees_only_its_own_tree(two):
    client, *_ = two
    owner_paths = {r["path"] for r in client.get("/api/tree", headers=H("m@example.com")).json()}
    b_paths = {r["path"] for r in client.get("/api/tree", headers=H("b@example.com")).json()}
    assert "owner_note" in owner_paths and "b_note" not in owner_paths
    assert "b_note" in b_paths and "owner_note" not in b_paths


def test_writes_land_in_the_identitys_database(two):
    client, pools, owner_kb, b_kb = two
    # body per dashboard/api/schemas.py: CreateIn
    r = client.post("/api/node", json={"content": "hello", "title": "FromB", "parent_path": ""},
                    headers=H("b@example.com"))
    assert r.status_code in (200, 201), r.text
    assert b_kb.get_by_path("FromB") is not None and owner_kb.get_by_path("FromB") is None


def test_cookie_fallback_works_for_the_websocket_upgrade_and_plain_requests(two):
    client, *_ = two
    assert client.get("/api/tree",
                      headers={"Cookie": "CF_Authorization=b@example.com"}).status_code == 200


def test_websocket_upgrade_is_gated_and_relays_the_identitys_database(two):
    client, _pools, _owner_kb, b_kb = two
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/ws") as ws:
            ws.receive_text()
    with client.websocket_connect(
            "/api/ws", headers={"Cookie": "CF_Authorization=b@example.com"}) as ws:
        # Writes land ONLY in b's database, so a notification arriving proves this
        # identity's hub LISTENs on b's database. Keep writing until the listener is up.
        stop = threading.Event()

        def _writer():
            i = 0
            while not stop.wait(0.2) and i < 50:
                b_kb.upsert(NodeRecord(source="agent", external_id=f"ws{i}", type="note",
                                       content=f"w{i}", slug=f"ws_{i}", title=f"Ws{i}"))
                i += 1

        t = threading.Thread(target=_writer, daemon=True)
        t.start()
        try:
            assert ws.receive_text()
        finally:
            stop.set()
            t.join(timeout=5)


def test_owner_only_routes_are_403_for_tenants(two):
    client, *_ = two
    pw = {**H("b@example.com"), "X-DB-Password": "pw"}
    for method, path, kwargs in [
        ("post", "/api/mcp/tools/add_memory", {"json": {"enabled": False}, "headers": pw}),
        ("get", "/api/auth/claude", {"headers": H("b@example.com")}),
        ("post", "/api/auth/claude/relogin", {"headers": pw}),
        ("post", "/api/db/auth", {"headers": pw}),
        ("get", "/api/db/tables", {"headers": pw}),
        ("get", "/api/db/table/public/nodes", {"headers": pw}),
        ("get", "/api/retrieval/evals", {"headers": H("b@example.com")}),
        ("post", "/api/concepts/curate/parse", {"json": {"text": "x"}, "headers": H("b@example.com")}),
        ("post", "/api/concepts/curate/apply", {"json": {"ops": []}, "headers": H("b@example.com")}),
        ("post", "/api/curation/pin", {"json": {"name": "x"}, "headers": H("b@example.com")}),
        ("post", "/api/curation/pin/preview", {"json": {"name": "x"}, "headers": H("b@example.com")}),
    ]:
        assert getattr(client, method)(path, **kwargs).status_code == 403, path
    assert client.get("/api/auth/claude", headers=H("m@example.com")).status_code == 200


def test_acting_identity_is_the_principal_email(two):
    client, _pools, owner_kb, _b_kb = two
    owner_kb.publish_tool_inventory([("add_memory", "write")])
    r = client.post("/api/mcp/tools/add_memory", json={"enabled": False},
                    headers={**H("m@example.com"), "X-DB-Password": "pw"})
    assert r.status_code == 200, r.text
    assert r.json()["updated_by"] == "m@example.com"


def test_me_and_agent_last_seen(two):
    client, *_ = two
    assert client.get("/api/me", headers=H("b@example.com")).json() == {
        "email": "b@example.com", "tenant": "b", "is_owner": False}
    assert client.get("/api/me", headers=H("m@example.com")).json()["is_owner"] is True
    body = client.get("/api/ingest/status", headers=H("b@example.com")).json()
    assert "agent_last_seen_at" in body


_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _signed(email: str) -> str:
    now = int(time.time())
    return jwt.encode({"email": email, "aud": "aud-dash", "iat": now, "exp": now + 600,
                       "iss": "https://t.cloudflareaccess.com"},
                      _KEY, algorithm="RS256")


def test_production_verifier_lets_an_unmapped_email_reach_the_403(two):
    _client, pools, *_ = two
    env = {"KB_DASHBOARD_AUTH": "cf-access", "KB_CF_ACCESS_TEAM_DOMAIN": "t.cloudflareaccess.com",
           "KB_CF_ACCESS_AUD_DASHBOARD": "aud-dash"}
    verifier = _dashboard_verifier(env, key_for_token=lambda tok: _KEY.public_key())
    client = TestClient(create_app(pools, registry=pools.registry, verifier=verifier))
    assert client.get("/api/me", headers=H(_signed("stranger@example.com"))).status_code == 403
    r = client.get("/api/me", headers=H(_signed("b@example.com")))
    assert r.status_code == 200 and r.json()["tenant"] == "b"
    assert _dashboard_verifier({}) is None                      # auth off unless asked for


def test_repeated_cookie_headers_are_joined(two):
    client, *_ = two
    r = client.get("/api/me", headers=[("cookie", "CF_Authorization=b@example.com"),
                                       ("cookie", "theme=dark")])
    assert r.status_code == 200 and r.json()["tenant"] == "b"


class _TeapotVerifier:
    def verify(self, token):
        raise AuthRejected(418, "teapot")


def test_an_unknown_rejection_status_fails_closed_as_401(kb):
    pools = TenantPools(_registry(), max_size=2, env={"KB_DATABASE_URL": TEST_DATABASE_URL})
    try:
        client = TestClient(create_app(pools, verifier=_TeapotVerifier()))
        r = client.get("/api/tree", headers=H("m@example.com"))
        assert r.status_code == 401 and r.json() == {"detail": "unauthorized"}
    finally:
        pools.close()


def test_aging_flip_is_stamped_with_the_principal(two):
    client, _pools, owner_kb, _b = two
    try:
        r = client.post("/api/aging", json={"enabled": False}, headers=H("m@example.com"))
        assert r.status_code == 200 and r.json()["updated_by"] == "m@example.com"
    finally:
        owner_kb.set_age_decay(True)


# ---- no verifier + tenants in the registry = refuse, never "everyone is the owner" ----

def _owner_only_registry(*, disabled_b: bool = False) -> Registry:
    db_a = TEST_DATABASE_URL.rsplit("/", 1)[1]
    owner = Tenant("owner", frozenset({"m@example.com"}), database=db_a)
    tenants = {}
    if disabled_b:
        tenants["b"] = Tenant("b", frozenset({"b@example.com"}), database="kb_t_b",
                              agent_secret="b" * 64, **tenant_login(), enabled=False)
    return Registry(owner, tenants, env={"KB_DATABASE_URL": TEST_DATABASE_URL})


def test_create_app_refuses_enabled_tenants_without_a_verifier():
    from dashboard.api.app import DashboardAuthRequired
    pools = TenantPools(_registry(), max_size=2, env={"KB_DATABASE_URL": TEST_DATABASE_URL})
    try:
        with pytest.raises(DashboardAuthRequired, match="KB_DASHBOARD_AUTH"):
            create_app(pools, verifier=None)
        create_app(pools, verifier=_FakeVerifier())              # with a verifier: fine
    finally:
        pools.close()
    for reg in (_owner_only_registry(), _owner_only_registry(disabled_b=True)):
        pools = TenantPools(reg, max_size=2, env={"KB_DATABASE_URL": TEST_DATABASE_URL})
        try:
            create_app(pools, verifier=None)                     # owner only: unchanged
        finally:
            pools.close()


def test_a_tenant_added_by_reload_without_a_verifier_is_refused_per_request(kb):
    reg = _owner_only_registry()
    pools = TenantPools(reg, max_size=2, env={"KB_DATABASE_URL": TEST_DATABASE_URL})
    try:
        client = TestClient(create_app(pools, verifier=None))
        assert client.get("/api/me").status_code == 200           # owner-only: served
        reg._tenants["b"] = _registry().by_slug("b")               # what a hot reload does
        r = client.get("/api/me")
        assert r.status_code == 503 and "owner" not in r.text
    finally:
        pools.close()


def test_main_refuses_to_start_with_tenants_and_no_dashboard_auth(monkeypatch, capsys):
    import dashboard.api.app as A
    for key in ("KB_DASHBOARD_AUTH", *_EXPOSURE_KEYS):     # the tenant check, not exposure
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(A, "load_registry", _registry)
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: pytest.fail("dashboard started"))
    with pytest.raises(SystemExit) as exc:
        A.main()
    assert exc.value.code == 1
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and "has enabled tenants" in err[0]


_EXPOSURE_KEYS = ("KB_DASHBOARD_ORIGINS", "KB_FRONTEND_ALLOWED_HOSTS", "KB_API_ALLOWED_HOSTS")


def _main_env(monkeypatch, auth=None, host="kb.example.com"):
    """main() under a controlled env: no DB, no server. Returns the list uvicorn.run
    appends to if the API would have started."""
    import dashboard.api.app as A
    for key in (*_EXPOSURE_KEYS, "KB_DASHBOARD_AUTH"):
        monkeypatch.delenv(key, raising=False)
    if host:
        monkeypatch.setenv("KB_FRONTEND_ALLOWED_HOSTS", host)
    if auth is not None:
        monkeypatch.setenv("KB_DASHBOARD_AUTH", auth)
    started = []
    monkeypatch.setattr(A, "load_registry", _owner_only_registry)
    monkeypatch.setattr(A, "TenantPools", lambda reg: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(A, "create_app", lambda *a, **k: "app")
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: started.append(a))
    return A, started


def test_main_refuses_a_public_hostname_without_dashboard_auth(monkeypatch, capsys):
    A, started = _main_env(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        A.main()
    assert exc.value.code == 1 and started == []
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and err[0].startswith("refusing to start the dashboard API:")
    assert "kb.example.com" in err[0] and "KB_DASHBOARD_AUTH=cf-access" in err[0]


def test_main_starts_locally_without_dashboard_auth(monkeypatch, capsys):
    A, started = _main_env(monkeypatch, host="127.0.0.1:8091")
    A.main()
    assert len(started) == 1 and capsys.readouterr().err == ""


def test_main_only_warns_when_auth_none_declares_another_gate(monkeypatch, capsys):
    A, started = _main_env(monkeypatch, auth="none")
    A.main()
    assert len(started) == 1
    err = capsys.readouterr().err
    assert err.startswith("WARNING:") and "kb.example.com" in err


@pytest.mark.parametrize("bad", ["cf_access", "cloudflare", "off"])
def test_main_refuses_an_unknown_dashboard_auth_mode(monkeypatch, capsys, bad):
    A, started = _main_env(monkeypatch, auth=bad, host=None)
    with pytest.raises(SystemExit) as exc:
        A.main()
    assert exc.value.code == 1 and started == []
    assert f"unknown KB_DASHBOARD_AUTH '{bad}'" in capsys.readouterr().err


def test_skills_routes_serve_only_the_identitys_registry(two):
    client, _pools, kb_a, kb_b = two
    with kb_b._pool.connection() as c:
        c.execute("TRUNCATE skills CASCADE")
        c.execute("TRUNCATE mcp_trace CASCADE")
    md = "---\nname: {n}\ndescription: Example.\n---\n\nBody\n"
    kb_a.upsert_skill("alpha-skill", None, {"SKILL.md": md.format(n="alpha-skill")}, "create")
    kb_b.upsert_skill("beta-skill", None, {"SKILL.md": md.format(n="beta-skill")}, "create")
    kb_a.log_trace(tool="get_skill", args={"name": "alpha-skill"}, latency_ms=1)
    owner, b = H("m@example.com"), H("b@example.com")
    assert [s["name"] for s in client.get("/api/skills", headers=owner).json()["skills"]] \
        == ["alpha-skill"]
    assert [s["name"] for s in client.get("/api/skills", headers=b).json()["skills"]] \
        == ["beta-skill"]
    assert client.get("/api/skills/alpha-skill", headers=b).status_code == 404
    assert client.get("/api/skills/alpha-skill/files/SKILL.md",
                      params={"revision": 1}, headers=b).status_code == 404
    assert client.get("/api/skills/alpha-skill/usage", headers=b).json()["loads"] == 0
    assert client.get("/api/skill-usage", headers=b).json()["per_skill"] == {}
    assert client.get("/api/skill-usage", headers=owner).json()["per_skill"] \
        ["alpha-skill"]["loads"] == 1
