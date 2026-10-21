"""The three benchmark knobs. Defaults must be behaviour-preserving; each knob's
effect is pinned against the un-knobbed result on the same corpus, so these tests
never assert absolute orders -- only deltas the knob is defined by.

Arrange patterns are mirrored from test_crawl_strategy.py (`_note`/`_vec`/`_seed`),
test_crawl_frontier.py (`_grad_vec` for a graded cosine) and test_crawl_doctrine.py
(`_AdmitAllLLMJudge`-style fakes for the agentic accumulation path). The `kb`,
`make_concept` and `make_entity` fixtures come from tests/conftest.py; there is no
`seeded_corpus` / `corrob_corpus` / `query_vec` fixture in this suite -- each test
builds its own small corpus inline, as every other file in this suite does.
"""
from uuid import UUID

import pytest
from kb_core.domain import NodeRecord

from kb_retrieval import hybrid as hybrid_mod
from kb_retrieval.contract import Query
from kb_retrieval.crawl.frontier import hop2_frontier
from kb_retrieval.crawl.judge import HOP_JUDGES, HopDecision
from kb_retrieval.registry import get_strategy


def _note(kb, ext, content, **kw):
    return kb.upsert(NodeRecord(source="agent", external_id=ext, type="note",
                                content=content, slug=ext, **kw))


def _vec(hot):
    v = [0.0] * 1024
    v[hot] = 1.0
    return v


def _grad_vec(weight):
    """Cosine to _grad_vec(1.0) is strictly increasing in `weight` -- a one-hot
    vector only gives cosine 0 or 1, which cannot express a rank GRADIENT. Mirrors
    test_crawl_frontier.py's helper of the same name."""
    v = [0.0] * 1024
    v[0] = weight
    v[1] = 1.0 - weight
    return v


def _seed(kb, make_concept):
    """A flat hit and a genuinely crawl-only note -- test_crawl_strategy.py's own
    arrange helper, copied rather than imported (test files do not import each
    other's helpers in this suite)."""
    flat = _note(kb, "flat", "postgres tuning notes")
    kb.set_embedding(flat.id, _vec(0), embedded_hash="hf")
    crawl_only = _note(kb, "crawlonly", "unrelated marmalade lorem")
    cid = make_concept("Postgres", [(crawl_only.id, "memory", 1.0)],
                       embedding=_vec(0))
    return flat, crawl_only, cid


def test_unknown_ablate_member_raises(kb):
    with pytest.raises(ValueError, match="ablate"):
        get_strategy("cluster-crawl", kb=kb, judge="none",
                     ablate=frozenset({"l_flat"}))   # l_flat is NOT ablatable


def test_negative_flat_reserve_raises(kb):
    with pytest.raises(ValueError, match="flat_reserve"):
        get_strategy("cluster-crawl", kb=kb, judge="none", flat_reserve=-1)


def test_defaults_are_behaviour_preserving(kb, make_concept):
    _seed(kb, make_concept)
    q = Query(text="postgres", embedding=_vec(0), limit=20)
    base = get_strategy("cluster-crawl", kb=kb, judge="none").search(q)
    knobbed = get_strategy("cluster-crawl", kb=kb, judge="none",
                           flat_reserve=0, bridge_fanout=None,
                           ablate=frozenset()).search(q)
    assert [r.id for r in base.rows] == [r.id for r in knobbed.rows]


def test_flat_reserve_guarantees_flat_top_r(kb):
    """Three flat-only rows whose FTS rank and cosine rank CROSS (X strongest FTS /
    weakest cosine; Y weakest-FTS-of-the-three / strongest cosine; Z in between on
    both), so the crawl's own RRF fusion (L_flat -- mode A's own FTS+KNN fusion --
    plus L_sim, the crawl's independent cosine recompute) lands on a DIFFERENT
    relative order than flat_ids (L_flat) alone. flat_reserve=10 (bigger than the
    3-row pool) reserves the whole pool, so a CORRECT implementation's page is
    identical to the un-knobbed fused order; a "reserved-first" mutation instead
    emits flat_ids' own (unfused) order, which is provably NOT a subsequence of the
    correct fused order -- see the subsequence assertion below."""
    x = _note(kb, "x", "postgres postgres postgres postgres postgres postgres tuning")
    y = _note(kb, "y", "postgres postgres postgres tuning")
    z = _note(kb, "z", "postgres tuning")
    kb.set_embedding(x.id, _grad_vec(0.1), embedded_hash="hx")
    kb.set_embedding(y.id, _grad_vec(0.9), embedded_hash="hy")
    kb.set_embedding(z.id, _grad_vec(0.5), embedded_hash="hz")
    q = Query(text="postgres", embedding=_grad_vec(1.0), limit=20)

    hybrid_top = [r.id for r in get_strategy("hybrid", kb=kb).search(q).rows]
    base = get_strategy("cluster-crawl", kb=kb, judge="none").search(q)
    reserved = get_strategy("cluster-crawl", kb=kb, judge="none",
                            flat_reserve=10).search(q)

    page_ids = {r.id for r in reserved.rows}
    assert set(hybrid_top[:10]) <= page_ids
    assert len(reserved.rows) <= q.limit
    # Reserve SELECTS which ids enter the page; it must never re-rank -- page order
    # stays fused order. Assert page order is a SUBSEQUENCE of the un-knobbed fused
    # order (mutation guard: a "reserved-first" bug fails this).
    base_order = [r.id for r in base.rows]
    reserved_order = [r.id for r in reserved.rows]
    it = iter(base_order)
    assert all(rid in it for rid in reserved_order)


def test_flat_reserve_fills_the_page_when_flat_is_short(kb, make_concept):
    """Regression for the page-cut bug: the page-cut sliced `rest` by
    `q.limit - r` (the REQUESTED reserve, clipped to q.limit) rather than
    `q.limit - len(reserved)` (what was ACTUALLY reserved). Whenever the flat arm
    returns FEWER ids than `r`, the buggy slice under-fills the page by that
    shortfall.

    Two FTS+KNN flat hits ("postgres"-matching, embedded) plus eight crawl-only
    concept members (no "postgres" in their content and unembedded, so they never
    enter the flat arm at all -- only hop 1's concept admission puts them in the
    pool) gives a pool of 10, comfortably over q.limit=6. flat_reserve=10 clips to
    r=min(10, 6)=6, but the flat arm returns only 2 -- exactly the short-page
    condition: the buggy `[:q.limit - r]` = `[:0]` fills nothing beyond the 2 flat
    ids (a 2-row page against a 6-row request); the fix's
    `[:q.limit - len(reserved)]` = `[:4]` fills the rest of the page from the pool
    floor, reaching the full 6.

    This corpus ALSO exercises overflow-displacement (the pool, 10, exceeds
    q.limit, 6 -- unlike test_flat_reserve_guarantees_flat_top_r's 3-row corpus,
    which fits entirely inside its q.limit=20 and so can never displace anything):
    the page must be EXACTLY q.limit long, and its order must still be the
    un-knobbed fused order restricted to whichever ids made the cut -- reserve
    SELECTS, it never re-ranks, even under real truncation pressure.
    """
    flat1 = _note(kb, "flat1", "postgres tuning notes one")
    flat2 = _note(kb, "flat2", "postgres tuning notes two")
    kb.set_embedding(flat1.id, _vec(0), embedded_hash="hf1")
    kb.set_embedding(flat2.id, _vec(0), embedded_hash="hf2")

    members = [_note(kb, f"member{i}", "unrelated marmalade lorem").id
              for i in range(8)]
    make_concept("Postgres", [(m, "memory", 1.0) for m in members],
                embedding=_vec(0))

    q = Query(text="postgres", embedding=_vec(0), limit=6)
    # A big-limit, un-knobbed call reads the FULL fused order this corpus
    # produces -- fused_order itself does not depend on q.limit, only the final
    # page cut does -- as the reference the reserved page's order is checked
    # against (base.rows at q.limit=6 would truncate before every pool id had a
    # chance to appear).
    full = get_strategy("cluster-crawl", kb=kb, judge="none").search(
        Query(text="postgres", embedding=_vec(0), limit=100))
    reserved = get_strategy("cluster-crawl", kb=kb, judge="none",
                            flat_reserve=10).search(q)

    result_ids = [r.id for r in reserved.rows]
    assert len(result_ids) == q.limit
    assert {flat1.id, flat2.id} <= set(result_ids)

    full_order = [r.id for r in full.rows]
    chosen = set(result_ids)
    assert result_ids == [rid for rid in full_order if rid in chosen]


def test_ablate_l_corrob_changes_only_corroboration(kb, make_entity, monkeypatch):
    """L_corrob has no committed end-to-end assertion anywhere else in this suite
    -- this is the first.

    THE COROB CORPUS, why it needs an agentic fake judge rather than judge="none":
    a bridge-admitted row MUST be embedded (bridge_frontier requires
    `nodes.embedding IS NOT NULL`), and knn_candidates has NO similarity floor, so
    the ONLY way to keep it out of the flat pool (and thus reachable by hop 2 at
    all -- a flat row is marked visited before hop 1 even runs) is for it to rank
    outside flat's own KNN cutoff, `pool = max(q.limit, HYBRID_ARM_POOL)`. That
    requires >= `pool` embedded DECOYS ranked better by cosine -- and since
    `pool >= q.limit` always, those decoys alone can already fill the entire page.
    Under judge="none" (L_admit permanently empty), a corroborated-but-ablated row
    has only ONE active list (L_sim, at a rank strictly worse than every decoy);
    a flat decoy always has TWO (L_flat + L_sim) at an adjacent-or-better rank, and
    2/(k+r) > 1/(k+r+1) for every k, r >= 0 -- so the ablated row can NEVER
    outscore even the worst decoy, and the "same ids, different order" property
    this test needs is unwitnessable.
    An AGENTIC hop (uses_llm=True, so hop_agentic=True) gives a bridge-admitted row
    a THIRD list, L_admit, that flat rows structurally cannot reach at all (they
    are never offered to a judge). That extra, corrob-independent list is what
    lets `shared`, once ablated, still land ABOVE the two weakest of six decoys
    instead of collapsing to the very bottom -- the same two decoys are excluded
    from the page in both the base and the ablated run, so the SET is identical
    and only `shared`'s position within it moves. ablate={"l_corrob"} does not
    touch l_admit, so this isolates l_corrob's own contribution.

    Corroboration itself: two entities (`e1`, `e2`) both `mentions`-link the SAME
    memory (`shared`), giving it TWO distinct bridge parents -- frontier.py keeps
    `parents` as a tuple precisely so this is representable (see its docstring).
    No concept is created at all: entry_step's own name-anchor resolution
    (`find_entities`, tier="name") seeds both entities directly as hop 2's bridge
    seeds, without hop 1 ever running (concept_seeds stays empty).
    """
    class _AdmitAllAgentic:
        """Mirrors test_crawl_doctrine.py's `_AdmitAllLLMJudge`: uses_llm=True with
        no `by` set exercises hop_agentic's capability-fallback clause, admitting
        everything offered in frontier order and always stopping."""
        name = "agentic-fake"
        uses_llm = True

        def decide(self, q, frontier, hop, *, admitted=()):
            return HopDecision(admit=list(range(len(frontier))), stop=True,
                               note="admit-all")

    monkeypatch.setitem(HOP_JUDGES, "agentic-fake", _AdmitAllAgentic)
    monkeypatch.setattr(hybrid_mod, "HYBRID_ARM_POOL", 6)

    decoys = []
    for i in range(6):
        n = _note(kb, f"decoy{i}", "unrelated marmalade lorem", title=f"Decoy {i}")
        # Strictly decreasing cosine, all higher than `shared`'s -- consumes all 6
        # of the shrunk flat pool's slots, ahead of `shared`.
        kb.set_embedding(n.id, _grad_vec(0.99 - i * 0.01), embedded_hash=f"hd{i}")
        decoys.append(n)
    shared = _note(kb, "shared", "unrelated damson conserve", title="Shared")
    kb.set_embedding(shared.id, _grad_vec(0.5), embedded_hash="hs")

    # Two entities both mention the SAME memory -- frontier.py's `parents` tuple
    # keeps both, giving `shared` two DISTINCT bridge parents (corrob count 2).
    make_entity("Zeta1", mentions=[shared.id])
    make_entity("Zeta2", mentions=[shared.id])

    q = Query(text="Zeta", embedding=_grad_vec(1.0), limit=6)

    base = get_strategy("cluster-crawl", kb=kb, judge="agentic-fake").search(q)
    ablated = get_strategy("cluster-crawl", kb=kb, judge="agentic-fake",
                           ablate=frozenset({"l_corrob"})).search(q)

    assert shared.id in {r.id for r in base.rows}          # sanity: it was reached
    assert {r.id for r in base.rows} == {r.id for r in ablated.rows}   # ADMIT unchanged
    assert [r.id for r in base.rows] != [r.id for r in ablated.rows]  # RANK moved


def test_ablate_label_cosine_drops_that_entry_family(kb, make_concept):
    """Also covers the review-flagged over-ablation case: a concept picked by
    BOTH the A rung (label-cosine) and the C rung (evidence-vote) must SURVIVE
    ablation under the C rung's own `via`, not vanish wholesale.

    `entry_step`'s `_add` is first-wins on `(kind, id)`, and the A rung runs
    before the C rung -- so a concept that both rungs would independently pick is
    stored under `via="label-cosine"` when both run. The OLD fix filtered the
    returned seeds post-hoc on `via == "label-cosine"`, which removed that
    concept ENTIRELY even though the C rung independently earned it too --
    ablating part of the damped evidence vote, contrary to the code's own
    "keep the damped evidence vote" promise. The fix threads the ablation into
    `entry_step` itself (`skip_label_cosine=True`), so the A rung never runs at
    all and the C rung claims the concept under its own `via` instead.

    `overlap_member` is embedded to match the query AND is a member of
    `overlap_cid`, giving that concept a real evidence-vote score (`vote_rank`);
    `overlap_cid`'s own embedding also matches the query, giving it a real
    label-cosine score (`label_rank`) -- both rungs independently qualify it.
    """
    _seed(kb, make_concept)

    overlap_member = _note(kb, "overlapmember", "unrelated damson conserve")
    kb.set_embedding(overlap_member.id, _vec(0), embedded_hash="hovm")
    overlap_cid = make_concept("Overlap", [(overlap_member.id, "memory", 1.0)],
                               embedding=_vec(0))

    q = Query(text="postgres", embedding=_vec(0), limit=20)

    base = get_strategy("cluster-crawl", kb=kb, judge="none").search(q)
    assert any(e["via"] == "label-cosine" for e in base.trace.entry)   # sanity
    # sanity: the overlap concept is present but claimed by the A rung -- _add is
    # first-wins and the A rung runs first, exactly the pre-fix trap.
    base_overlap = [e for e in base.trace.entry if e["id"] == str(overlap_cid)]
    assert base_overlap and base_overlap[0]["via"] == "label-cosine"

    ablated = get_strategy("cluster-crawl", kb=kb, judge="none",
                           ablate=frozenset({"label-cosine"})).search(q)
    assert all(e["via"] != "label-cosine" for e in ablated.trace.entry)
    # The overlap concept must SURVIVE ablation, now under the C rung's own via
    # -- only the A rung's claim on it is ablated, not the evidence vote itself.
    ablated_overlap = [e for e in ablated.trace.entry if e["id"] == str(overlap_cid)]
    assert ablated_overlap and ablated_overlap[0]["via"] == "evidence-vote"


def test_bridge_fanout_is_threaded(kb, make_entity, monkeypatch):
    """Signature-level spy on kb.bridge_frontier.

    DeterministicHopJudge (judge="none") always stops after hop 1 (strategy.py's
    own module docstring: "the deterministic judge always stops after hop 1, so
    THIS strategy never reaches hop 2 in production") -- so a corpus that reaches
    hop 2 via a hop-1 concept admission (e.g. test_crawl_strategy.py's own
    `_two_hop_corpus`) never actually calls hop2_frontier under judge="none": hop 1
    always sets stop=True first. The reachable path instead SKIPS hop 1 entirely
    (no concept seeded at all, so `concept_seeds` stays empty and hop 1 just
    `continue`s) and reaches hop 2 through an ENTRY-SEEDED entity -- entry_step's
    name-anchor rung resolves an entity directly from q.text, with no concept and
    no judge decision required to get there. hop2_frontier calls
    kb.bridge_frontier() unconditionally once it has a non-empty, not-yet-visited
    entity id list, before any visited-node filtering of what it returns, so the
    spy fires regardless of whether the offered memory ends up empty afterward.
    """
    seen = {}
    real = kb.bridge_frontier

    def spy(*a, **kw):
        seen["fanout"] = kw.get("fanout")
        return real(*a, **kw)

    monkeypatch.setattr(kb, "bridge_frontier", spy)
    m = _note(kb, "m", "unrelated marmalade lorem")
    kb.set_embedding(m.id, _grad_vec(1.0), embedded_hash="hm")
    make_entity("Zeta", mentions=[m.id])
    q = Query(text="Zeta", embedding=_grad_vec(1.0), limit=20)

    get_strategy("cluster-crawl", kb=kb, judge="none", bridge_fanout=16).search(q)
    # This corpus provably reaches hop 2 under judge="none" (the mutation-3 red
    # run proved it: the spy fired and the mutated call's fanout mismatched), so
    # the assertion is unconditional -- a guard here would silently pass if this
    # corpus ever regressed to not reaching hop 2 at all.
    assert seen["fanout"] == 16

    # Direct unit-level call too: pins frontier.py's own signature
    # independently of whichever path the strategy-level corpus above takes.
    seen.clear()
    with kb.read_snapshot() as conn:
        eid = UUID(kb.find_entities("Zeta", conn=conn)[0]["id"])
        hop2_frontier(kb, [eid], q, conn=conn, visited_nodes=set(),
                      visited_pairs=set(), visited_bridges=set(), fanout=16)
    assert seen.get("fanout") == 16
