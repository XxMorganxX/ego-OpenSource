"""The inference HTTP client's timeout is env-configurable (KB_INFERENCE_TIMEOUT)."""

import json
from kb_core.ingest.inference import EmbeddingClient, HTTPInferenceClient, InferenceClient


def test_timeout_defaults_to_300_without_env(monkeypatch):
    # 300s, not 120s: a single-flight model can queue a call behind another generation (and a
    # cold reload), so the gate has to outlast the call ahead of it. See KB_INFERENCE_TIMEOUT.
    monkeypatch.delenv("KB_INFERENCE_TIMEOUT", raising=False)
    assert HTTPInferenceClient("http://x")._timeout == 300.0


def test_timeout_reads_kb_inference_timeout_env(monkeypatch):
    monkeypatch.setenv("KB_INFERENCE_TIMEOUT", "300")
    assert HTTPInferenceClient("http://x")._timeout == 300.0


def test_explicit_timeout_overrides_env(monkeypatch):
    monkeypatch.setenv("KB_INFERENCE_TIMEOUT", "300")
    assert HTTPInferenceClient("http://x", timeout=45.0)._timeout == 45.0


def test_embed_posts_input_and_returns_vectors(monkeypatch):
    captured = {}
    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps(
            {"model": "qwen3-embedding:0.6b", "dim": 3, "count": 2,
             "embeddings": [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]}).encode()
    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        return _Resp()
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    out = HTTPInferenceClient("http://x:8000").embed(["a", "b"])

    assert captured["url"].endswith("/embed")
    assert captured["body"] == {"input": ["a", "b"]}
    assert out == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]


def test_http_inference_client_satisfies_inference_client_protocol():
    assert isinstance(HTTPInferenceClient("http://x:8000"), InferenceClient)


def test_http_inference_client_satisfies_embedding_client_protocol():
    assert isinstance(HTTPInferenceClient("http://x:8000"), EmbeddingClient)


def test_model_resolved_lazily_from_health_and_cached(monkeypatch):
    calls = []
    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps({"ollama": "up", "model": "gemma4:26b"}).encode()
    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        return _Resp()
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    c = HTTPInferenceClient("http://x:8000")
    assert c.provider == "local"
    assert c.model == "gemma4:26b"
    assert c.model == "gemma4:26b"
    assert calls == ["http://x:8000/health"]      # one probe, then cached


def test_model_probe_failure_degrades_to_question_mark_and_is_retried(monkeypatch):
    # /health down (or the key absent — the handler omits `model` when Ollama is down):
    # model reads "?" but is NOT cached, so a later read can still resolve it.
    state = {"up": False}
    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps({"ollama": "up", "model": "gemma4:26b"}).encode()
    def fake_urlopen(req, timeout=None):
        if not state["up"]:
            raise OSError("connection refused")
        return _Resp()
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    c = HTTPInferenceClient("http://x:8000")
    assert c.model == "?"
    state["up"] = True
    assert c.model == "gemma4:26b"


def test_embed_stashes_the_served_model(monkeypatch):
    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps(
            {"model": "qwen3-embedding:0.6b", "dim": 3, "count": 1,
             "embeddings": [[0.1, 0.2, 0.3]]}).encode()
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: _Resp())
    c = HTTPInferenceClient("http://x:8000")
    assert c.embed_model is None
    c.embed(["a"])
    assert c.embed_model == "qwen3-embedding:0.6b"


def test_client_identity_formats_and_tolerates_plain_objects():
    from kb_core.ingest.inference import client_identity
    class _Bare:
        pass
    class _Ided:
        provider = "claude-cli"
        model = "claude-sonnet-5"
    class _NoModel:
        provider = "local"
    assert client_identity(_Bare()) is None
    assert client_identity(_Ided()) == "claude-cli:claude-sonnet-5"
    assert client_identity(_NoModel()) == "local:?"


# --- KB_INFERENCE_TOKEN: bearer auth to a KB_Inference started with a token ---------

def _capture_all(monkeypatch, body=None):
    seen = []
    payload = json.dumps(body or {"reply": "ok", "model": "m", "embeddings": [[0.0]]}).encode()

    class _Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return payload
        def close(self): pass

    def fake_urlopen(req, timeout=None):
        seen.append((req.full_url, req.get_header("Authorization")))
        return _Resp()
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return seen


def test_no_token_sends_no_authorization_header(monkeypatch):
    monkeypatch.delenv("KB_INFERENCE_TOKEN", raising=False)
    seen = _capture_all(monkeypatch)
    c = HTTPInferenceClient("http://x:8001")
    c.complete("sys", "user")
    c.embed(["a"])
    c.health()
    assert c.model == "m"
    assert seen and all(auth is None for _, auth in seen)


def test_token_from_env_is_sent_on_every_call(monkeypatch):
    monkeypatch.setenv("KB_INFERENCE_TOKEN", "test-token-123")
    seen = _capture_all(monkeypatch)
    c = HTTPInferenceClient("http://x:8001")
    c.complete("sys", "user")        # /chat, then the /sessions purge
    c.embed(["a"])
    c.health()
    assert c.model == "m"
    paths = [u.split("8001", 1)[1].split("?", 1)[0] for u, _ in seen]
    assert {"/chat", "/embed", "/health"} <= set(paths)
    assert any(p.startswith("/sessions/") for p in paths)
    assert all(auth == "Bearer test-token-123" for _, auth in seen)


def test_build_inference_client_passes_the_env_mapping_token(monkeypatch):
    monkeypatch.delenv("KB_INFERENCE_TOKEN", raising=False)
    from kb_core.ingest.inference import build_inference_client
    seen = _capture_all(monkeypatch)
    build_inference_client({"KB_INFERENCE_TOKEN": "mapped-token"}).embed(["a"])
    assert seen[-1][1] == "Bearer mapped-token"


def test_empty_token_means_unset(monkeypatch):
    # A launcher forwarding ${KB_INFERENCE_TOKEN:-} sets it to ''; that is "no token".
    monkeypatch.setenv("KB_INFERENCE_TOKEN", "")
    from kb_core.ingest.inference import inference_auth_headers
    assert inference_auth_headers() == {}
    assert inference_auth_headers({"KB_INFERENCE_TOKEN": "  "}) == {}
    assert inference_auth_headers({"KB_INFERENCE_TOKEN": "t"}) == {"Authorization": "Bearer t"}


def test_healthcheck_inference_probe_sends_the_token(monkeypatch):
    monkeypatch.setenv("KB_INFERENCE_TOKEN", "probe-token")
    monkeypatch.setenv("KB_INFERENCE_URL", "http://x:8001")
    from kb_core import healthcheck
    seen = _capture_all(monkeypatch)
    problems, info = [], {}
    healthcheck._inference_signal(problems, info)
    assert info["inference"] == "ok" and problems == []
    assert seen == [("http://x:8001/health", "Bearer probe-token")]


# --- an unreachable server is an outage of "local", not a failure of the document ------

def _dead_url() -> str:
    """A loopback port with nothing listening (bound, then released)."""
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


def test_unreachable_server_raises_provider_unavailable_for_complete_and_embed():
    from datetime import datetime, timedelta, timezone

    import pytest

    from kb_core.ingest.inference import LOCAL_UNREACHABLE_PAUSE, ProviderUnavailable
    client = HTTPInferenceClient(_dead_url(), timeout=5)
    for call in (lambda: client.complete("s", "u"), lambda: client.embed(["x"])):
        with pytest.raises(ProviderUnavailable) as info:
            call()
        assert info.value.provider == "local"
        left = info.value.until - datetime.now(timezone.utc)
        assert timedelta(0) < left <= LOCAL_UNREACHABLE_PAUSE
        assert "unreachable" in str(info.value)


def test_failures_after_the_request_was_sent_stay_plain_inference_errors(monkeypatch):
    """A timeout, a reset or an HTTP error can be about the document itself (a single-flight
    server timing out on a huge note): those must still fail the node, not defer it forever."""
    import io
    import urllib.error
    import urllib.request

    import pytest

    from kb_core.ingest.inference import InferenceError, ProviderUnavailable
    errors = [TimeoutError("timed out"), ConnectionResetError(54, "reset"),
              urllib.error.URLError(TimeoutError("timed out")),
              urllib.error.HTTPError("http://x/chat", 500, "boom", {}, io.BytesIO(b""))]
    for err in errors:
        def urlopen(*a, _e=err, **k):
            raise _e
        monkeypatch.setattr(urllib.request, "urlopen", urlopen)
        with pytest.raises(InferenceError) as info:
            HTTPInferenceClient("http://x").complete("s", "u")
        assert not isinstance(info.value, ProviderUnavailable), err


# --- KB_Inference up, Ollama down: 503 + Retry-After -> ProviderUnavailable -------------

def _http_503(retry_after):
    import io
    import urllib.error
    from email.message import Message
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError("http://x/chat", 503, "Service Unavailable", headers,
                                  io.BytesIO(b'{"detail": "chat unavailable"}'))


def _assert_paused_for(exc_info, expected, *, slack=5):
    from datetime import datetime, timezone

    from kb_core.ingest.inference import ProviderUnavailable
    assert isinstance(exc_info.value, ProviderUnavailable)
    assert exc_info.value.provider == "local"
    left = (exc_info.value.until - datetime.now(timezone.utc)).total_seconds()
    assert expected - slack <= left <= expected, left


def test_503_from_the_server_is_a_local_outage_for_complete_and_embed(monkeypatch):
    """A running KB_Inference whose Ollama is stopped answers 503: the note must be
    deferred (provider paused), never marked failed."""
    import urllib.request

    import pytest

    from kb_core.ingest.inference import ProviderUnavailable

    def urlopen(req, *a, **k):
        if getattr(req, "get_method", lambda: "GET")() == "DELETE":
            raise OSError("purge is best-effort")
        raise _http_503("45")
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    client = HTTPInferenceClient("http://x")
    for call in (lambda: client.complete("s", "u"), lambda: client.embed(["x"])):
        with pytest.raises(ProviderUnavailable) as info:
            call()
        _assert_paused_for(info, 45)
        assert "model backend unavailable" in str(info.value)


def test_retry_after_is_honoured_and_clamped(monkeypatch):
    import urllib.request
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    import pytest

    from kb_core.ingest.inference import (LOCAL_UNREACHABLE_PAUSE, RETRY_AFTER_MAX,
                                          RETRY_AFTER_MIN, ProviderUnavailable)
    in_two_minutes = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=120),
                                     usegmt=True)
    cases = [
        ("30", 30),
        (in_two_minutes, 120),                                   # HTTP-date form
        ("0", RETRY_AFTER_MIN.total_seconds()),                  # floor
        ("999999", RETRY_AFTER_MAX.total_seconds()),             # ceiling
        (None, LOCAL_UNREACHABLE_PAUSE.total_seconds()),         # absent -> default
        ("soon", LOCAL_UNREACHABLE_PAUSE.total_seconds()),       # garbage -> default
    ]
    for header, expected in cases:
        def urlopen(*a, _h=header, **k):
            raise _http_503(_h)
        monkeypatch.setattr(urllib.request, "urlopen", urlopen)
        with pytest.raises(ProviderUnavailable) as info:
            HTTPInferenceClient("http://x").embed(["x"])
        _assert_paused_for(info, expected)


def test_503_over_a_real_socket_carries_the_retry_after():
    """End to end through urllib (no monkeypatch): the HTTPError's real headers object."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    import pytest

    from kb_core.ingest.inference import ProviderUnavailable

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code, body=b"{}", extra=()):
            self.send_response(code)
            for k, v in extra:
                self.send_header(k, v)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self._reply(503, b'{"detail": "unavailable"}', [("Retry-After", "40")])

        def do_DELETE(self):
            self._reply(200)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        client = HTTPInferenceClient(f"http://127.0.0.1:{srv.server_address[1]}", timeout=5)
        for call in (lambda: client.complete("s", "u"), lambda: client.embed(["x"])):
            with pytest.raises(ProviderUnavailable) as info:
                call()
            _assert_paused_for(info, 40)
    finally:
        srv.shutdown()
        srv.server_close()
