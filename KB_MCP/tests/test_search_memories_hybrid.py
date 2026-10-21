"""search_memories' WIRE OUTPUT must not change by a byte: agents read it, and the
memory-retrieval heatmap aggregates its trace `results`. It still serves rows from
KnowledgeBase.search() (its replay comes from a separate pre-search flat-pool pass);
this oracle -- the legacy body reproduced verbatim -- pins that any future change
keeps the wire identical (e.g. rank is a SQL numeric string, not a float).

One deliberate change since: prose fields (snippet, summary) now carry the per-response
untrusted-content frame (test_untrusted_framing.py). The oracle compares with the frame
stripped, and separately pins that every non-empty prose field carries it, so the
comparison is still byte-exact for everything else."""

import asyncio
import json

import pytest

from kb_core.domain import NodePatch, NodeRecord
from kb_core.knowledge_base import search_confidence
from kb_mcp.server import (FRAMED_FIELDS, SNIPPET_CHARS, QueryVectors, _hit_dict,
                           build_server, unframe)


def _unit(i):
    v = [0.0] * 1024
    v[i] = 1.0
    return v


def _legacy(kb, query, embedding, *, under=None, kind=None, tags=None, limit=20):
    hits = kb.search(query, under=under, kind=kind, tags=tags, embedding=embedding,
                     include_dead=True, limit=limit)
    confidence = search_confidence(
        hits, scoped=any(x is not None for x in (under, kind, tags)))
    return {
        "confidence": confidence,
        "embedder": "ok" if embedding is not None else "unavailable",
        "results": [{"rank": r.rank, "similarity": r.similarity,
                     "snippet": (r.snippet or "")[:SNIPPET_CHARS],
                     **_hit_dict(r.node, lambda text: text)}
                    for r in hits],
    }


@pytest.fixture
def corpus(kb, monkeypatch):
    monkeypatch.setattr(kb, "_record_access", lambda ids: None)
    rows = [("a", "postgres vector index tuning", ["db"], "proj"),
            ("b", "postgres budget meeting", [], "proj"),
            ("c", "vector latency notes", ["perf", "db"], "ops"),
            ("d", "quarterly budget review", [], "ops")]
    for i, (ext, body, tags, parent) in enumerate(rows):
        n = kb.upsert(NodeRecord(source="agent", external_id=ext, type="note",
                                 content=body, slug=ext, title=body.title(),
                                 tags=tags, parent_path=parent))
        kb.set_embedding(n.id, _unit(i), embedded_hash=f"h{i}")
        if ext == "c":
            kb.update(n.id, NodePatch(doc_kind="runbook"))
    return kb


def _tool(kb, args, vec):
    qe = (lambda text, want_label: QueryVectors(retrieval=vec)) if vec else None
    server = build_server(kb, query_embedder=qe)
    return asyncio.run(server.call_tool("search_memories", args))[1]


def _norm(d):
    return json.loads(json.dumps(d, default=str))


def _unframed(out):
    """The tool's output with each row's frame stripped, after checking it is there."""
    rows = []
    for row in out["results"]:
        for key in FRAMED_FIELDS:
            if row.get(key):
                assert row[key].startswith("<untrusted-"), (key, row[key])
        rows.append({k: (unframe(v) if k in FRAMED_FIELDS else v) for k, v in row.items()})
    return {**out, "results": rows}


@pytest.mark.parametrize("args,vec,legacy_kw", [
    ({"query": "postgres"}, None, {}),                                   # FTS only
    ({"query": "postgres vector"}, _unit(0), {}),                        # hybrid
    ({"query": "budget", "under": "ops"}, _unit(3), {"under": "ops"}),   # filtered
    ({"query": "budget", "kind": "runbook"}, _unit(2), {"kind": "runbook"}),
    ({"query": "budget latency"}, None, {}),                             # OR-fallback
    ({"query": "vector", "limit": 2}, _unit(2), {"limit": 2}),
])
def test_search_memories_wire_output_is_unchanged(corpus, args, vec, legacy_kw):
    expected = _norm(_legacy(corpus, args["query"], vec, **legacy_kw))
    assert _norm(_unframed(_tool(corpus, args, vec))) == expected
