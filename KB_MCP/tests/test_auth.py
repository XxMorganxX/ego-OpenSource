"""Auth wall for the public listener — config parsing, JWT verification, middleware."""
import pytest

from kb_mcp.auth import (
    AuthRejected,
    CfAccessConfig,
    InternalSecretConfig,
    auth_from_env,
    ensure_http_transport,
    public_hostname_from_env,
)


def test_auth_from_env_unset_means_no_auth():
    assert auth_from_env({}) is None
    assert auth_from_env({"KB_MCP_AUTH": ""}) is None


def test_auth_from_env_cf_access_parses_and_normalizes():
    cfg = auth_from_env({
        "KB_MCP_AUTH": "cf-access",
        "KB_CF_ACCESS_TEAM_DOMAIN": "team.cloudflareaccess.com",
        "KB_CF_ACCESS_AUD": "a" * 64,
        "KB_CF_ACCESS_EMAILS": "Avery@Example.com, other@example.com",
    })
    assert isinstance(cfg, CfAccessConfig)
    assert cfg.team_domain == "team.cloudflareaccess.com"
    assert cfg.audience == "a" * 64
    assert cfg.emails == frozenset({"avery@example.com", "other@example.com"})


def test_auth_from_env_cf_access_missing_config_exits():
    with pytest.raises(SystemExit):
        auth_from_env({"KB_MCP_AUTH": "cf-access",
                       "KB_CF_ACCESS_TEAM_DOMAIN": "team.cloudflareaccess.com"})


def test_auth_from_env_internal_secret_requires_length():
    cfg = auth_from_env({"KB_MCP_AUTH": "internal-secret",
                         "KB_MCP_INTERNAL_SECRET": "f" * 64})
    assert isinstance(cfg, InternalSecretConfig)
    with pytest.raises(SystemExit):  # short secret = misconfiguration, not weak-but-working
        auth_from_env({"KB_MCP_AUTH": "internal-secret",
                       "KB_MCP_INTERNAL_SECRET": "short"})


def test_auth_from_env_unknown_mode_exits():
    with pytest.raises(SystemExit):
        auth_from_env({"KB_MCP_AUTH": "basic"})


def test_ensure_http_transport_rejects_auth_on_stdio():
    cfg = InternalSecretConfig(secret="f" * 64)
    ensure_http_transport("streamable-http", cfg)   # fine
    ensure_http_transport("stdio", None)            # fine — no auth requested
    with pytest.raises(SystemExit):
        ensure_http_transport("stdio", cfg)


import time

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from kb_mcp.auth import CfAccessVerifier

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PRIV = _KEY.private_bytes(serialization.Encoding.PEM,
                           serialization.PrivateFormat.PKCS8,
                           serialization.NoEncryption())
_PUB = _KEY.public_key()
_AUD = "a" * 64
_CFG = CfAccessConfig(team_domain="team.cloudflareaccess.com", audience=_AUD,
                      emails=frozenset({"avery@example.com"}))


def _token(email="avery@example.com", aud=_AUD, exp_delta=600):
    now = int(time.time())
    return jwt.encode({"aud": aud, "email": email, "iat": now - 10,
                       "exp": now + exp_delta, "iss": "https://team.cloudflareaccess.com"},
                      _PRIV, algorithm="RS256")


def _verifier(key_for_token=lambda tok: _PUB):
    return CfAccessVerifier(_CFG, key_for_token=key_for_token)


def _rejects(verifier, token, status):
    with pytest.raises(AuthRejected) as exc:
        verifier.verify(token)
    assert exc.value.status == status


def test_verifier_accepts_a_valid_token():
    assert _verifier().verify(_token()) == "avery@example.com"


def test_verifier_rejects_missing_expired_wrong_aud_wrong_email_garbage():
    v = _verifier()
    _rejects(v, None, 401)                                  # no header at all
    _rejects(v, _token(exp_delta=-600), 401)                # expired
    _rejects(v, _token(aud="b" * 64), 401)                  # wrong audience
    _rejects(v, _token(email="mallory@example.com"), 401)   # not allowlisted
    _rejects(v, "not-a-jwt", 401)                           # garbage


def test_verifier_jwks_outage_is_503_fail_closed():
    def down(tok):
        raise jwt.exceptions.PyJWKClientConnectionError("connect timeout")
    _rejects(_verifier(down), _token(), 503)


def test_verifier_unknown_kid_is_401_not_503():
    def no_kid(tok):
        raise jwt.exceptions.PyJWKClientError("kid not found")
    _rejects(_verifier(no_kid), _token(), 401)


import asyncio

from kb_mcp.auth import wrap_with_auth


async def _inner_app(scope, receive, send):
    if scope["type"] != "http":
        scope.setdefault("touched", []).append("lifespan-reached")
        return
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"tool-surface"})


def _call(app, headers: list[tuple[bytes, bytes]]):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    scope = {"type": "http", "method": "POST", "path": "/mcp", "headers": headers}
    asyncio.run(app(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, body


def test_middleware_cf_access_valid_token_reaches_the_app():
    app = wrap_with_auth(_inner_app, _CFG, key_for_token=lambda tok: _PUB)
    status, body = _call(app, [(b"cf-access-jwt-assertion", _token().encode())])
    assert (status, body) == (200, b"tool-surface")


def test_middleware_cf_access_bad_or_missing_token_is_401_neutral_body():
    app = wrap_with_auth(_inner_app, _CFG, key_for_token=lambda tok: _PUB)
    for headers in ([], [(b"cf-access-jwt-assertion", b"garbage")]):
        status, body = _call(app, headers)
        assert status == 401
        assert b"tool-surface" not in body
        assert b"jwt" not in body.lower() and b"email" not in body.lower()  # no detail echo


def test_middleware_jwks_outage_is_503():
    def down(tok):
        raise jwt.exceptions.PyJWKClientConnectionError("connect timeout")
    app = wrap_with_auth(_inner_app, _CFG, key_for_token=down)
    status, _ = _call(app, [(b"cf-access-jwt-assertion", _token().encode())])
    assert status == 503


def test_middleware_internal_secret_right_wrong_missing():
    cfg = InternalSecretConfig(secret="f" * 64)
    app = wrap_with_auth(_inner_app, cfg)
    ok, _ = _call(app, [(b"x-kb-mcp-internal", b"f" * 64)])
    bad, _ = _call(app, [(b"x-kb-mcp-internal", b"e" * 64)])
    missing, _ = _call(app, [])
    assert (ok, bad, missing) == (200, 401, 401)


def test_middleware_lifespan_passes_through_without_auth():
    cfg = InternalSecretConfig(secret="f" * 64)
    app = wrap_with_auth(_inner_app, cfg)
    scope = {"type": "lifespan", "touched": []}
    asyncio.run(app(scope, None, None))
    assert scope["touched"] == ["lifespan-reached"]


def test_middleware_unexpected_check_error_fails_closed_500():
    """A crash inside the check must yield a neutral 500 from the wall itself,
    never escape to the server or reach the app."""
    from kb_mcp.auth import _AuthMiddleware

    def boom(headers):
        raise RuntimeError("kaboom")

    app = _AuthMiddleware(_inner_app, boom)
    status, body = _call(app, [])
    assert status == 500
    assert b"tool-surface" not in body and b"kaboom" not in body


def test_middleware_websocket_scope_is_refused_not_forwarded():
    cfg = InternalSecretConfig(secret="f" * 64)
    app = wrap_with_auth(_inner_app, cfg)
    sent = []

    async def send(msg):
        sent.append(msg)

    scope = {"type": "websocket", "path": "/mcp", "headers": []}
    asyncio.run(app(scope, None, send))
    assert sent == [{"type": "websocket.close", "code": 1008}]
    assert "touched" not in scope  # the inner app must never see the scope


def test_middleware_unknown_scope_type_is_dropped():
    cfg = InternalSecretConfig(secret="f" * 64)
    app = wrap_with_auth(_inner_app, cfg)
    sent = []

    async def send(msg):
        sent.append(msg)

    scope = {"type": "webtransport"}
    asyncio.run(app(scope, None, send))
    assert sent == [] and "touched" not in scope


def test_middleware_internal_secret_non_ascii_header_is_401_not_500():
    """A header byte >= 0x80 must be an ordinary bad credential (401), never a
    crash into the catch-all (500 + traceback an attacker can spam the log with)."""
    cfg = InternalSecretConfig(secret="f" * 64)
    app = wrap_with_auth(_inner_app, cfg)
    status, body = _call(app, [(b"x-kb-mcp-internal", b"\xff\xfe" + b"f" * 62)])
    assert status == 401
    assert b"tool-surface" not in body


# ---- main() dispatch: the auth seam -----------------------------------------
import uvicorn
from types import SimpleNamespace

import kb_mcp.server as server_mod


class _FakeServer:
    def __init__(self):
        self.settings = SimpleNamespace(host="127.0.0.1", port=3199)
        self.ran_transport = None

    def streamable_http_app(self):
        return "raw-app"

    def run(self, transport):
        self.ran_transport = transport


def _wire_main_fakes(monkeypatch):
    """Stub every heavy collaborator so main() runs as pure control flow."""
    fake = _FakeServer()
    calls = {"open_pool": 0, "uvicorn_app": None, "uvicorn_kwargs": None}

    def fake_open_pool(*a, **k):
        calls["open_pool"] += 1
        return "pool"

    def fake_uvicorn_run(app, **kwargs):
        calls["uvicorn_app"] = app
        calls["uvicorn_kwargs"] = kwargs

    monkeypatch.setattr(server_mod, "open_pool", fake_open_pool)
    monkeypatch.setattr(server_mod, "migrate_pool", lambda pool: None)
    monkeypatch.setattr(server_mod, "KnowledgeBase", lambda pool, reg: "kb")
    monkeypatch.setattr(server_mod, "build_server", lambda *a, **k: fake)
    monkeypatch.setattr(server_mod, "wrap_with_auth", lambda app, cfg: ("wrapped", app))
    monkeypatch.setattr(uvicorn, "run", fake_uvicorn_run)
    for var in ("KB_MCP_AUTH", "KB_MCP_INTERNAL_SECRET", "KB_CF_ACCESS_TEAM_DOMAIN",
                "KB_CF_ACCESS_AUD", "KB_CF_ACCESS_EMAILS", "KB_OBSIDIAN_VAULT",
                "KB_INFERENCE_URL", "KB_MCP_PUBLIC_HOSTNAME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("KB_DATABASE_URL", "postgresql://ignored/ignored")
    monkeypatch.setenv("KB_MCP_TRANSPORT", "http")
    return fake, calls


def test_main_serves_wrapped_app_when_auth_set(monkeypatch):
    fake, calls = _wire_main_fakes(monkeypatch)
    monkeypatch.setenv("KB_MCP_AUTH", "internal-secret")
    monkeypatch.setenv("KB_MCP_INTERNAL_SECRET", "f" * 64)
    server_mod.main()
    assert calls["uvicorn_app"] == ("wrapped", "raw-app")
    assert calls["uvicorn_kwargs"]["host"] == "127.0.0.1"
    assert calls["uvicorn_kwargs"]["port"] == 3199
    assert fake.ran_transport is None  # server.run() must NOT also fire


def test_main_serves_plain_transport_when_auth_unset(monkeypatch):
    fake, calls = _wire_main_fakes(monkeypatch)
    server_mod.main()
    assert fake.ran_transport == "streamable-http"
    assert calls["uvicorn_app"] is None  # no wrapping on the local path


def test_main_misconfigured_auth_exits_before_touching_the_db(monkeypatch):
    _fake, calls = _wire_main_fakes(monkeypatch)
    monkeypatch.setenv("KB_MCP_AUTH", "cf-access")  # missing all KB_CF_ACCESS_* config
    with pytest.raises(SystemExit):
        server_mod.main()
    assert calls["open_pool"] == 0


def test_main_refuses_a_public_hostname_without_auth(monkeypatch):
    """A public hostname means "tunnel traffic reaches this listener". Without an
    auth mode that would put the bare tool surface on the internet, so main() must
    die loudly -- before opening pools -- instead of serving it."""
    _fake, calls = _wire_main_fakes(monkeypatch)
    monkeypatch.setenv("KB_MCP_PUBLIC_HOSTNAME", "kb-mcp.example.com")
    with pytest.raises(SystemExit, match="KB_MCP_PUBLIC_HOSTNAME.*KB_MCP_AUTH"):
        server_mod.main()
    assert calls["open_pool"] == 0
    assert _fake.ran_transport is None


def test_main_passes_the_public_hostname_only_with_auth(monkeypatch):
    fake, calls = _wire_main_fakes(monkeypatch)
    seen = {}

    def capture(*a, **k):
        seen.update(k)
        return fake
    monkeypatch.setattr(server_mod, "build_server", capture)
    monkeypatch.setenv("KB_MCP_PUBLIC_HOSTNAME", "kb-mcp.example.com")
    monkeypatch.setenv("KB_MCP_AUTH", "internal-secret")
    monkeypatch.setenv("KB_MCP_INTERNAL_SECRET", "f" * 64)
    server_mod.main()
    assert seen["public_hostname"] == "kb-mcp.example.com"
    assert seen["authenticated"] is True
    assert calls["uvicorn_app"] == ("wrapped", "raw-app")


def test_public_hostname_from_env():
    cfg = InternalSecretConfig(secret="f" * 64)
    assert public_hostname_from_env({}, None) is None
    assert public_hostname_from_env({"KB_MCP_PUBLIC_HOSTNAME": "  "}, None) is None
    assert public_hostname_from_env({}, cfg) is None
    assert public_hostname_from_env(
        {"KB_MCP_PUBLIC_HOSTNAME": " kb-mcp.example.com "}, cfg) == "kb-mcp.example.com"
    with pytest.raises(SystemExit, match="KB_MCP_AUTH"):
        public_hostname_from_env({"KB_MCP_PUBLIC_HOSTNAME": "kb-mcp.example.com"}, None)


# ---- transport security: the SDK's DNS-rebinding guard vs the tunnel Host ----
from kb_mcp.server import build_server


def test_build_server_public_hostname_is_transport_allowed():
    """FastMCP auto-enables DNS-rebinding protection for loopback binds with a
    localhost-only Host allowlist -- tunnel traffic (Host: kb-mcp.example.com)
    would get 421 Misdirected Request AFTER passing the auth wall. A configured
    public hostname must be an allowed Host."""
    srv = build_server(None, host="127.0.0.1", port=3199,
                       public_hostname="kb-mcp.example.com", authenticated=True)
    ts = srv.settings.transport_security
    assert ts is not None and ts.enable_dns_rebinding_protection
    assert "kb-mcp.example.com" in ts.allowed_hosts
    assert "127.0.0.1:*" in ts.allowed_hosts  # local smoke tests keep working


def test_build_server_never_allows_a_public_hostname_on_an_unauthenticated_server():
    """The unauthenticated (local) listener must never accept the tunnel's Host
    header: that is what lets a tunnel pointed at the wrong port serve the bare tool
    surface publicly. build_server refuses the combination outright."""
    with pytest.raises(ValueError, match="unauthenticated"):
        build_server(None, host="127.0.0.1", port=3199,
                     public_hostname="kb-mcp.example.com")
    srv = build_server(None, host="127.0.0.1", port=3199)
    ts = srv.settings.transport_security
    assert ts is None or "kb-mcp.example.com" not in ts.allowed_hosts


@pytest.mark.filterwarnings("ignore:Using `httpx` with `starlette.testclient`")
def test_authed_request_with_public_hostname_is_not_421():
    """End-to-end through the wall + SDK transport: an AUTHENTICATED request with
    the public Host must reach the MCP protocol layer (any protocol-level 4xx is
    fine), and an unauthenticated one must still die at the wall with 401."""
    from starlette.testclient import TestClient

    srv = build_server(None, host="127.0.0.1", port=3199,
                       public_hostname="kb-mcp.example.com", authenticated=True)
    app = wrap_with_auth(srv.streamable_http_app(),
                         InternalSecretConfig(secret="f" * 64))
    with TestClient(app) as client:
        authed = client.post(
            "/mcp", json={},
            headers={"Host": "kb-mcp.example.com",
                     "X-KB-MCP-Internal": "f" * 64,
                     "Accept": "application/json, text/event-stream"})
        assert authed.status_code != 421
        assert authed.status_code in (400, 406)  # protocol layer answered
        unauthed = client.post(
            "/mcp", json={}, headers={"Host": "kb-mcp.example.com"})
        assert unauthed.status_code == 401


# ---- principal: the wall keeps the verified email for the tenant router ----
# The helpers are named `_echo_state_app` / `_call_principal` so they do not
# shadow `_call` above, which the earlier middleware tests resolve from module
# globals at call time (they unpack a (status, body) tuple).


def _echo_state_app():
    seen = {}

    async def app(scope, receive, send):
        seen["principal"] = scope.get("state", {}).get("kb_principal")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})
    return app, seen


def _call_principal(app, headers):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)
    scope = {"type": "http", "path": "/mcp", "headers": headers, "state": {}}
    asyncio.run(app(scope, receive, send))
    return sent[0]["status"]


def test_cf_access_principal_reaches_the_app(monkeypatch):
    import kb_mcp.auth as A
    inner, seen = _echo_state_app()

    class _V:
        def __init__(self, *a, **k): pass

        def verify(self, token):
            if token != "good":
                raise A.AuthRejected(401, "nope")
            return "alice@example.com"
    monkeypatch.setattr(A, "CfAccessVerifier", _V)
    cfg = CfAccessConfig(team_domain="t.cloudflareaccess.com", audience="a" * 64,
                         emails=frozenset({"alice@example.com"}))
    app = wrap_with_auth(inner, cfg)
    assert _call_principal(app, [(b"cf-access-jwt-assertion", b"good")]) == 200
    assert seen["principal"] == "alice@example.com"
    assert _call_principal(app, [(b"cf-access-jwt-assertion", b"bad")]) == 401


def test_allow_callback_replaces_the_frozen_allowlist(monkeypatch):
    """Registry-driven allowlists reload; the frozen set can be empty when `allow` decides."""
    import kb_mcp.auth as A
    inner, seen = _echo_state_app()
    cfg = CfAccessConfig(team_domain="t.cloudflareaccess.com", audience="a" * 64, emails=frozenset())
    verifier_cfg = {}

    class _V:
        def __init__(self, c, key_for_token=None, allow=None):
            verifier_cfg["allow"] = allow

        def verify(self, token):
            if verifier_cfg["allow"] is not None and not verifier_cfg["allow"]("bob@example.com"):
                raise A.AuthRejected(401, "email not allowlisted")
            return "bob@example.com"
    monkeypatch.setattr(A, "CfAccessVerifier", _V)
    app = wrap_with_auth(inner, cfg, allow=lambda e: e == "bob@example.com")
    assert _call_principal(app, [(b"cf-access-jwt-assertion", b"x")]) == 200 and seen["principal"] == "bob@example.com"
    app = wrap_with_auth(inner, cfg, allow=lambda e: False)
    assert _call_principal(app, [(b"cf-access-jwt-assertion", b"x")]) == 401


def test_real_verifier_allow_and_internal_secret_principal():
    """The fakes above never run CfAccessVerifier.verify: pin that `allow` really
    replaces cfg.emails there, and that internal-secret mode stores None."""
    empty = CfAccessConfig(team_domain="team.cloudflareaccess.com", audience=_AUD,
                           emails=frozenset())
    seen_emails = []

    def allow(email):
        seen_emails.append(email)
        return email == "avery@example.com"
    v = CfAccessVerifier(empty, key_for_token=lambda tok: _PUB, allow=allow)
    assert v.verify(_token()) == "avery@example.com"
    _rejects(v, _token(email="mallory@example.com"), 401)
    assert seen_emails == ["avery@example.com", "mallory@example.com"]
    _rejects(CfAccessVerifier(empty, key_for_token=lambda tok: _PUB), _token(), 401)

    inner, seen = _echo_state_app()
    app = wrap_with_auth(inner, InternalSecretConfig(secret="f" * 64))
    seen["principal"] = "sentinel"
    assert _call_principal(app, [(b"x-kb-mcp-internal", b"f" * 64)]) == 200
    assert seen["principal"] is None


def test_auth_reuses_the_one_kb_core_verifier():
    """One Access verifier for the dashboard and the public MCP listener: kb_mcp.auth
    re-exports kb_core.cf_access's classes instead of carrying a second copy."""
    import kb_core.cf_access as C
    import kb_mcp.auth as A
    assert A.AuthRejected is C.AuthRejected
    assert A.CfAccessConfig is C.CfAccessConfig
    assert A.CfAccessVerifier is C.CfAccessVerifier


# ---- tunnel.sh: may only ever forward to the authenticated listener ----
import shutil
import subprocess
import sys
from pathlib import Path

_TUNNEL = Path(__file__).resolve().parent.parent / "tunnel.sh"
_GOOD_CORE_ENV = ("KB_MCP_PUBLIC_AUTH=internal-secret\n"
                  "KB_MCP_PUBLIC_HOSTNAME=kb-mcp.example.com\n")


def _run_tunnel(tmp_path, tunnel_env: str, core_env: str = _GOOD_CORE_ENV,
                curl_status: str = "000"):
    """Run a copy of tunnel.sh in a scratch checkout with cloudflared/brew/curl
    stubbed; every stub call is logged so a refusal can be shown to happen before
    anything touches Cloudflare."""
    mcp = tmp_path / "KB_MCP"
    core = tmp_path / "KB_Core"
    stubs = tmp_path / "bin"
    for d in (mcp, core, stubs):
        d.mkdir()
    shutil.copy(_TUNNEL, mcp / "tunnel.sh")
    (mcp / ".env.tunnel").write_text(tunnel_env, encoding="utf-8")
    (core / ".env").write_text(core_env, encoding="utf-8")
    log = tmp_path / "calls.log"
    (stubs / "cloudflared").write_text(
        "#!/bin/bash\n"
        f'echo "cloudflared $*" >> "{log}"\n'
        'if [[ "$1 $2" == "tunnel list" ]]; then\n'
        '  echo \'[{"id":"tid-1","name":"my-kb-tunnel"}]\'\n'
        "fi\n", encoding="utf-8")
    (stubs / "brew").write_text(f'#!/bin/bash\necho "brew $*" >> "{log}"\n', encoding="utf-8")
    (stubs / "curl").write_text(f"#!/bin/bash\nprintf '%s' '{curl_status}'\n",
                                encoding="utf-8")
    for f in stubs.iterdir():
        f.chmod(0o755)
    (stubs / "python3").symlink_to(sys.executable)
    env = {"PATH": f"{stubs}:/usr/bin:/bin", "HOME": str(tmp_path)}
    res = subprocess.run(["/bin/bash", str(mcp / "tunnel.sh")], env=env, cwd=tmp_path,
                         capture_output=True, text=True, timeout=60)
    calls = log.read_text(encoding="utf-8") if log.exists() else ""
    return res, calls, mcp / ".cloudflare" / "tunnel.yml"


_TUNNEL_ENV = "CF_TUNNEL_NAME=my-kb-tunnel\nCF_HOSTNAME=kb-mcp.example.com\n"


def test_tunnel_defaults_to_the_authenticated_listener(tmp_path):
    res, calls, config = _run_tunnel(tmp_path, _TUNNEL_ENV)
    assert res.returncode == 0, res.stderr
    text = config.read_text(encoding="utf-8")
    assert "service: http://localhost:3100" in text
    assert "3000" not in text
    assert "cloudflared tunnel --config" in calls


@pytest.mark.parametrize("tunnel_env, core_env, needle", [
    # the plain, unauthenticated listener, by its default port ...
    (_TUNNEL_ENV + "MCP_PUBLIC_PORT=3000\n", _GOOD_CORE_ENV, "NO auth"),
    # ... or by whatever port KB_Core/.env moved it to
    (_TUNNEL_ENV + "MCP_PUBLIC_PORT=3200\n", _GOOD_CORE_ENV + "KB_MCP_PORT=3200\n",
     "NO auth"),
    # a legacy .env.tunnel still carrying the old MCP_PORT=3000 line
    (_TUNNEL_ENV + "MCP_PORT=3000\n", _GOOD_CORE_ENV, "MCP_PORT is no longer read"),
    # mcp-public is not configured with an auth mode / hostname
    (_TUNNEL_ENV, "KB_MCP_PUBLIC_HOSTNAME=kb-mcp.example.com\n", "KB_MCP_PUBLIC_AUTH"),
    (_TUNNEL_ENV, "KB_MCP_PUBLIC_AUTH=cf-access\n", "KB_MCP_PUBLIC_HOSTNAME"),
    (_TUNNEL_ENV, "KB_MCP_PUBLIC_AUTH=cf-access\nKB_MCP_PUBLIC_HOSTNAME=other.example.com\n",
     "421"),
])
def test_tunnel_refuses_before_touching_cloudflare(tmp_path, tunnel_env, core_env, needle):
    res, calls, config = _run_tunnel(tmp_path, tunnel_env, core_env)
    assert res.returncode != 0
    assert needle in res.stderr
    assert calls == ""          # neither cloudflared nor brew ran
    assert not config.exists()


def test_tunnel_refuses_a_port_that_serves_unauthenticated_requests(tmp_path):
    res, calls, config = _run_tunnel(tmp_path, _TUNNEL_ENV, curl_status="406")
    assert res.returncode != 0
    assert "expected 401" in res.stderr
    assert calls == "" and not config.exists()


def test_tunnel_accepts_a_listener_that_answers_401(tmp_path):
    res, _calls, config = _run_tunnel(tmp_path, _TUNNEL_ENV, curl_status="401")
    assert res.returncode == 0, res.stderr
    assert "service: http://localhost:3100" in config.read_text(encoding="utf-8")


def test_tunnel_env_example_targets_the_authenticated_listener():
    example = (_TUNNEL.parent / ".env.tunnel.example").read_text(encoding="utf-8")
    assert "MCP_PUBLIC_PORT=3100" in example
    assert not any(line.startswith("MCP_PORT=") for line in example.splitlines())


# --- the unauthenticated listener never binds off loopback --------------------------

@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.20", "::", "kb.lan"])
def test_main_refuses_a_non_loopback_bind_without_auth(monkeypatch, host):
    _fake, calls = _wire_main_fakes(monkeypatch)
    monkeypatch.setenv("KB_MCP_HOST", host)
    with pytest.raises(SystemExit, match="refusing to bind the unauthenticated"):
        server_mod.main()
    assert calls["open_pool"] == 0
    assert _fake.ran_transport is None


@pytest.mark.parametrize("host", [None, "127.0.0.1", "localhost", "::1", "127.0.0.2"])
def test_main_binds_loopback_without_auth(monkeypatch, host):
    fake, _calls = _wire_main_fakes(monkeypatch)
    seen = {}

    def capture(*a, **k):
        seen.update(k)
        return fake
    monkeypatch.setattr(server_mod, "build_server", capture)
    if host is None:
        monkeypatch.delenv("KB_MCP_HOST", raising=False)
    else:
        monkeypatch.setenv("KB_MCP_HOST", host)
    server_mod.main()
    assert seen["host"] == (host or "127.0.0.1")
    assert fake.ran_transport == "streamable-http"


def test_non_loopback_bind_is_allowed_with_auth_or_on_stdio():
    from kb_mcp.auth import InternalSecretConfig, bind_host_from_env
    cfg = InternalSecretConfig(secret="f" * 64)
    assert bind_host_from_env({"KB_MCP_HOST": "0.0.0.0"}, cfg, "streamable-http") == "0.0.0.0"
    assert bind_host_from_env({"KB_MCP_HOST": "0.0.0.0"}, None, "stdio") == "0.0.0.0"


def test_internal_secret_is_refused_when_tenants_are_enabled():
    """No identity in internal-secret mode: with tenants it would serve the owner to all."""
    from kb_mcp.auth import InternalSecretConfig, refuse_internal_secret_with_tenants

    class _Reg:
        path = "tenants.toml"

        def __init__(self, tenants):
            self._tenants = tenants

        def tenants(self, include_owner=True):
            assert include_owner is False
            return self._tenants

    secret = InternalSecretConfig(secret="s" * 32)
    with pytest.raises(SystemExit, match="enabled tenants"):
        refuse_internal_secret_with_tenants(secret, _Reg(["b"]))
    refuse_internal_secret_with_tenants(secret, _Reg([]))           # no tenants: allowed
    refuse_internal_secret_with_tenants(_CFG, _Reg(["b"]))          # cf-access: allowed
    refuse_internal_secret_with_tenants(None, _Reg(["b"]))          # local listener: allowed
