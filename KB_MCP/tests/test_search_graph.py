"""The search_graph tool: the crawl's only agent-facing surface.

EVERY test here pins judge="none", except the two judge-default tests below which exercise
the default and the rejected "llm" value directly -- both are cheap and safe to
run for real now that the tier they used to guard against is gone. The
deterministic judge is the production fail-soft path; it is what makes
the whole crawl testable with no LLM, no network and no subprocess -- and CI has
none of the three.

Historical note: DEFAULT_MCP_HOP_JUDGE once defaulted `judge` to
"llm" here while kb_retrieval's own library default stayed "none" (a deliberate
design rule: a default must never silently opt a caller into an expensive
path) -- so THIS tool was the one thing that made production agentic, and the test
that mattered most in this file was the one asserting it still asked for "llm" by
default. A 2x2 benchmark measured that tier losing recall at far higher latency;
it was deleted and
DEFAULT_MCP_HOP_JUDGE flipped to "none" to match -- see server.py's search_graph docstring
and DEFAULT_MCP_HOP_JUDGE's own comment for the full account.
"""

import asyncio
import json
import threading
from datetime import datetime, timezone

import pytest

from kb_core.domain import NodePatch, NodeRecord
from kb_mcp.server import DEFAULT_MCP_HOP_JUDGE, MAX_LIMIT, build_server, unframe

# Every corpus note gets this. `SEARCH_FILTERS` filters on
# `COALESCE(event_at, source_created_at)` (knowledge_base.SEARCH_FILTERS), and
# `nodes.source_created_at` has NO DEFAULT (migration 0001_init.sql) -- so a note written
# through `kb.upsert` with neither column set is invisible to ANY since/until query.
# The `since` tests below would then index `results[0]` on an empty list and
# IndexError. Dating the corpus keeps the facet meaningfully exercised instead.
CORPUS_EVENT_AT = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _vec(hot):
    v = [0.0] * 1024
    v[hot] = 1.0
    return v


def _note(kb, ext, content, **kw):
    """Every note is DATED (see CORPUS_EVENT_AT) unless a caller overrides it."""
    kw.setdefault("event_at", CORPUS_EVENT_AT)
    return kb.upsert(NodeRecord(source="agent", external_id=ext, type="note",
                                content=content, slug=ext, **kw))


def _concept(kb, name, members, *, embedding=None):
    """One active concept through the real write path -- apply_concept_run is the
    only way in, mirroring test_server.py's _seed_concept."""
    run = kb.begin_concept_run({})
    kb.apply_concept_run(run, [
        {"key": name, "prev_id": None, "parent_key": None, "level": 0,
         "name": name, "kind": "topic", "description": f"{name} work",
         "embedding": embedding, "members": members}], [])
    kb.finish_concept_run(run, status="done", stats={})
    return {c["name"]: c for c in kb.active_concepts()}[name]["id"]


def _routed_corpus(kb):
    """A corpus where the crawl can reach something flat structurally cannot.

    `reachable` has NO embedding and no lexical overlap with the query, so the flat
    arm cannot see it at all: the FTS arm does not match it and knn_candidates'
    predicate is `embedding IS NOT NULL`. Its only route is concept membership.

    The anchor also carries a SUMMARY, TAGS and a URI, so the row-parity test can
    assert on real values rather than on `None`. Both notes are dated -- see
    CORPUS_EVENT_AT for why that is load-bearing rather than tidy.

    Returns (anchor, reachable, concept_id)."""
    anchor = _note(kb, "anchor", "pgvector rebuild after the postgres upgrade",
                   title="Rebuild note")
    kb.set_embedding(anchor.id, _vec(9), embedded_hash="h-anchor")
    kb.update(anchor.id, NodePatch(summary="the pgvector rebuild, in one line",
                                   uri="https://example.invalid/anchor",
                                   tags=["infra", "postgres"]))
    reachable = _note(kb, "reachable", "quarterly cadence notes for the same effort",
                      title="Cadence note")
    cid = _concept(kb, "Vector Infra",
                   [(str(anchor.id), "memory", 1.0),
                    (str(reachable.id), "memory", 0.9)],
                   embedding=_vec(9))
    return anchor, reachable, cid


def _call(server, args):
    return asyncio.run(server.call_tool("search_graph", args))[1]


# ---------------------------------------------------------------------------
# the disclosure surface
# ---------------------------------------------------------------------------

def test_search_graph_returns_the_full_spec_s8_disclosure_surface(kb):
    """The design names six disclosure blocks besides the rows. A crawl that returned only rows
    would be indistinguishable from flat search at a far higher price, and the
    benchmark's cost/agentic_fraction columns would have nothing to read."""
    _routed_corpus(kb)
    server = build_server(kb, embedder=lambda q: _vec(9))
    out = _call(server, {"query": "embedding extension broken", "judge": "none"})

    assert set(out) == {"strategy", "confidence", "embedder", "cost", "trace",
                        "applied_filters", "ignored_filters", "results"}
    assert out["strategy"] == "cluster-crawl"
    assert out["embedder"] == "ok"
    assert set(out["confidence"]) == {"level", "best_similarity", "n", "basis"}
    # The confidence disclosure key: WHICH pool best_similarity was computed over.
    assert out["confidence"]["basis"] == "crawl-pool"
    # cost.llm_calls == 0 is the machine-checkable "this was not an agentic run"
    # flag. Under judge="none" it must be exactly 0, never absent.
    assert out["cost"]["llm_calls"] == 0
    # `> 0`, not `>= 0`. Both are non-negative ints by construction, so `>= 0` cannot
    # fail for any producible value and would be a decoration. There ARE rows here
    # with titles and snippets, so tokens_returned is genuinely positive -- and a
    # strategy that forgot to accumulate it would report exactly 0, which is the
    # regression worth catching (contract.py makes tokens_returned a MUST).
    assert out["cost"]["tokens_returned"] > 0
    assert set(out["cost"]) == {"tokens_returned", "latency_ms", "llm_calls",
                                "provider_tokens", "provider_cost_usd"}
    assert out["cost"]["provider_tokens"] is None    # no LLM tier ran
    assert out["trace"]["crawl_mode"] == "deterministic"
    assert out["trace"]["llm"] is None          # no LLM tier selected at all
    assert out["trace"]["time_basis"] == "event_at, else source_created_at"
    assert out["trace"]["entry"], "the concept layer should have routed this query"
    assert {e["via"] for e in out["trace"]["entry"]} <= {
        "label-cosine", "evidence-vote", "name", "fuzzy-name", "anchor_entity"}


def test_search_graph_reaches_a_memory_the_flat_ranker_structurally_cannot(kb):
    """THE reason this tool exists. `reachable` has no vector and no lexical
    overlap, so search_memories cannot return it at any limit -- the FTS arm does
    not match and the kNN arm's predicate is `embedding IS NOT NULL`. Concept
    membership is its only route.

    Without this test the whole tool could be a slower alias for search_memories
    and every other test here would still pass."""
    _anchor, reachable, _cid = _routed_corpus(kb)
    server = build_server(kb, embedder=lambda q: _vec(9))

    flat = asyncio.run(server.call_tool(
        "search_memories", {"query": "embedding extension broken", "limit": 50}))[1]
    assert str(reachable.id) not in {r["id"] for r in flat["results"]}

    out = _call(server, {"query": "embedding extension broken", "judge": "none",
                         "limit": 50})
    row = next(r for r in out["results"] if r["id"] == str(reachable.id))
    # `via` says WHICH arm produced it. No "flat" tag == a row search_memories
    # would not have returned; that is the field an agent reads to see what the
    # crawl added.
    assert row["via"] == ["concept"]
    assert row["similarity"] is None      # in scope, but genuinely unscoreable


def test_search_graph_discloses_the_concept_arms_dead_blind_spot(kb):
    """include_dead=True is ACCEPTED and passed down, but consolidation builds its
    graph dead-free, so the concept arm can only ever fail to REMOVE rows, never
    add the ones consolidation never wrote. In practice some must-gold rows can be
    dead and therefore unreachable by this arm. Disclosure is
    the whole contract
    (contract.py's module docstring) -- a silently dropped facet must not be scoreable as a
    win."""
    _routed_corpus(kb)
    server = build_server(kb, embedder=lambda q: _vec(9))
    on = _call(server, {"query": "embedding extension broken", "judge": "none",
                        "include_dead": True})
    assert on["ignored_filters"] == ["include_dead@concept-arm"]
    off = _call(server, {"query": "embedding extension broken", "judge": "none",
                         "include_dead": False})
    assert off["ignored_filters"] == []
    assert off["applied_filters"]["include_dead"] is False


# ---------------------------------------------------------------------------
# the judge default
# ---------------------------------------------------------------------------

def test_search_graph_defaults_to_the_deterministic_judge_and_rejects_llm(kb):
    """Post-deletion: DEFAULT_MCP_HOP_JUDGE is "none" now, matching
    kb_retrieval's own library default -- so omitting `judge` reaches the crawl for
    real, no spy required (there is no live inference client left to avoid). A
    default search_graph call must come back exactly as if judge="none" had been
    passed explicitly: no LLM tier, ever.

    The LLM tier is gone, not merely defaulted away from: HOP_JUDGES has exactly
    one entry, so `judge="llm"` is not a slower opt-in any more, it is an unknown
    name. get_judge raises that itself (kb_retrieval/crawl/judge.py); this asserts
    no new validation code intercepts it and FastMCP surfaces it as a ToolError, the
    same pattern test_search_graph_rejects_an_unknown_strategy_by_name pins for an
    unknown strategy name."""
    from mcp.server.fastmcp.exceptions import ToolError

    _routed_corpus(kb)
    server = build_server(kb, embedder=lambda q: _vec(9))

    assert DEFAULT_MCP_HOP_JUDGE == "none"
    out = _call(server, {"query": "embedding extension broken"})
    assert out["cost"]["llm_calls"] == 0
    assert out["trace"]["llm"] is None
    assert out["trace"]["crawl_mode"] == "deterministic"

    with pytest.raises(ToolError, match="unknown hop judge 'llm'"):
        _call(server, {"query": "embedding extension broken", "judge": "llm"})


def test_search_graph_lets_a_caller_ask_for_the_cheap_deterministic_crawl(kb, monkeypatch):
    """An explicit judge="none" must reach the strategy unchanged -- `judge or
    DEFAULT` would be correct for None but a `judge if judge is not None else
    DEFAULT` mistake in the other direction (or an `or` against the string "none")
    is the kind of thing that turns every cheap call into a 30 s one."""
    import kb_mcp.server as srv

    _routed_corpus(kb)
    real = srv.get_strategy
    seen = {}

    def spy(name, kb_, **kw):
        seen.update(kw)
        return real(name, kb_, **kw)

    monkeypatch.setattr(srv, "get_strategy", spy)
    server = build_server(kb, embedder=lambda q: _vec(9))
    out = _call(server, {"query": "embedding extension broken", "judge": "none"})
    assert seen["judge"] == "none"
    assert out["trace"]["crawl_mode"] == "deterministic"
    assert out["cost"]["llm_calls"] == 0


# ---------------------------------------------------------------------------
# the wire
# ---------------------------------------------------------------------------

def test_search_graph_puts_only_json_native_values_on_the_wire(kb):
    """Trace and Cost are dataclasses holding UUIDs and datetimes; applied_filters
    holds the caller's parsed since/until. MCP puts all of it on the wire as JSON."""
    _routed_corpus(kb)
    server = build_server(kb, embedder=lambda q: _vec(9))
    out = _call(server, {"query": "embedding extension broken", "judge": "none",
                         "since": "2026-01-01"})
    json.dumps(out)          # raises TypeError on any stray UUID/datetime
    assert isinstance(out["trace"]["consolidated_at"], str)
    assert isinstance(out["results"][0]["id"], str)


def test_search_graph_formats_datetimes_the_way_every_other_tool_here_does(kb):
    """FastMCP's dict[str, Any] path WILL serialise a raw datetime for you -- via
    pydantic, as '2026-01-01T00:00:00Z'. Every other tool in server.py emits
    `.isoformat()`, i.e. '+00:00'. Leaning on the automatic path would put two
    datetime formats in one server's output, differing per tool, for no reason a
    caller could discover. Verified on mcp 1.27.2: the two strings differ."""
    _routed_corpus(kb)
    server = build_server(kb, embedder=lambda q: _vec(9))
    out = _call(server, {"query": "embedding extension broken", "judge": "none",
                         "since": "2026-01-01"})
    # _parse_when pins a naive ISO date to UTC, so this value is tz-AWARE and the
    # two formats are genuinely distinguishable.
    assert out["applied_filters"]["since"] == "2026-01-01T00:00:00+00:00"
    assert out["trace"]["consolidated_at"] == kb.last_consolidation().isoformat()


def test_search_graph_rows_omit_the_body_and_bound_the_snippet(kb):
    """Payload-economy parity with search_memories (test_search_contract.py):
    one default search once returned hundreds of KB, almost all of it note bodies.
    Adding display fields made the row RICHER in metadata; it did not put the body back.

    LONG TOKENS, not just a long note: ts_headline caps at ~35 WORDS, not
    characters, so "postgres " + "padding " * 5000 -- five-letter tokens -- yields
    only a 127-char raw snippet, well inside 400 with or without `_row_dict`'s
    `[:SNIPPET_CHARS]` slice. That made the assertion below decorative: deleting the
    slice left it passing. 30-char tokens push the SAME 35-word cap to an
    (measured) 449-char raw snippet, so the slice is what keeps this test's
    assertion true -- and now provably load-bearing."""
    _routed_corpus(kb)
    long_note = _note(kb, "long",
                      "postgres " + ("paddingtokenlongwordxxxxxxxxxx " * 50),
                      title="Long")
    kb.set_embedding(long_note.id, _vec(9), embedded_hash="h-long")
    server = build_server(kb, embedder=lambda q: _vec(9))
    out = _call(server, {"query": "postgres", "judge": "none"})
    row = next(r for r in out["results"] if r["id"] == str(long_note.id))
    assert "content" not in row
    assert row["content_chars"] == len(long_note.content)
    # The bound is on the note text; the untrusted-content frame wraps it after.
    assert len(unframe(row["snippet"])) <= 400


def test_search_graph_rows_carry_everything_search_memories_rows_carry(kb):
    """TASK 8'S PARITY RULING, enforced at the wire.

    `_hit_dict` and `_row_dict` are siblings, not a reuse: `_hit_dict` takes a
    `kb_core.domain.Node`, a `kb_retrieval.contract.Row` is not one, and
    `_hit_dict(row)` still raises AttributeError. But the FIELD SET is now a
    superset. A later change added the seven display fields Row was missing -- type, tags,
    source, uri, summary, importance, dead_reason -- and populated them in BOTH
    strategies from the Node hydrate() already returned, at zero extra cost. Before
    that, search_graph returned strictly poorer rows than the tool it competes with,
    and the design's accepted costs say a mode B that is not compelling becomes dead
    code with a maintenance bill.

    DERIVED from _hit_dict's own output on a real Node, never a hand-copied list, so
    the day someone adds an eighth field to search_memories' rows THIS test is what
    says search_graph is about to ship the poorer one.

    One-directional: search_graph rows also carry similarity / rank / score / via /
    snippet, which search_memories has no counterpart for."""
    from kb_mcp.server import _hit_dict

    anchor, _reachable, _cid = _routed_corpus(kb)
    server = build_server(kb, embedder=lambda q: _vec(9))
    row = next(r for r in _call(server, {"query": "embedding extension broken",
                                         "judge": "none"})["results"]
               if r["id"] == str(anchor.id))

    missing = set(_hit_dict(kb.get(anchor.id), lambda text: text)) - set(row)
    assert missing == set(), (
        "search_graph rows are poorer than search_memories rows; missing: "
        f"{sorted(missing)}")
    # PRESENT AND CORRECT, not merely present. `row["summary"] is None or
    # isinstance(row["summary"], str)` is true of every producible value and would be
    # a decoration; _routed_corpus writes real values precisely so these can be
    # equalities. A _row_dict that emitted the key as a hardcoded None would pass the
    # superset check above and fail here.
    assert unframe(row["summary"]) == "the pgvector rebuild, in one line"
    assert row["tags"] == ["infra", "postgres"]
    assert row["uri"] == "https://example.invalid/anchor"
    assert row["type"] == "note"
    assert row["source"] == "agent"
    assert row["dead_reason"] is None          # live note, genuinely null
    for ranking_field in ("similarity", "rank", "score", "via", "snippet"):
        assert ranking_field in row


# ---------------------------------------------------------------------------
# argument handling -- the same machinery search_memories uses
# ---------------------------------------------------------------------------

def test_search_graph_clamps_limit_at_both_ends(kb, monkeypatch):
    """Same _clamp/MAX_LIMIT machinery as search_memories -- not a second one.
    limit=-5 went straight into SQL LIMIT and surfaced the raw driver error.

    BOTH ends are asserted on the QUERY THE STRATEGY RECEIVED, not on
    len(results). This corpus holds three notes, so `len(results) <= MAX_LIMIT`
    holds with the clamp deleted and is a decoration; the ONLY observable
    difference an upper clamp makes is the number that reaches Query.limit."""
    import kb_mcp.server as srv

    _routed_corpus(kb)
    real = srv.get_strategy
    seen = {}

    class _Spy:
        def __init__(self, inner):
            self._inner = inner

        def search(self, q):
            seen["limit"] = q.limit
            return self._inner.search(q)

    monkeypatch.setattr(srv, "get_strategy",
                        lambda name, kb_, **kw: _Spy(real(name, kb_, **kw)))
    server = build_server(kb, embedder=lambda q: _vec(9))

    assert _call(server, {"query": "postgres", "judge": "none",
                          "limit": -5})["results"]
    assert seen["limit"] == 1                  # floored, not passed through
    _call(server, {"query": "postgres", "judge": "none", "limit": 10_000_000})
    assert seen["limit"] == MAX_LIMIT          # capped, not passed through


def test_search_graph_echoes_the_facets_it_applied(kb):
    """applied_filters is half of the contract's disclosure pair (contract.py's module docstring):
    the static capabilities() lets a harness plan, applied/ignored make it
    impossible to score a silently-dropped facet as a win."""
    from kb_core.domain import NodePatch

    anchor, _reachable, _cid = _routed_corpus(kb)
    kb.update(anchor.id, NodePatch(doc_kind="meeting notes"))
    server = build_server(kb, embedder=lambda q: _vec(9))
    out = _call(server, {"query": "postgres", "judge": "none",
                         "kind": "meeting notes"})
    assert out["applied_filters"] == {"kind": "meeting notes"}
    assert {r["doc_kind"] for r in out["results"]} == {"meeting notes"}


def test_search_graph_seeds_the_crawl_from_a_supplied_anchor_entity(kb):
    """anchor_entity is the crawl-only filter (CRAWL_CAPS adds exactly it) and the
    documented hand-off from find_entity: a minority of golden items resolve an
    entity first, and most of the items concept routing misses are entity-routed."""
    from kb_core.entities import MENTIONS

    anchor, _reachable, _cid = _routed_corpus(kb)
    ent = kb.upsert_entity("Pgvector", categories=["tool"])
    kb.link_entity(anchor.id, ent.node.id, MENTIONS, weight=0.9)
    server = build_server(kb, embedder=lambda q: _vec(9))
    out = _call(server, {"query": "embedding extension broken", "judge": "none",
                         "anchor_entity": str(ent.node.id)})
    assert any(e["via"] == "anchor_entity" and e["id"] == str(ent.node.id)
               for e in out["trace"]["entry"])
    # The contract's disclosure pair (RetrievalStrategy's docstring): every filter the query
    # SETS must appear in applied_filters or ignored_filters. anchor_entity is
    # CRAWL_CAPS' one filter beyond FILTER_NAMES and satisfied neither, on the wire,
    # before this fix -- _jsonable turns the UUID crawl/strategy.py now stores into
    # the same string form every other id on this response already uses.
    assert out["applied_filters"]["anchor_entity"] == str(ent.node.id)


def test_search_graph_hybrid_reports_anchor_entity_as_ignored_not_dropped(kb):
    """server.py builds the Query with anchor_entity set for BOTH strategies
    (search_graph's Query) but HybridStrategy has no crawl and nothing to seed with an
    entity anchor. Before this fix hybrid.py hardcoded ignored_filters=[], so the
    facet vanished on the wire with no disclosure at all -- the same defect the
    anchor_entity fix above closed for cluster-crawl, on the arm that fix didn't
    touch. Disclosure over enforcement (contract.py's module docstring): the call does not raise,
    it reports 'anchor_entity@hybrid' so a caller can tell the anchor did nothing."""
    anchor, _reachable, _cid = _routed_corpus(kb)
    ent = kb.upsert_entity("Pgvector", categories=["tool"])
    server = build_server(kb, embedder=lambda q: _vec(9))
    out = _call(server, {"query": "postgres", "strategy": "hybrid",
                         "anchor_entity": str(ent.node.id)})
    assert out["ignored_filters"] == ["anchor_entity@hybrid"]
    assert "anchor_entity" not in out["applied_filters"]


def test_search_graph_degrades_and_discloses_when_the_embedder_is_down(kb):
    """Existing precedent, search_memories' embedder disclosure. No embedding means no entry step and no
    L_sim; the crawl falls back to mode A's FTS-only path and SAYS SO, rather than
    failing the tool call."""
    _routed_corpus(kb)
    server = build_server(kb, embedder=lambda q: None)
    out = _call(server, {"query": "postgres", "judge": "none"})
    assert out["embedder"] == "unavailable"
    assert out["trace"]["degraded"]["embedder"] == "unavailable"


def test_search_graph_tolerates_no_embedder_configured_at_all(kb):
    """A DIFFERENT production state than the one above: `embedder=lambda q: None`
    is a callable that RUNS and returns None (inference reachable but the call
    failed). `main()` leaves the closed-over `embedder` name ITSELF None when
    KB_INFERENCE_URL is unset -- no callable at all -- and search_graph's
    `vec = embedder(query) if embedder is not None else None` guard exists
    precisely so that state doesn't call None as a function. Every other test in
    this file passes an embedder callable, so nothing here exercised the `embedder
    is None` branch; delete the guard (`vec = embedder(query)` unconditionally) and
    this raises TypeError: 'NoneType' object is not callable."""
    _routed_corpus(kb)
    server = build_server(kb)          # no embedder kwarg at all -- the real default
    out = _call(server, {"query": "postgres", "judge": "none"})
    assert out["embedder"] == "unavailable"
    assert out["trace"]["degraded"]["embedder"] == "unavailable"


def test_search_graph_rows_disclose_an_undated_note_without_raising(kb):
    """`row.event_at.isoformat() if row.event_at else None` guards a real production
    state: in practice many notes are undated. Every note elsewhere in this file goes
    through `_note()`, which dates it via CORPUS_EVENT_AT by design (see that
    helper's docstring) -- so nothing here has exercised the `None` branch. Delete
    the guard (`row.event_at.isoformat()` unconditionally) and this raises
    AttributeError: 'NoneType' object has no attribute 'isoformat'."""
    _routed_corpus(kb)
    undated = kb.upsert(NodeRecord(
        source="agent", external_id="undated", type="note",
        content="postgres undated note, no event_at at all", slug="undated"))
    kb.set_embedding(undated.id, _vec(9), embedded_hash="h-undated")
    server = build_server(kb, embedder=lambda q: _vec(9))
    out = _call(server, {"query": "postgres", "judge": "none"})
    row = next(r for r in out["results"] if r["id"] == str(undated.id))
    assert row["event_at"] is None


def test_search_graph_is_byte_stable_under_the_deterministic_judge(kb):
    """The benchmark protocol: deterministic cells are run twice and the second MUST be
    byte-identical, which is achievable only because nothing orders by weight,
    time or updated_at and every tiebreak ends in a server-side ordinal. If this
    ever fails, the benchmark's deterministic control stops being a control."""
    _routed_corpus(kb)
    server = build_server(kb, embedder=lambda q: _vec(9))
    args = {"query": "embedding extension broken", "judge": "none"}
    first = _call(server, args)
    second = _call(server, args)
    assert [r["id"] for r in first["results"]] == [r["id"] for r in second["results"]]
    assert first["ignored_filters"] == second["ignored_filters"]


def test_search_graph_runs_off_the_event_loop_like_every_other_tool(kb):
    """The off-the-event-loop property, re-asserted for the one tool that actually needs it: a
    multi-second crawl on the loop would stall every other session on this server."""
    _routed_corpus(kb)
    seen = {}

    def embedder(text):
        seen["worker"] = threading.get_ident()
        return _vec(9)

    server = build_server(kb, embedder=embedder)

    async def drive():
        seen["loop"] = threading.get_ident()
        return await server.call_tool(
            "search_graph", {"query": "postgres", "judge": "none"})

    asyncio.run(drive())
    assert seen["worker"] != seen["loop"]


def test_search_graph_rejects_an_unknown_strategy_by_name(kb):
    """registry.get_strategy raises UnknownStrategy naming what IS registered;
    FastMCP surfaces it as a ToolError rather than a 500."""
    from mcp.server.fastmcp.exceptions import ToolError

    _routed_corpus(kb)
    server = build_server(kb, embedder=lambda q: _vec(9))
    with pytest.raises(ToolError, match="unknown strategy 'nope'"):
        _call(server, {"query": "postgres", "strategy": "nope"})
