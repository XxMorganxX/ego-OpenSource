"""Tenant daemons (hybrid tenancy): the ingest daemon threads its pool into the
inference router, gates the enrich lane on whichever remote providers the enrich passes
are routed to (so `agent` pauses exactly like `claude-cli`), and can run without its push
receiver."""
import os
from datetime import datetime, timedelta, timezone

import pytest

from kb_core.domain import NodeRecord
from kb_core.inference_routing import TASKS
from kb_core.ingest import daemon as D
from kb_core.ingest.inference import InferenceError
from kb_core.ingest.receiver import InsecureSecretError


def _tenant_routing(tmp_path, monkeypatch) -> str:
    p = tmp_path / "inference.toml"
    p.write_text('[provider.agent]\nconcurrency = 2\ntimeout = 600\n'
                 + "".join(f'[task.{n}]\nprovider = "{"local" if n == "embed" else "agent"}"\n'
                           for n in TASKS))
    monkeypatch.setenv("KB_INFERENCE_ROUTING", str(p))
    return str(p)


def test_enrich_gate_providers_follow_the_routing(tmp_path, monkeypatch, kb):
    _tenant_routing(tmp_path, monkeypatch)
    assert D.enrich_gate_providers(dict(os.environ), kb.policy) == ("agent",)


def test_enrich_gate_providers_fall_back_to_claude_cli_without_passes(tmp_path, monkeypatch, kb):
    _tenant_routing(tmp_path, monkeypatch)
    monkeypatch.setenv("KB_ENRICHMENT_CONFIG", str(tmp_path / "absent.toml"))
    assert D.enrich_gate_providers(dict(os.environ), kb.policy) == ("claude-cli",)


def test_enrichment_tick_skips_while_the_agent_provider_is_paused(kb, pool):
    n = kb.upsert(NodeRecord(source="agent", external_id="p1", type="note", content="c",
                             slug="p1", parent_path=""))
    kb.flag_stale_enrichment()
    lines: list[str] = []
    jobs = {j.name: j for j in D.build_jobs(kb, pool, [], enrichers=[], log=lines.append,
                                            gate_providers=("agent",))}
    kb.pause_provider("agent", until=datetime.now(timezone.utc) + timedelta(minutes=10),
                      reason="offline")
    assert jobs["enrichment"].run() == 0
    assert jobs["enrichment"].more(0) is False
    assert kb.get(n.id).enrichment_status == "pending"        # never claimed
    assert any("enrichment paused: agent" in ln for ln in lines)
    # pause_provider keeps the LATER `until` (GREATEST), so an earlier one cannot lift
    # the pause; the outage ends by the row lapsing, simulated here by deleting it.
    with pool.connection() as conn:
        conn.execute("DELETE FROM provider_pause")
    assert jobs["enrichment"].run() == 1


def test_a_claude_cli_pause_does_not_idle_an_agent_routed_lane(kb, pool):
    kb.upsert(NodeRecord(source="agent", external_id="p2", type="note", content="c",
                         slug="p2", parent_path=""))
    kb.flag_stale_enrichment()
    jobs = {j.name: j for j in D.build_jobs(kb, pool, [], enrichers=[], log=lambda _: None,
                                            gate_providers=("agent",))}
    kb.pause_provider("claude-cli", until=datetime.now(timezone.utc) + timedelta(minutes=10),
                      reason="session limit")
    assert jobs["enrichment"].more(0) is True
    assert jobs["enrichment"].run() == 1


def test_receiver_flag_parses_off_values():
    for off in ("0", "off", "false", "no", " OFF "):
        assert D.receiver_enabled({"KB_INGEST_RECEIVER": off}) is False
    assert D.receiver_enabled({"KB_INGEST_RECEIVER": "1"}) is True
    assert D.receiver_enabled({}) is True


def test_build_enrichers_threads_the_pool_into_the_router(tmp_path, monkeypatch, kb, pool):
    """With every pass routed to `agent`, building the enrichers needs the pool; without it
    the factory fails loudly, so a successful build proves the pool was threaded."""
    _tenant_routing(tmp_path, monkeypatch)
    monkeypatch.setenv("KB_EMBED_ENABLED", "0")
    enrichers = D._build_enrichers(dict(os.environ), phase="enrich", policy=kb.policy, pool=pool)
    assert any(type(e).__name__ == "LLMEnricher" for e in enrichers)
    with pytest.raises(InferenceError, match="pool"):
        D._build_enrichers(dict(os.environ), phase="enrich", policy=kb.policy)


def test_entity_resolve_daemon_skips_a_pass_while_the_agent_is_offline(monkeypatch, capsys):
    """An offline agent is not a failed pass: the daemon says so, skips, and sleeps its
    period. The router gets the daemon's pool so `agent` routes can be built."""
    from types import SimpleNamespace

    import kb_core.entity_resolve_daemon as daemon
    from kb_core.ingest.inference import ProviderUnavailable

    pool, routers = object(), []
    monkeypatch.setenv("KB_DATABASE_URL", "postgresql://unused")
    monkeypatch.setattr(daemon, "open_pool", lambda dsn: pool)
    monkeypatch.setattr(daemon, "migrate_pool", lambda p: None)
    monkeypatch.setattr(daemon, "KnowledgeBase", lambda p, r, **_kw: object())
    monkeypatch.setattr(daemon, "default_registry", lambda: object())

    def router(env, *, pool=None):
        routers.append(pool)
        return SimpleNamespace(describe=lambda: "test", client_for=lambda t: object(),
                               embedder=lambda: object())

    monkeypatch.setattr(daemon, "InferenceRouter", router)
    monkeypatch.setattr(daemon, "run_once", lambda *a, **k: (_ for _ in ()).throw(
        ProviderUnavailable("no agent answered", provider="agent", until=None)))

    def stop(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(daemon.time, "sleep", stop)
    daemon.main()
    assert routers == [pool]
    out = capsys.readouterr().out
    assert "agent unavailable" in out and "pass failed" not in out


def test_entity_resolve_daemon_runs_every_pass_and_reports_paused_roles(monkeypatch, capsys):
    """No whole-pass gate: run_once gates each role itself, so the local re-embed and
    candidate scan keep running through a remote provider's pause."""
    from types import SimpleNamespace

    import kb_core.entity_resolve_daemon as daemon

    calls = []
    monkeypatch.setenv("KB_DATABASE_URL", "postgresql://unused")
    monkeypatch.setattr(daemon, "open_pool", lambda dsn: object())
    monkeypatch.setattr(daemon, "migrate_pool", lambda p: None)
    monkeypatch.setattr(daemon, "KnowledgeBase", lambda p, r, **_kw: object())
    monkeypatch.setattr(daemon, "default_registry", lambda: object())
    monkeypatch.setattr(daemon, "InferenceRouter", lambda env, *, pool=None: SimpleNamespace(
        describe=lambda: "test", client_for=lambda t: SimpleNamespace(provider="agent"),
        embedder=lambda: SimpleNamespace(provider="local")))

    def run_once(*a, **k):
        calls.append(1)
        return {"described": 0, "reembedded": 3, "candidates": 2, "staged": 0,
                "blocked": 0, "paused": {"describe": "agent", "judge": "agent"}}

    monkeypatch.setattr(daemon, "run_once", run_once)

    def stop(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(daemon.time, "sleep", stop)
    daemon.main()
    assert calls == [1]
    out = capsys.readouterr().out
    assert "reembedded=3" in out and "paused: describe=agent judge=agent" in out


def _wire_ingest_main(monkeypatch, *, receiver):
    """Stub everything main() touches; returns the record of what it was handed."""
    from types import SimpleNamespace

    seen: dict = {"app": False}
    pool = object()
    kb = SimpleNamespace(policy="the-policy", requeue_stuck_processing=lambda: 0)
    monkeypatch.setenv("KB_DATABASE_URL", "postgresql://unused")
    monkeypatch.delenv("KB_INGEST_SECRET", raising=False)
    if receiver is None:
        monkeypatch.delenv("KB_INGEST_RECEIVER", raising=False)
    else:
        monkeypatch.setenv("KB_INGEST_RECEIVER", receiver)
    monkeypatch.setattr(D, "open_pool", lambda dsn, max_size: pool)
    monkeypatch.setattr(D, "migrate_pool", lambda p: None)
    monkeypatch.setattr(D, "default_registry", lambda: object())
    monkeypatch.setattr(D, "KnowledgeBase", lambda p, r, **_kw: kb)
    monkeypatch.setattr(D, "load_sources", lambda: [])

    def build_enrichers(env, *, phase, policy, pool=None):
        seen["enrichers_pool"] = pool
        return ["enricher"]

    def gate(env, policy):
        seen["gate_policy"] = policy
        return ("agent",)

    def build_jobs(kb_, pool_, sources, *, enrichers, kick_enrich, gate_providers):
        seen["jobs"] = {"enrichers": enrichers, "gate_providers": gate_providers}
        return []

    monkeypatch.setattr(D, "_build_enrichers", build_enrichers)
    monkeypatch.setattr(D, "enrich_gate_providers", gate)
    monkeypatch.setattr(D, "build_jobs", build_jobs)
    monkeypatch.setattr(D, "run_lanes", lambda jobs, **kw: {})

    class _Parked:
        def wait(self):
            seen["parked"] = True

    monkeypatch.setattr(D, "threading", SimpleNamespace(Event=_Parked))   # D's binding only

    def build_app(*a, **k):
        seen["app"] = True
        raise AssertionError("receiver must not be built")

    monkeypatch.setattr(D, "build_app", build_app)
    return seen, pool


def test_ingest_main_threads_the_pool_and_the_gate_and_skips_the_receiver(monkeypatch):
    seen, pool = _wire_ingest_main(monkeypatch, receiver="0")
    D.main()                       # no KB_INGEST_SECRET: reading it would KeyError
    assert seen["enrichers_pool"] is pool
    assert seen["gate_policy"] == "the-policy"
    assert seen["jobs"] == {"enrichers": ["enricher"], "gate_providers": ("agent",)}
    assert seen.get("parked") is True and seen["app"] is False


def test_ingest_main_still_builds_the_receiver_by_default(monkeypatch):
    seen, _pool = _wire_ingest_main(monkeypatch, receiver=None)
    with pytest.raises(InsecureSecretError, match="KB_INGEST_SECRET"):
        D.main()                   # the receiver is on, so the key is required
    assert "parked" not in seen


@pytest.mark.parametrize("value", ["dev-secret", "change-me-app", "short"])
def test_ingest_main_refuses_a_weak_secret_before_starting_anything(monkeypatch, value):
    seen, _pool = _wire_ingest_main(monkeypatch, receiver=None)
    monkeypatch.setenv("KB_INGEST_SECRET", value)
    with pytest.raises(InsecureSecretError):
        D.main()
    assert "jobs" not in seen and seen["app"] is False   # refused before the lanes start


def test_ingest_main_passes_a_strong_secret_to_the_receiver(monkeypatch):
    seen, _pool = _wire_ingest_main(monkeypatch, receiver=None)
    strong = "0123456789abcdef" * 4
    monkeypatch.setenv("KB_INGEST_SECRET", strong)
    got = {}

    def build_app(inbox, *, secret):
        got["secret"] = secret
        raise SystemExit(0)        # stop before uvicorn

    monkeypatch.setattr(D, "build_app", build_app)
    monkeypatch.setattr(D, "Inbox", lambda pool: object())
    with pytest.raises(SystemExit):
        D.main()
    assert got["secret"] == strong


def _dead_inference_url() -> str:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


def test_unreachable_inference_defers_notes_and_idles_the_lane_instead_of_failing_them(
        kb, pool):
    """First boot: every daemon starts at once and ingest beats KB_Inference. A refused
    connection used to mark each claimed note 'failed' for good (nothing retries them).
    Now it is an outage of `local`: the note goes back to 'pending', `local` is paused
    briefly, and the enrich lane holds — no claim churn into a dead server — until the
    pause lapses, after which the same notes enrich normally."""
    from kb_core.ingest.embedding import EmbeddingEnricher
    from kb_core.ingest.inference import HTTPInferenceClient

    ids = [kb.upsert(NodeRecord(source="obsidian", external_id=f"b{i}", type="note",
                                content=f"note {i}", slug=f"b{i}", parent_path="")).id
           for i in range(3)]
    kb.flag_stale_enrichment()
    enricher = EmbeddingEnricher(HTTPInferenceClient(_dead_inference_url(), timeout=5))
    lines: list[str] = []
    jobs = {j.name: j for j in D.build_jobs(kb, pool, [], enrichers=[enricher],
                                            log=lines.append)}
    assert jobs["enrichment"].run() == 1                  # one tried, the rest handed back
    assert {kb.get(i).enrichment_status for i in ids} == {"pending"}
    assert kb.provider_paused("local") is not None
    events = [r["event"] for r in kb.ingest_log()]
    assert "enrichment_failed" not in events and "enrichment_deferred" in events
    # The lane holds while `local` is paused: no claim, and `more` does not re-tick it.
    assert jobs["enrichment"].run() == 0
    assert jobs["enrichment"].more(0) is False
    assert any("enrichment paused: local" in ln for ln in lines)
    # Inference comes up, the pause lapses: the deferred notes enrich on the next tick.
    class _Up:
        provider = "local"
        def embed(self, texts):
            return [[0.1] * 1024 for _ in texts]
    enricher._client = _Up()
    with pool.connection() as conn:
        conn.execute("DELETE FROM provider_pause")
    assert jobs["enrichment"].run() == 3
    assert {kb.get(i).enrichment_status for i in ids} == {"done"}


def test_ollama_down_behind_a_running_inference_server_defers_instead_of_failing(kb, pool):
    """KB_Inference is up but its Ollama is stopped: the server answers 503 + Retry-After.
    That is the same outage as a refused connection — the note goes back to 'pending' and
    `local` pauses for the stated time — not a per-note failure needing a manual retry."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from kb_core.ingest.embedding import EmbeddingEnricher
    from kb_core.ingest.inference import HTTPInferenceClient

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            body = b'{"detail": "embed unavailable: the model backend (Ollama) is down"}'
            self.send_response(503)
            self.send_header("Retry-After", "30")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    ids = [kb.upsert(NodeRecord(source="obsidian", external_id=f"o{i}", type="note",
                                content=f"note {i}", slug=f"o{i}", parent_path="")).id
           for i in range(2)]
    kb.flag_stale_enrichment()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        enricher = EmbeddingEnricher(HTTPInferenceClient(
            f"http://127.0.0.1:{srv.server_address[1]}", timeout=5))
        jobs = {j.name: j for j in D.build_jobs(kb, pool, [], enrichers=[enricher],
                                                log=lambda _l: None)}
        assert jobs["enrichment"].run() == 1              # one tried, the rest handed back
    finally:
        srv.shutdown()
        srv.server_close()
    assert {kb.get(i).enrichment_status for i in ids} == {"pending"}
    paused = kb.provider_paused("local")
    assert paused is not None
    left = (paused - datetime.now(timezone.utc)).total_seconds()
    assert 20 <= left <= 30, left                         # the server's Retry-After
    events = [r["event"] for r in kb.ingest_log()]
    assert "enrichment_failed" not in events and "enrichment_deferred" in events
