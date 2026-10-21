"""The dashboard API rejects requests whose Host header isn't allow-listed.

This is the app-layer guard (defence-in-depth) against an unauthenticated tunnel ingress —
e.g. a hostname like evil.example.com pointed straight at this backend, bypassing the
access gate on the public front-door (e.g. kb.example.com). The legitimate dashboard path
reaches this API as 127.0.0.1:8000 (the vite preview proxy sets changeOrigin), which stays
allowed.
"""
from fastapi.testclient import TestClient

from dashboard.api.app import create_app


def test_rejects_untrusted_host(kb):
    client = TestClient(create_app(kb))
    # Legit path: same-origin proxy delivers Host 127.0.0.1:8000 -> allowed.
    assert client.get("/api/tree", headers={"host": "127.0.0.1:8000"}).status_code == 200
    # A public hostname bypassing Access is refused at the middleware, before routing.
    assert client.get(
        "/api/tree", headers={"host": "evil.example.com"}
    ).status_code == 400


def test_allowed_hosts_env_override(kb, monkeypatch):
    monkeypatch.setenv("KB_API_ALLOWED_HOSTS", "example.test")
    client = TestClient(create_app(kb))
    assert client.get("/api/tree", headers={"host": "example.test"}).status_code == 200
    # Anything outside the override (even the built-in default) is now rejected.
    assert client.get("/api/tree", headers={"host": "127.0.0.1:8000"}).status_code == 400
