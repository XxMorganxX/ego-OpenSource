"""Cross-site write rejection on the dashboard API.

CORS governs whether a page may READ a response, not whether the request runs. A
"simple" POST (text/plain, no custom headers) skips preflight entirely, so before the
guard any page the user visited could drive mutating /api/* routes on localhost.
"""

import pytest
from fastapi.testclient import TestClient

from dashboard.api.app import create_app


@pytest.fixture
def client(kb):
    return TestClient(create_app(kb))


# A real, reachable mutating route; the guard must fire before the handler body runs,
# so the payload does not need to be valid.
_WRITE_PATH = "/api/merge-suggestions/1/approve"

EVIL = "https://evil.example"


def test_cross_site_post_is_rejected(client):
    resp = client.post(_WRITE_PATH, json={},
                       headers={"Sec-Fetch-Site": "cross-site"})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "cross-site write rejected"


def test_foreign_origin_post_is_rejected(client):
    """Belt and braces for clients predating Sec-Fetch-Site."""
    resp = client.post(_WRITE_PATH, json={},
                       headers={"Origin": EVIL})
    assert resp.status_code == 403


def test_cors_simple_post_is_rejected(client):
    """The actual attack shape: text/plain body, no custom headers -> no preflight."""
    resp = client.post(_WRITE_PATH, data="{}",
                       headers={"Content-Type": "text/plain", "Origin": EVIL,
                                "Sec-Fetch-Site": "cross-site"})
    assert resp.status_code == 403


def test_same_site_is_also_rejected(client):
    """A sibling subdomain is not the dashboard; only same-origin drives writes."""
    resp = client.post(_WRITE_PATH, json={},
                       headers={"Sec-Fetch-Site": "same-site"})
    assert resp.status_code == 403


@pytest.mark.parametrize("origin", ["http://127.0.0.1:8091", "http://localhost:8091",
                                    "http://[::1]:8091"])
def test_local_preview_write_is_allowed_through_the_guard(client, origin):
    """vite preview forwards the page's loopback Origin unrewritten: a local click on
    the dashboard served at :8091 must reach the handler."""
    resp = client.post(_WRITE_PATH, json={},
                       headers={"Origin": origin, "Sec-Fetch-Site": "same-origin"})
    assert resp.status_code != 403


@pytest.mark.parametrize("headers", [
    {"Origin": "http://127.0.0.1:3000", "Sec-Fetch-Site": "same-site"},   # other local app
    {"Origin": "http://localhost:3000", "Sec-Fetch-Site": "cross-site"},
    {"Origin": "http://127.0.0.1:8091"},                                 # no fetch metadata
    {"Origin": "http://127.0.0.1.evil.example", "Sec-Fetch-Site": "same-origin"},
])
def test_loopback_origin_needs_same_origin_fetch_metadata(client, headers):
    resp = client.post(_WRITE_PATH, json={}, headers=headers)
    assert resp.status_code == 403


def test_same_origin_write_is_allowed_through_the_guard(client):
    """The dashboard's own traffic must still reach the handler.

    Not a 403 is the assertion — the handler is free to 404/422 on a bogus id.
    """
    resp = client.post(_WRITE_PATH, json={},
                       headers={"Sec-Fetch-Site": "same-origin",
                                "Origin": "http://localhost:5173"})
    assert resp.status_code != 403


def test_tunnel_front_door_write_is_allowed_through_the_guard(kb, monkeypatch):
    """The dashboard served behind an access-gated tunnel is same-origin traffic too —
    its Origin is the public front-door, not localhost, and is admitted via the
    KB_DASHBOARD_ORIGINS env var. Before that, the guard 403'd every remote write
    (retry, curation, merges) while reads worked.

    _ALLOWED_ORIGINS is computed once at module import time, so proving the env var is
    honored requires reloading the module after setting it. That reload mutates shared
    module state, so it must be undone (env first, then a second reload) before this
    test returns, or every later test in the process would see the public origin as
    allowed.
    """
    monkeypatch.setenv("KB_DASHBOARD_ORIGINS", "https://kb.example.com")
    import importlib

    import dashboard.api.app as app_module
    importlib.reload(app_module)
    try:
        tunnel_client = TestClient(app_module.create_app(kb))
        resp = tunnel_client.post(_WRITE_PATH, json={},
                                  headers={"Sec-Fetch-Site": "same-origin",
                                           "Origin": "https://kb.example.com"})
        assert resp.status_code != 403
    finally:
        monkeypatch.undo()
        importlib.reload(app_module)


def test_non_browser_client_is_unaffected(client):
    """curl/httpx/CLI send neither header; the test suite depends on this."""
    resp = client.post(_WRITE_PATH, json={})
    assert resp.status_code != 403


def test_cross_site_read_is_still_allowed(client):
    """Only state-changing methods are gated; GETs are not CSRF-relevant here."""
    resp = client.get("/api/tree", headers={"Sec-Fetch-Site": "cross-site"})
    assert resp.status_code == 200


# --- /api/ws: the CSRF guard is HTTP-only, so the socket checks Origin itself ----------

def test_cross_origin_websocket_handshake_is_refused(client):
    """Any page the owner visited could subscribe to the change feed (every node's
    table/op/id) because the HTTP guard never sees a websocket scope."""
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect) as info:
        with client.websocket_connect("/api/ws", headers={"Origin": EVIL}) as ws:
            ws.receive_text()
    assert info.value.code == 1008


@pytest.mark.parametrize("origin", [
    None,                                   # non-browser client
    "http://127.0.0.1:8091",                # vite preview proxies the socket unrewritten
    "http://localhost:5173",                # vite dev server
    "http://[::1]:8091",
])
def test_dashboard_websocket_origins_are_accepted(client, origin):
    headers = {} if origin is None else {"Origin": origin}
    with client.websocket_connect("/api/ws", headers=headers):
        pass                                # handshake completed: accepted


@pytest.mark.parametrize("origin", [
    "http://127.0.0.1.evil.example", "https://127.0.0.1:8091", "http://localhost.evil:80",
    "null"])
def test_lookalike_origins_are_not_loopback(origin):
    from dashboard.api.app import _ws_origin_allowed
    assert _ws_origin_allowed(origin) is False
