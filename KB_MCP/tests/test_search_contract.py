"""The search_memories agent contract: payload economy, limit clamps, confidence.

Why it is shaped this way:
  * one default search returned hundreds of KB, almost all of it note bodies;
  * limits went unbounded into SQL, so limit=-5 leaked a driver error;
  * RRF rank is a position artifact, so an off-corpus query was indistinguishable
    from a correct one.
"""

import asyncio

from kb_core.domain import NodeRecord
from kb_mcp.server import MAX_LIMIT, build_server, unframe


def _seed(kb, n=3, body="alpha beta gamma delta"):
    made = []
    for i in range(n):
        made.append(kb.upsert(NodeRecord(
            source="agent", external_id=f"sc{i}", type="note",
            content=f"{body} number {i}", slug=f"sc{i}", title=f"Note {i}")))
    return made


def _search(kb, args, embedder=None):
    server = build_server(kb, embedder=embedder)
    return asyncio.run(server.call_tool("search_memories", args))[1]


def test_rows_omit_the_note_body_but_advertise_its_length(kb):
    made = _seed(kb, 1)
    out = _search(kb, {"query": "alpha"})
    assert out["results"], "seeded note should match"
    row = out["results"][0]
    assert "content" not in row
    assert row["content_chars"] == len(made[0].content)


def test_rows_drop_internal_bookkeeping(kb):
    """Dashboard telemetry is not agent-actionable and cost context on every row."""
    _seed(kb, 1)
    row = _search(kb, {"query": "alpha"})["results"][0]
    for noise in ("importance_modifiers", "access_count", "entity_skip_reason",
                  "entity_eligible", "embedded_hash"):
        assert noise not in row


def test_limit_is_clamped_at_both_ends(kb):
    _seed(kb, 3)
    # Negative went straight into SQL LIMIT and surfaced the raw driver error.
    assert _search(kb, {"query": "alpha", "limit": -5})["results"]
    huge = _search(kb, {"query": "alpha", "limit": 10_000_000})
    assert len(huge["results"]) <= MAX_LIMIT


def test_snippet_is_bounded(kb):
    """LONG TOKENS, not just a long note: ts_headline caps at ~35 WORDS, not
    characters, so "alpha " + "padding " * 5000 -- five-letter tokens -- yields only
    a ~127-char raw snippet, well inside 400 whether or not `_hit_dict`'s call site
    slices it. That made this assertion decorative: deleting `[:SNIPPET_CHARS]` left
    it passing. 30-char tokens push the same 35-word cap to a (measured) 446-char
    raw snippet, so the slice is what keeps the assertion true."""
    kb.upsert(NodeRecord(source="agent", external_id="long", type="note",
                         content="alpha " + ("paddingtokenlongwordxxxxxxxxxx " * 50),
                         slug="long"))
    row = _search(kb, {"query": "alpha"})["results"][0]
    # The bound is on the note text; the untrusted-content frame wraps it after.
    assert len(unframe(row["snippet"])) <= 400


def test_confidence_is_none_when_nothing_matches(kb):
    """The failure this exists to prevent: answering confidently from junk rows."""
    _seed(kb, 1)
    out = _search(kb, {"query": "quantum cryptography patent filing in Tokyo"})
    assert out["results"] == []
    assert out["confidence"]["level"] == "none"


def test_confidence_reports_keyword_when_there_is_no_vector_arm(kb):
    """FTS-only hits are self-evidently relevant; they are not 'low confidence'."""
    _seed(kb, 1)
    out = _search(kb, {"query": "alpha"})
    assert out["results"]
    assert out["confidence"]["level"] == "keyword"
    assert out["confidence"]["best_similarity"] is None


def test_confidence_is_strong_on_an_exact_vector_match(kb):
    vec = [0.0] * 1024
    vec[3] = 1.0
    mem = kb.upsert(NodeRecord(source="agent", external_id="v1", type="note",
                               content="wholly unrelated lexical content", slug="v1"))
    kb.set_embedding(mem.id, vec, embedded_hash="h")
    out = _search(kb, {"query": "no shared words at all"}, embedder=lambda q: vec)
    assert out["results"][0]["id"] == str(mem.id)
    assert out["confidence"]["level"] == "strong"
    assert out["confidence"]["best_similarity"] > 0.99


def test_embedder_availability_is_disclosed(kb):
    _seed(kb, 1)
    assert _search(kb, {"query": "alpha"})["embedder"] == "unavailable"
    vec = [0.0] * 1024
    vec[1] = 1.0
    assert _search(kb, {"query": "alpha"}, embedder=lambda q: vec)["embedder"] == "ok"


def test_list_memories_defaults_to_notes(kb):
    """Page 1 was 100% scaffolding: 'entities' sorts before 'Obsidian' by path."""
    _seed(kb, 2)
    kb.upsert_entity("Globex")
    server = build_server(kb)
    rows = asyncio.run(server.call_tool("list_memories", {}))[1]["result"]
    assert rows, "should return the seeded notes"
    assert {r["type"] for r in rows} == {"note"}
    # Opt out explicitly to see scaffolding.
    every = asyncio.run(server.call_tool("list_memories", {"type": None}))[1]["result"]
    assert {r["type"] for r in every} > {"note"}
