"""Dashboard: the library download URLs (ETag/304, approved-only), the client
bootstrap file, the agent routes, and service-token principals limited to downloads."""
import io
import tarfile
import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from dashboard.api.app import _dashboard_verifier, create_app
from kb_core import library_client
from kb_core.tenancy import TenantPools
from kb_core.tenants import Registry, Tenant
from tests.conftest import TEST_DATABASE_URL


def _md(name, body="v1\n"):
    return f"---\nname: {name}\ndescription: Example for tests.\n---\n\n{body}"


def _approve(kb, name, rev=1, kind="skill"):
    get = kb.get_skill if kind == "skill" else kb.get_agent
    review = kb.review_skill if kind == "skill" else kb.review_agent
    review(name, rev, get(name, rev)["package_hash"], True, "read")


@pytest.fixture
def client(kb):
    return TestClient(create_app(kb))


def _names(body):
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tf:
        return [m.name for m in tf.getmembers()]


def test_library_download_serves_approved_items_with_an_etag_and_304(kb, client):
    kb.upsert_skill("deploy-checklist", None, {"SKILL.md": _md("deploy-checklist")}, "c")
    _approve(kb, "deploy-checklist")
    kb.upsert_skill("draft-only", None, {"SKILL.md": _md("draft-only")}, "c")
    r = client.get("/api/library/skills", headers={"Accept-Encoding": "identity"})
    assert r.status_code == 200 and r.headers["content-type"] == "application/gzip"
    assert _names(r.content) == ["manifest.json", "skills/deploy-checklist/SKILL.md"]
    etag = r.headers["etag"]
    assert etag == f'"{kb.library_etag("skill")}"'
    again = client.get("/api/library/skills", headers={"If-None-Match": etag})
    assert again.status_code == 304 and again.content == b"" and again.headers["etag"] == etag
    kb.upsert_skill("deploy-checklist", None, {"SKILL.md": _md("deploy-checklist", "v2\n")}, "e")
    assert client.get("/api/library/skills", headers={"If-None-Match": etag}).status_code == 304
    _approve(kb, "deploy-checklist", 2)
    assert client.get("/api/library/skills", headers={"If-None-Match": etag}).status_code == 200


def test_agents_download_and_routes(kb, client):
    kb.upsert_agent("code-auditor", None, _md("code-auditor", "You audit.\n"), "c")
    assert client.get("/api/agents").json()["agents"][0]["review_status"] == "pending"
    d = client.get("/api/agents/code-auditor").json()
    assert d["kind"] == "agent" and d["agent_md"].startswith("---\nname: code-auditor")
    assert client.get("/api/agents/nobody").status_code == 404
    assert _names(client.get("/api/library/agents").content) == ["manifest.json"]
    _approve(kb, "code-auditor", kind="agent")
    assert _names(client.get("/api/library/agents").content) == ["manifest.json", "agents/code-auditor.md"]


def test_skills_page_lists_pending_items_and_shows_the_newest_revision(kb, client):
    kb.upsert_skill("draft-only", None, {"SKILL.md": _md("draft-only")}, "c")
    rows = client.get("/api/skills").json()["skills"]
    assert [(r["name"], r["review_status"], r["approved_revision"]) for r in rows] == [("draft-only", "pending", None)]
    assert client.get("/api/skills/draft-only").json()["revision"] == 1


def test_client_bootstrap_is_the_exact_client_source(client):
    r = client.get("/api/library/client.py")
    assert r.status_code == 200 and r.content == Path(library_client.__file__).read_bytes()


KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _svc(cid):
    now = int(time.time())
    return {"Cf-Access-Jwt-Assertion": jwt.encode(
        {"common_name": cid, "sub": "", "aud": "aud-d", "iat": now, "exp": now + 600,
         "iss": "https://t.cloudflareaccess.com"}, KEY, algorithm="RS256")}


@pytest.fixture
def authed(kb):
    """Pools mode with the REAL dashboard verifier (_dashboard_verifier, so the
    allow_service wiring is under test too) and an owner that lists device dev-a.access.
    The owner's pool is database A, the one the `kb` fixture writes to."""
    db_a = TEST_DATABASE_URL.rsplit("/", 1)[1]
    owner = Tenant("owner", frozenset({"m@example.com"}), database=db_a,
                   library_tokens=frozenset({"dev-a.access"}))
    reg = Registry(owner, {}, env={"KB_DATABASE_URL": TEST_DATABASE_URL})
    pools = TenantPools(reg, max_size=2, env={"KB_DATABASE_URL": TEST_DATABASE_URL})
    verifier = _dashboard_verifier(
        {"KB_DASHBOARD_AUTH": "cf-access", "KB_CF_ACCESS_TEAM_DOMAIN": "t.cloudflareaccess.com",
         "KB_CF_ACCESS_AUD_DASHBOARD": "aud-d"}, key_for_token=lambda tok: KEY.public_key())
    yield TestClient(create_app(pools, registry=reg, verifier=verifier, db_viewer_password="pw"))
    pools.close()


def test_a_device_token_downloads_its_tenants_library_and_nothing_else(kb, authed):
    kb.upsert_skill("deploy-checklist", None, {"SKILL.md": _md("deploy-checklist")}, "c")
    _approve(kb, "deploy-checklist")
    ok = authed.get("/api/library/skills", headers=_svc("dev-a.access"))
    assert ok.status_code == 200 and "skills/deploy-checklist/SKILL.md" in _names(ok.content)
    assert authed.get("/api/library/client.py", headers=_svc("dev-a.access")).status_code == 200
    for path in ("/api/tree", "/api/skills", "/api/agents/x", "/api/skills/deploy-checklist"):
        assert authed.get(path, headers=_svc("dev-a.access")).status_code == 403, path
    assert authed.post("/api/library/skills", headers=_svc("dev-a.access")).status_code == 403
    assert authed.get("/api/library/skills", headers=_svc("dev-z.access")).status_code == 403


def _person(email):
    now = int(time.time())
    return {"Cf-Access-Jwt-Assertion": jwt.encode(
        {"email": email, "aud": "aud-d", "iat": now, "exp": now + 600,
         "iss": "https://t.cloudflareaccess.com"}, KEY, algorithm="RS256")}


def test_device_tokens_cannot_open_the_websocket_and_people_are_unaffected(authed):
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect) as exc:
        with authed.websocket_connect("/api/ws", headers=_svc("dev-a.access")) as ws:
            ws.receive_text()
    assert exc.value.code == 1008
    assert authed.head("/api/library/skills", headers=_svc("dev-a.access")).status_code == 403
    # the owner's person still reaches everything, the library included
    assert authed.get("/api/tree", headers=_person("m@example.com")).status_code == 200
    assert authed.get("/api/library/skills", headers=_person("m@example.com")).status_code == 200
    # a person whose email equals a device's Client ID is not that device
    assert authed.get("/api/library/skills", headers=_person("dev-a.access")).status_code == 403
