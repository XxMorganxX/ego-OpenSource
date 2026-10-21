"""Concurrency tests for the inference server's single-flight semaphore (server._gen_sem).

These don't touch Ollama: manager.achat is replaced with a fake that records how many
'generations' run at once, so we can assert the semaphore caps real overlap.
"""

import asyncio

import pytest
from fastapi import HTTPException

import server
from server import ChatRequest, EmbedRequest


def _tracking_achat(state):
    """A fake achat that holds the 'model' briefly and tracks peak concurrency."""
    async def achat(session, message, *, format=None):
        state["cur"] += 1
        state["max"] = max(state["max"], state["cur"])
        await asyncio.sleep(0.02)              # keep the slot busy so overlap is observable
        state["cur"] -= 1
        return f"reply:{session}"
    return achat


def test_chat_is_single_flight_by_default(monkeypatch):
    state = {"cur": 0, "max": 0}
    monkeypatch.setattr(server.manager, "achat", _tracking_achat(state))

    async def run():
        # A fresh semaphore bound to THIS event loop (default ceiling = 1).
        monkeypatch.setattr(server, "_gen_sem", asyncio.Semaphore(1))
        await asyncio.gather(*(
            server.chat(ChatRequest(message="hi", session=f"s{i}")) for i in range(5)))

    asyncio.run(run())
    assert state["max"] == 1                   # never two generations at once


def test_semaphore_caps_at_configured_concurrency(monkeypatch):
    state = {"cur": 0, "max": 0}
    monkeypatch.setattr(server.manager, "achat", _tracking_achat(state))

    async def run():
        monkeypatch.setattr(server, "_gen_sem", asyncio.Semaphore(2))
        await asyncio.gather(*(
            server.chat(ChatRequest(message="hi", session=f"s{i}")) for i in range(6)))

    asyncio.run(run())
    assert state["max"] == 2                   # exactly the configured ceiling, not more


# --- /embed -----------------------------------------------------------------

def _tracking_aembed(state, dim=4):
    """A fake aembed that holds the 'model' briefly and tracks peak concurrency."""
    async def aembed(texts):
        state["cur"] += 1
        state["max"] = max(state["max"], state["cur"])
        await asyncio.sleep(0.02)
        state["cur"] -= 1
        return [[0.0] * dim for _ in texts]
    return aembed


def test_embed_accepts_string_and_reports_shape(monkeypatch):
    monkeypatch.setattr(server.embedder, "aembed", _tracking_aembed({"cur": 0, "max": 0}, dim=4))
    monkeypatch.setattr(server.embedder, "model", "fake-embed")

    resp = asyncio.run(server.embed(EmbedRequest(input="hello")))
    assert resp.count == 1
    assert resp.dim == 4
    assert resp.model == "fake-embed"
    assert len(resp.embeddings) == 1 and len(resp.embeddings[0]) == 4


def test_embed_accepts_list(monkeypatch):
    monkeypatch.setattr(server.embedder, "aembed", _tracking_aembed({"cur": 0, "max": 0}, dim=3))
    resp = asyncio.run(server.embed(EmbedRequest(input=["a", "b", "c"])))
    assert resp.count == 3
    assert resp.dim == 3


def test_embed_rejects_empty_input():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(server.embed(EmbedRequest(input=[])))
    assert exc.value.status_code == 422

    with pytest.raises(HTTPException) as exc:
        asyncio.run(server.embed(EmbedRequest(input="   ")))
    assert exc.value.status_code == 422


def test_embed_has_its_own_concurrency_slot(monkeypatch):
    """Embeds run concurrently up to _embed_sem and aren't serialized by the chat semaphore."""
    state = {"cur": 0, "max": 0}
    monkeypatch.setattr(server.embedder, "aembed", _tracking_aembed(state))

    async def run():
        monkeypatch.setattr(server, "_embed_sem", asyncio.Semaphore(4))
        await asyncio.gather(*(
            server.embed(EmbedRequest(input=f"text{i}")) for i in range(8)))

    asyncio.run(run())
    assert state["max"] == 4                    # capped at the embed ceiling, not chat's 1


# --- network exposure (ExposureGuard, bind_host, debug sessions) ---------------
# HTTP-level tests through FastAPI's TestClient: in-process ASGI, no socket, no Ollama.
# manager.health / manager.achat are faked so no request reaches a model.

from fastapi.testclient import TestClient  # noqa: E402

LOCAL = "http://127.0.0.1:8001"


@pytest.fixture
def fake_backend(monkeypatch):
    monkeypatch.setattr(server.manager, "health",
                        lambda: {"ok": True, "model": "fake", "models": []})
    monkeypatch.setattr(server.manager, "achat", _tracking_achat({"cur": 0, "max": 0}))


def _client(monkeypatch, env=None, *, base_url=LOCAL, client_ip="127.0.0.1"):
    monkeypatch.setattr(server.app.state, "exposure", server.load_exposure(env or {}))
    return TestClient(server.app, base_url=base_url, client=(client_ip, 50000))


def test_defaults_are_loopback_only_and_tokenless():
    exp = server.load_exposure({})
    assert exp.token is None
    assert exp.allow_remote is False
    assert exp.debug_sessions is False
    assert exp.allowed_hosts == {"localhost", "127.0.0.1", "::1"}


def test_loopback_request_passes_without_token(monkeypatch, fake_backend):
    for base in (LOCAL, "http://localhost:8001", "http://[::1]:8001"):
        assert _client(monkeypatch, base_url=base).get("/health").status_code == 200


def test_foreign_host_header_is_refused(monkeypatch, fake_backend):
    """DNS rebinding: a loopback connection carrying an attacker's name as Host."""
    c = _client(monkeypatch)
    r = c.get("/health", headers={"host": "rebind.example.com:8001"})
    assert r.status_code == 400
    assert c.get("/docs", headers={"host": "rebind.example.com"}).status_code == 400


def test_allowed_hosts_extends_the_loopback_set(monkeypatch, fake_backend):
    c = _client(monkeypatch, {"KB_INFERENCE_ALLOWED_HOSTS": "inference.example.internal"})
    assert c.get("/health", headers={"host": "inference.example.internal:8001"}).status_code == 200
    assert c.get("/health").status_code == 200        # loopback names still accepted
    assert c.get("/health", headers={"host": "other.example.com"}).status_code == 400


def test_wildcard_allowed_host_is_rejected():
    with pytest.raises(RuntimeError):
        server.load_exposure({"KB_INFERENCE_ALLOWED_HOSTS": "*"})


def test_non_loopback_client_is_refused_by_default(monkeypatch, fake_backend):
    # e.g. `uvicorn server:app --host 0.0.0.0` bypasses bind_host; the guard still holds.
    r = _client(monkeypatch, client_ip="192.0.2.10").get("/health")
    assert r.status_code == 403


def test_allow_remote_requires_a_token():
    with pytest.raises(RuntimeError):
        server.load_exposure({"KB_INFERENCE_ALLOW_REMOTE": "1"})


def test_allow_remote_accepts_remote_client_with_token(monkeypatch, fake_backend):
    env = {"KB_INFERENCE_ALLOW_REMOTE": "1", "KB_INFERENCE_TOKEN": "s3cret-test-token"}
    c = _client(monkeypatch, env, client_ip="192.0.2.10")
    assert c.get("/health").status_code == 401
    ok = c.get("/health", headers={"authorization": "Bearer s3cret-test-token"})
    assert ok.status_code == 200


@pytest.mark.parametrize("path,method,body", [
    ("/health", "get", None),
    ("/chat", "post", {"message": "hi", "session": "t"}),
    ("/embed", "post", {"input": "x"}),
    ("/sessions/t", "delete", None),
    ("/sessions/t/system", "post", {"system_prompt": "p"}),
    ("/docs", "get", None),
    ("/openapi.json", "get", None),
    ("/no-such-route", "get", None),
])
def test_token_is_required_on_every_endpoint(monkeypatch, fake_backend, path, method, body):
    c = _client(monkeypatch, {"KB_INFERENCE_TOKEN": "s3cret-test-token"})
    kw = {"json": body} if body is not None else {}
    r = getattr(c, method)(path, **kw)
    assert r.status_code == 401
    assert r.headers.get("www-authenticate") == "Bearer"
    bad = getattr(c, method)(path, headers={"authorization": "Bearer wrong"}, **kw)
    assert bad.status_code == 401


def test_correct_token_passes(monkeypatch, fake_backend):
    c = _client(monkeypatch, {"KB_INFERENCE_TOKEN": "s3cret-test-token"})
    auth = {"authorization": "Bearer s3cret-test-token"}
    assert c.get("/health", headers=auth).status_code == 200
    r = c.post("/chat", json={"message": "hi", "session": "tok"}, headers=auth)
    assert r.status_code == 200 and r.json()["reply"] == "reply:tok"
    assert c.delete("/sessions/tok?purge=true", headers=auth).status_code == 200


def test_session_readback_is_off_by_default(monkeypatch, fake_backend):
    server.manager.set_system_prompt("dbg", "secret system prompt")
    try:
        c = _client(monkeypatch)
        assert c.get("/sessions").status_code == 404
        r = c.get("/sessions/dbg")
        assert r.status_code == 404
        assert "secret system prompt" not in r.text
        # purge (what KB_Core's client calls) still works with debug off
        assert c.delete("/sessions/dbg?purge=true").status_code == 200
    finally:
        server.manager.delete("dbg")


def test_session_readback_opt_in(monkeypatch, fake_backend):
    server.manager.set_system_prompt("dbg2", "visible prompt")
    try:
        c = _client(monkeypatch, {"KB_INFERENCE_DEBUG_SESSIONS": "1"})
        assert "dbg2" in c.get("/sessions").json()["sessions"]
        assert c.get("/sessions/dbg2").json()["system_prompt"] == "visible prompt"
    finally:
        server.manager.delete("dbg2")


def test_session_readback_still_needs_the_token(monkeypatch, fake_backend):
    c = _client(monkeypatch, {"KB_INFERENCE_DEBUG_SESSIONS": "1",
                              "KB_INFERENCE_TOKEN": "s3cret-test-token"})
    assert c.get("/sessions").status_code == 401


@pytest.mark.parametrize("env", [
    {}, {"HOST": "127.0.0.1"}, {"KB_INFERENCE_HOST": "localhost"}, {"HOST": "::1"},
])
def test_bind_host_defaults_to_loopback(env):
    assert server.is_loopback(server.bind_host(env, server.load_exposure({})))


@pytest.mark.parametrize("env", [
    {"HOST": "0.0.0.0"}, {"KB_INFERENCE_HOST": "0.0.0.0"}, {"HOST": "192.0.2.1"},
    {"HOST": "my-laptop.local"}, {"KB_INFERENCE_HOST": "::"},
])
def test_bind_host_refuses_non_loopback_without_opt_in(env):
    with pytest.raises(SystemExit):
        server.bind_host(env, server.load_exposure({}))


def test_bind_host_non_loopback_with_opt_in():
    exp = server.load_exposure({"KB_INFERENCE_ALLOW_REMOTE": "1", "KB_INFERENCE_TOKEN": "t"})
    assert server.bind_host({"KB_INFERENCE_HOST": "0.0.0.0"}, exp) == "0.0.0.0"


def test_kb_inference_host_wins_over_host():
    exp = server.load_exposure({})
    assert server.bind_host({"KB_INFERENCE_HOST": "127.0.0.1", "HOST": "0.0.0.0"}, exp) \
        == "127.0.0.1"


def test_interactive_docs_and_schema_are_not_served(monkeypatch, fake_backend):
    c = _client(monkeypatch)
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert c.get(path).status_code == 404, path


# --- upstream (Ollama) outages: 503 + Retry-After, not 500 --------------------
# KB_Core's HTTP client turns a 503 into ProviderUnavailable (pause the provider, re-queue
# the note); a 500 marks the note failed. So only a genuine backend outage may be a 503.

import httpx  # noqa: E402
import ollama  # noqa: E402


def _failing(exc):
    async def boom(*_a, **_kw):
        raise exc
    return boom


def _raw_client(monkeypatch):
    """Like _client, but a server-side exception becomes the 500 a real client sees."""
    monkeypatch.setattr(server.app.state, "exposure", server.load_exposure({}))
    return TestClient(server.app, base_url=LOCAL, client=("127.0.0.1", 50000),
                      raise_server_exceptions=False)


OLLAMA_DOWN = ConnectionError("Failed to connect to Ollama. Please check that Ollama is "
                              "downloaded, running and accessible.")


@pytest.mark.parametrize("exc", [
    OLLAMA_DOWN,                                         # ollama client: nothing listening
    ConnectionRefusedError(61, "Connection refused"),
    httpx.ConnectError("connect failed"),
    httpx.ConnectTimeout("connect timed out"),
    ollama.ResponseError("server busy, please try again", 503),
])
def test_chat_answers_503_with_retry_after_when_ollama_is_unavailable(monkeypatch, exc):
    monkeypatch.setattr(server.manager, "achat", _failing(exc))
    r = _raw_client(monkeypatch).post("/chat", json={"message": "hi", "session": "down"})
    assert r.status_code == 503
    assert r.headers["retry-after"] == str(server.UPSTREAM_RETRY_AFTER_S)
    assert "unavailable" in r.json()["detail"]


def test_embed_answers_503_with_retry_after_when_ollama_is_unavailable(monkeypatch):
    monkeypatch.setattr(server.embedder, "aembed", _failing(OLLAMA_DOWN))
    r = _raw_client(monkeypatch).post("/embed", json={"input": ["a"]})
    assert r.status_code == 503
    assert int(r.headers["retry-after"]) > 0
    assert "embed unavailable" in r.json()["detail"]


@pytest.mark.parametrize("exc", [
    ollama.ResponseError("model 'nope' not found", 404),   # misconfiguration, not an outage
    ollama.ResponseError("llama runner process has terminated", 500),
    ConnectionResetError(54, "Connection reset by peer"),  # mid-request: may be the input
    httpx.ReadTimeout("read timed out"),
    ValueError("bad format"),
])
def test_other_model_errors_stay_500(monkeypatch, exc):
    monkeypatch.setattr(server.manager, "achat", _failing(exc))
    monkeypatch.setattr(server.embedder, "aembed", _failing(exc))
    c = _raw_client(monkeypatch)
    assert c.post("/chat", json={"message": "hi", "session": "err"}).status_code == 500
    assert c.post("/embed", json={"input": "a"}).status_code == 500


def test_upstream_unavailable_follows_the_exception_chain():
    try:
        try:
            raise OLLAMA_DOWN
        except ConnectionError as inner:
            raise RuntimeError("wrapped by a library") from inner
    except RuntimeError as outer:
        assert server.upstream_unavailable(outer) is True
    assert server.upstream_unavailable(RuntimeError("plain")) is False
    assert server.upstream_unavailable(None) is False
