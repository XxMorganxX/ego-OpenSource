from types import SimpleNamespace

import pytest

from kb_core.analysis import daemon


class _FakeWorker:
    last_changed = 2

    def __init__(self, events):
        self._events = events
        self.runs = 0

    def run(self):
        self.runs += 1
        if self.runs > 1:
            raise KeyboardInterrupt
        self._events.append("run")
        return 3


class _FakeListener:
    closed = False

    def execute(self, _sql):
        return None

    def notifies(self, *, timeout):
        return ()

    def close(self):
        self.closed = True


def _run_main(monkeypatch, kb, worker, *, clients=None, worker_kwargs=None):
    passes = [SimpleNamespace(name=name)
              for name in ("entity_gate", "extract_entities", "doc_kind")]
    monkeypatch.setattr(daemon.os, "environ", {
        "KB_DATABASE_URL": "postgresql://unused",
        "KB_REANALYZE_PERIOD": "0",
    })
    monkeypatch.setattr(daemon.sys, "argv", ["kb-mcp-reanalyzed"])
    monkeypatch.setattr(daemon, "open_pool", lambda _dsn: object())
    monkeypatch.setattr(daemon, "migrate_pool", lambda _pool: None)
    monkeypatch.setattr(daemon, "default_registry", lambda: object())
    monkeypatch.setattr(daemon, "KnowledgeBase", lambda _pool, _registry, **_kw: kb)
    monkeypatch.setattr(daemon, "load_passes", lambda _env, _policy: passes)
    monkeypatch.setattr(daemon, "passes_for_phase", lambda passes, _phase: passes)
    monkeypatch.setattr(daemon, "InferenceRouter",
                        lambda _env, pool=None: SimpleNamespace(describe=lambda: "test"))
    monkeypatch.setattr(daemon, "clients_for_passes", lambda _router, _passes: clients or {})
    monkeypatch.setattr(daemon, "reanalysis_sources", lambda _env: ["agent"])
    monkeypatch.setattr(daemon, "LLMEnricher", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(daemon, "build_embedding_enricher", lambda _env: None)
    def _worker(*_args, **kwargs):
        if worker_kwargs is not None:
            worker_kwargs.update(kwargs)
        return worker

    monkeypatch.setattr(daemon, "ReanalysisWorker", _worker)
    monkeypatch.setattr(daemon.psycopg, "connect", lambda *_args, **_kwargs: _FakeListener())
    monkeypatch.setattr(daemon.time, "sleep", lambda _seconds: None)

    daemon.main()


def test_main_adopts_existing_cursors_and_reports_changed_count(monkeypatch, capsys):
    """Boot must adopt before the first tick; otherwise every old node is immediately due."""
    events = []

    class FakeKB:
        policy = object()

        def active_class_hash(self):
            events.append("class_hash")
            return "classes"

        def doc_kind_vocab_hash(self):
            events.append("kind_hash")
            return "kinds"

        def adopt_vocabulary_cursors(self, *, class_hash, kind_hash):
            events.append(("adopt", class_hash, kind_hash))
            return 42

    _run_main(monkeypatch, FakeKB(), _FakeWorker(events))

    assert events == ["class_hash", "kind_hash", ("adopt", "classes", "kinds"), "run"]
    output = capsys.readouterr().out
    assert "adopted current vocabulary for 42 node(s)" in output
    assert "re-analyzed 3 node(s), 2 changed" in output


def test_main_retries_failed_cursor_adoption_before_crawling(monkeypatch, capsys):
    events = []

    class FlakyKB:
        policy = object()
        attempts = 0

        def active_class_hash(self):
            self.attempts += 1
            if self.attempts == 1:
                events.append("hash_failed")
                raise RuntimeError("database unavailable")
            events.append("class_hash")
            return "classes"

        def doc_kind_vocab_hash(self):
            events.append("kind_hash")
            return "kinds"

        def adopt_vocabulary_cursors(self, *, class_hash, kind_hash):
            events.append(("adopt", class_hash, kind_hash))
            return 1

    _run_main(monkeypatch, FlakyKB(), _FakeWorker(events))

    assert events == ["hash_failed", "class_hash", "kind_hash",
                      ("adopt", "classes", "kinds"), "run"]
    assert "cursor adoption failed" in capsys.readouterr().out


def test_required_recrawl_passes_fail_loudly():
    passes = [SimpleNamespace(name="doc_kind")]

    with pytest.raises(RuntimeError) as caught:
        daemon._validate_recrawl_passes(passes)
    assert "entity_gate" in str(caught.value)
    assert "extract_entities" in str(caught.value)


def test_adopt_only_migrates_and_adopts_before_loading_inference(monkeypatch, capsys):
    events = []

    class FakeKB:
        def active_class_hash(self):
            events.append("class_hash")
            return "classes"

        def doc_kind_vocab_hash(self):
            events.append("kind_hash")
            return "kinds"

        def adopt_vocabulary_cursors(self, *, class_hash, kind_hash):
            events.append(("adopt", class_hash, kind_hash))
            return 42

    monkeypatch.setattr(daemon.os, "environ", {"KB_DATABASE_URL": "postgresql://unused"})
    monkeypatch.setattr(daemon.sys, "argv", ["kb-mcp-reanalyzed", "--adopt-only"])
    monkeypatch.setattr(daemon, "open_pool", lambda _dsn: "pool")
    monkeypatch.setattr(daemon, "migrate_pool", lambda pool: events.append(("migrate", pool)))
    monkeypatch.setattr(daemon, "default_registry", lambda: object())
    monkeypatch.setattr(daemon, "KnowledgeBase", lambda _pool, _registry, **_kw: FakeKB())
    monkeypatch.setattr(
        daemon, "load_passes", lambda *_args: pytest.fail("must not load inference config"))

    daemon.main()

    assert events == [
        ("migrate", "pool"),
        "class_hash",
        "kind_hash",
        ("adopt", "classes", "kinds"),
    ]
    assert "cursor adoption complete: 42 node(s)" in capsys.readouterr().out


def test_main_gates_the_recrawl_lane_on_its_remote_providers(monkeypatch):
    """Same gate shape as the ingest enrich lane: every remote provider a recrawl pass
    rides, never local."""
    class KB:
        policy = object()

        def active_class_hash(self):
            return "c"

        def doc_kind_vocab_hash(self):
            return "k"

        def adopt_vocabulary_cursors(self, *, class_hash, kind_hash):
            return 0

    kwargs = {}
    clients = {"entity_gate": SimpleNamespace(provider="agent"),
               "extract_entities": SimpleNamespace(provider="agent"),
               "doc_kind": SimpleNamespace(provider="local")}
    _run_main(monkeypatch, KB(), _FakeWorker([]), clients=clients, worker_kwargs=kwargs)
    assert kwargs["gate_providers"] == ("agent",)
