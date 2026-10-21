"""/healthz reports DB + inference readiness (200 when both up, 503 otherwise).

Part of the observability work: before this, the dashboard API had no health probe at
all — a broken backend looked identical to a healthy one to any external monitor.
"""
from fastapi.testclient import TestClient

from dashboard.api.app import create_app


def test_healthz_reports_db_ok(kb):
    client = TestClient(create_app(kb))
    r = client.get("/healthz")
    body = r.json()
    # The test DB is reachable via the kb fixture, so the DB check must pass.
    assert body["db"] == "ok"
    assert "inference" in body
    # Inference may or may not be running in the test env, so accept either overall verdict;
    # the point is the probe runs both checks and returns a structured breakdown.
    assert r.status_code in (200, 503)
    assert (r.status_code == 200) == (body["db"] == "ok" and body["inference"] == "ok")


def test_healthz_failures_are_generic_and_never_echo_the_exception(kb, monkeypatch):
    """Unauthenticated probe: a raw exception can carry a DSN, a host or a file path."""
    import urllib.request

    def db_down():
        raise RuntimeError("connection to postgresql://u:SECRET@db-host:5433/kb failed")

    def inference_down(*a, **kw):
        raise OSError("SECRET-inference-host refused")
    monkeypatch.setattr(kb, "healthcheck", db_down)
    monkeypatch.setattr(urllib.request, "urlopen", inference_down)
    r = TestClient(create_app(kb)).get("/healthz")
    assert r.status_code == 503
    assert r.json() == {"db": "error", "inference": "unreachable"}
    assert "SECRET" not in r.text


def test_dashboard_serves_no_api_docs_or_schema(kb):
    client = TestClient(create_app(kb))
    for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
        assert client.get(path).status_code == 404, path
