"""The query-vector seam: which text is embedded, which
vector reaches which consumer, and what the trace records."""
import asyncio

import pytest

import kb_mcp.server as srv
from kb_core.domain import NodeRecord
from kb_core.embedding import QUERY_INSTRUCTION_VERSION, instruct_query
from kb_core.ingest.inference import InferenceError
from kb_mcp.server import (NO_VECTORS, QueryVectors, build_server, embedders_from_env,
                           make_query_embedders)


def _unit(i):
    v = [0.0] * 1024
    v[i] = 1.0
    return v


RETRIEVAL, LABEL, BARE = _unit(3), _unit(7), _unit(9)


def _call(server, tool, args):
    return asyncio.run(server.call_tool(tool, args))[1]


class _FakeClient:
    """Records every /embed batch; replies one distinct unit vector per input."""

    def __init__(self, reply=None, exc=None):
        self.calls, self._reply, self._exc = [], reply, exc

    def embed(self, texts):
        self.calls.append(list(texts))
        if self._exc is not None:
            raise self._exc
        if self._reply is not None:
            return self._reply(texts)
        return [_unit(i + 1) for i in range(len(texts))]


def _qe_spy(seen):
    def qe(text, want_label):
        seen.append((text, want_label))
        return QueryVectors(retrieval=RETRIEVAL, label=LABEL if want_label else None,
                            version=QUERY_INSTRUCTION_VERSION)
    return qe


# ---- the factory --------------------------------------------------------------

def test_search_only_sends_the_instructed_text():
    c = _FakeClient()
    _, qe = make_query_embedders(c)
    qv = qe("pgvector rebuild", False)
    assert c.calls == [[instruct_query("pgvector rebuild")]]
    assert (qv.retrieval, qv.label, qv.version) == (_unit(1), None, QUERY_INSTRUCTION_VERSION)


def test_both_vectors_come_from_one_batch():
    c = _FakeClient()
    _, qe = make_query_embedders(c)
    qv = qe("pgvector rebuild", True)
    assert c.calls == [[instruct_query("pgvector rebuild"), "pgvector rebuild"]]
    assert (qv.retrieval, qv.label) == (_unit(1), _unit(2))


def test_a_blank_query_embeds_nothing():
    c = _FakeClient()
    _, qe = make_query_embedders(c)
    assert qe("   ", True) == NO_VECTORS
    assert c.calls == []


@pytest.mark.parametrize("client", [
    _FakeClient(exc=InferenceError("down")),
    _FakeClient(reply=lambda texts: [_unit(1)]),            # short batch
    _FakeClient(reply=lambda texts: None),                  # "embeddings": null
    _FakeClient(reply=lambda texts: [_unit(1), []]),        # an empty vector
], ids=["error", "short", "null", "empty-vector"])
def test_any_bad_batch_yields_no_vectors_never_a_mixed_pair(client):
    _, qe = make_query_embedders(client)
    assert qe("pgvector rebuild", True) == NO_VECTORS


def test_the_bare_embedder_is_unchanged():
    c = _FakeClient()
    emb, _ = make_query_embedders(c)
    assert emb("x" * 7000) == _unit(1)
    assert c.calls == [["x" * 6000]]


def test_long_queries_are_clipped_after_instructing():
    c = _FakeClient()
    _, qe = make_query_embedders(c)
    qe("y" * 7000, True)
    instructed, bare = c.calls[0]
    assert instructed == instruct_query("y" * 7000)[:6000]
    assert bare == "y" * 6000


def test_no_inference_url_means_no_embedders():
    assert embedders_from_env({}) == (None, None)


def test_production_instructs_the_retrieval_vector(monkeypatch):
    calls = []

    class _Client:
        def __init__(self, url, timeout, token=None):
            pass

        def embed(self, texts):
            calls.append(list(texts))
            return [_unit(1) for _ in texts]

    monkeypatch.setattr(srv, "HTTPInferenceClient", _Client)
    emb, qe = embedders_from_env({"KB_INFERENCE_URL": "http://127.0.0.1:9"})
    assert callable(emb)
    assert qe("pgvector", False).version == QUERY_INSTRUCTION_VERSION
    assert calls == [[instruct_query("pgvector")]]


def test_embedders_from_env_forwards_the_inference_token(monkeypatch):
    seen = {}

    class _Client:
        def __init__(self, url, timeout, token=None):
            seen["token"] = token

        def embed(self, texts):
            return [_unit(1) for _ in texts]

    monkeypatch.setattr(srv, "HTTPInferenceClient", _Client)
    embedders_from_env({"KB_INFERENCE_URL": "http://127.0.0.1:9",
                        "KB_INFERENCE_TOKEN": "mcp-token"})
    assert seen["token"] == "mcp-token"


# ---- MCP wiring -----------------------------------------------------------------

def test_search_memories_ranks_on_the_retrieval_vector(kb, monkeypatch):
    seen_q, got = [], {}
    real = kb.search

    def spy(*a, **kw):
        got["embedding"] = kw.get("embedding")
        return real(*a, **kw)

    monkeypatch.setattr(kb, "search", spy)
    server = build_server(kb, embedder=lambda t: BARE, query_embedder=_qe_spy(seen_q))
    out = _call(server, "search_memories", {"query": "pgvector"})
    assert seen_q == [("pgvector", False)]
    assert got["embedding"] == RETRIEVAL
    assert out["embedder"] == "ok"


def _strategy_spy(monkeypatch, got):
    real = srv.get_strategy

    class _Spy:
        def __init__(self, inner):
            self._inner = inner

        def search(self, q):
            got["q"] = q
            return self._inner.search(q)

    monkeypatch.setattr(srv, "get_strategy",
                        lambda name, kb_, **kw: _Spy(real(name, kb_, **kw)))


def test_search_graph_hands_the_strategy_both_vectors_from_one_embed(kb, monkeypatch):
    seen_q, got = [], {}
    _strategy_spy(monkeypatch, got)
    server = build_server(kb, embedder=lambda t: BARE, query_embedder=_qe_spy(seen_q))
    _call(server, "search_graph", {"query": "pgvector", "judge": "none"})
    assert seen_q == [("pgvector", True)]
    assert got["q"].embedding == RETRIEVAL
    assert got["q"].label_embedding == LABEL


def test_find_entity_keeps_the_bare_vector(kb, monkeypatch):
    got, seen_q = {}, []
    real = kb.find_entities

    def spy(name, **kw):
        got.update(kw)
        return real(name, **kw)

    monkeypatch.setattr(kb, "find_entities", spy)
    server = build_server(kb, embedder=lambda t: BARE, query_embedder=_qe_spy(seen_q))
    _call(server, "find_entity", {"name": "Zeta"})
    assert got["embedding"] == BARE
    assert seen_q == []


def test_an_embedder_alone_still_serves_both_roles(kb, monkeypatch):
    """Every pre-existing test injects only embedder=: one bare vector, no marker."""
    got = {}
    _strategy_spy(monkeypatch, got)
    server = build_server(kb, embedder=lambda t: BARE)
    _call(server, "search_graph", {"query": "pgvector", "judge": "none"})
    assert got["q"].embedding == BARE
    assert got["q"].label_vector() == BARE


def test_no_vectors_degrade_to_keyword_only(kb):
    kb.upsert(NodeRecord(source="agent", external_id="k1", type="note",
                         content="zanzibar retrospective", slug="k1"))
    server = build_server(kb, embedder=lambda t: None,
                          query_embedder=lambda t, want_label: NO_VECTORS)
    out = _call(server, "search_memories", {"query": "zanzibar"})
    assert out["embedder"] == "unavailable"
    assert out["confidence"]["level"] == "keyword"
    row = kb.trace_recent()[0]
    assert row["embedder_ok"] is False
    assert "query_vector" not in (kb.trace_get(row["id"])["trace"] or {})


# ---- trace markers ------------------------------------------------------------------

def test_search_traces_carry_the_query_vector_marker(kb):
    server = build_server(kb, embedder=lambda t: BARE, query_embedder=_qe_spy([]))
    _call(server, "search_memories", {"query": "pgvector"})
    _call(server, "search_graph", {"query": "pgvector", "judge": "none"})
    rows = {r["tool"]: r for r in kb.trace_recent()}
    for tool in ("search_memories", "search_graph"):
        trace = kb.trace_get(rows[tool]["id"])["trace"]
        assert trace["query_vector"] == QUERY_INSTRUCTION_VERSION, tool
    # search_graph keeps its own trace payload alongside the marker
    assert "applied_filters" in kb.trace_get(rows["search_graph"]["id"])["trace"]


def test_bare_vectors_leave_no_marker(kb):
    server = build_server(kb, embedder=lambda t: BARE)
    _call(server, "search_memories", {"query": "pgvector"})
    row = kb.trace_recent()[0]
    assert "query_vector" not in (kb.trace_get(row["id"])["trace"] or {})

    # search_graph's trace dict exists for every call (it carries applied_filters
    # regardless of embedder) -- a bare-only embedder must still leave it unmarked.
    _call(server, "search_graph", {"query": "pgvector", "judge": "none"})
    row = kb.trace_recent()[0]
    trace = kb.trace_get(row["id"])["trace"]
    assert "query_vector" not in trace
    assert "applied_filters" in trace


def test_find_entity_records_embedder_health(kb):
    _call(build_server(kb, embedder=lambda t: None), "find_entity", {"name": "Zeta"})
    assert kb.trace_recent()[0]["embedder_ok"] is False
    _call(build_server(kb, embedder=lambda t: BARE), "find_entity", {"name": "Zeta"})
    assert kb.trace_recent()[0]["embedder_ok"] is True


def test_call_discards_stale_trace_notes_before_the_body_runs(kb, monkeypatch):
    """_traced's _call must clear _TRACE_NOTES before `fn(...)` runs, not only read it
    afterwards via the extractor -- otherwise a note left by an earlier call that noted
    and then raised (skipping the extractor entirely) would leak into a LATER call that
    happens to land on the same pooled worker thread.

    `anyio.to_thread.run_sync` does not guarantee which OS thread runs any given call,
    so this test cannot reliably force two calls onto the same thread. Instead it pins
    the call ORDER within a single call: the discard-read must happen before the tool
    body runs, not just after (in the extractor). A spy on `_take_trace_notes` and a
    probe inside the embedder (standing in for "the body ran") makes that order directly
    observable, independent of threading.
    """
    order = []
    real_take = srv._take_trace_notes

    def spy_take():
        order.append("take")
        return real_take()

    def embedder(name):
        order.append("body")
        return BARE

    monkeypatch.setattr(srv, "_take_trace_notes", spy_take)
    server = build_server(kb, embedder=embedder)
    _call(server, "find_entity", {"name": "Zeta"})

    # One "take" at the top of _call (the discard) before the body runs, and a second
    # from _trace_find_entity's own read of the note the body just left.
    assert order == ["take", "body", "take"]
