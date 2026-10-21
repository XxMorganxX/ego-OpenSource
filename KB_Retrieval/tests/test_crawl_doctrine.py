"""The two properties only the ASSEMBLED pipeline can violate.

test_crawl_rank.py proves assemble() cannot be handed a menu score. This file proves
nothing between frontier.py and rank.py smuggles one in anyway -- a unit test on
rank.py cannot see a real wiring bug, and a wiring bug is the likely shape of a
future "simplification" (e.g. sorting admitted rows by menu_score to break ties).
"""

from kb_core.domain import NodeRecord

from kb_retrieval.contract import Query
from kb_retrieval.crawl.judge import HOP_JUDGES, HopDecision
from kb_retrieval.registry import get_strategy


class _AdmitAllLLMJudge:
    """Stands in for the deleted LLMHopJudge, for the ONE property that matters to the
    test below: `uses_llm = True`, with `by` left unset. Configured as a CLASS
    attribute -- the same shape _FakeJudge (test_crawl_strategy.py) and
    DeterministicHopJudge both use, since get_judge() constructs with no arguments.

    Admits everything offered, in frontier-menu order, and always stops after hop 1
    -- deliberately the SAME admission behaviour as DeterministicHopJudge (see its
    docstring). The only thing that differs is `uses_llm`: strategy.py's gate lives
    at the ACCUMULATION site, `if hop_agentic and it.node_id not in admit_order`,
    where `hop_agentic = decision.by == "llm" or (judge.uses_llm and not
    decision.by)`. This judge declares no `by`, so it exercises the fallback half of
    that expression -- capability present, no per-hop tier named -- which is what
    makes its hop agentic. Every judge every OTHER strategy-level test uses
    (DeterministicHopJudge here, _FakeJudge in test_crawl_strategy.py) has
    `uses_llm = False`, so before this test that fallback had never run anywhere in
    the suite."""
    name = "llm-fake"
    uses_llm = True

    def decide(self, q, frontier, hop, *, admitted=()):
        return HopDecision(admit=list(range(len(frontier))), stop=True,
                           note="admit-all llm-fake")


def _note(kb, ext, content, **kw):
    return kb.upsert(NodeRecord(source="agent", external_id=ext, type="note",
                                content=content, slug=ext, **kw))


def _vec(hot):
    v = [0.0] * 1024
    v[hot] = 1.0
    return v


def test_a_maximally_scored_concept_member_does_not_outrank_the_flat_top_hit(
        kb, make_concept):
    """THE DOCTRINE, end to end. `crawl_only` has concept_members.score = 1.0 -- the
    saturated maximum -- and zero query similarity (it is deliberately unembedded, so
    the kNN arm cannot reach it and it really is crawl-only). `flat_top` matches both
    the query text and the query vector.

    Expected arithmetic, after two fixes (the per-hop l_admit gate and the scoreless-
    row omission) that both changed this row's score:
      flat_top   L_flat rank1 (1/61) + L_sim rank1 (1/61) = 0.03279
      crawl_only                                          = 0.0
    `crawl_only` used to read L_sim rank2 (1/62) + L_admit rank1 (1/61) = 0.03252 --
    but BOTH of those contributions were the bugs this suite now guards against.
    L_admit rank1: with DeterministicHopJudge, `admit_order` is hop 1's frontier
    MENU order (concept_frontier's `ORDER BY cm.score DESC, cm.node_id`), so handing
    it to assemble() unconditionally let concept_members.score set an RRF position
    among crawl rows -- forbidden by the design. l_admit is now gated PER HOP, at
    the ACCUMULATION site in strategy.py's hop loop (`if hop_agentic and it.node_id
    not in admit_order`), not at the `assemble()` call -- `l_admit = admit_order`
    there is unconditional. DeterministicHopJudge's hop is never agentic
    (`decision.by` empty and `uses_llm` False both fail `hop_agentic`), so its
    admissions never reach `admit_order` and it contributes nothing here.
    L_sim rank2: crawl_only has NO embedding, so it was scoreless, and
    similarity_rank_list used to tail every scoreless row on sorted by `str(id)`
    rather than omitting it -- an str(id)-ordered position at full RRF weight for a
    row the ruler had nothing to say about. similarity_rank_list now omits scoreless
    ids instead, matching corrob_rank_list's existing reasoning, so crawl_only
    contributes nothing there either. With no bridge parents (it is a concept-family
    admission, not a bridge one), L_corrob has nothing on it. So crawl_only is a
    member of ZERO of the four fusion lists -- which does NOT mean it vanishes:
    assemble()'s `pool` floor (the design's "nothing admitted is ever removed") gives
    every admitted id AT LEAST a 0.0 entry, so it still appears, tied at the bottom,
    ordered only by the already-permitted `path_order` tiebreak. The 1.0 membership
    score plays no part in any of this, at any point -- if a future edit threads
    menu_score OR admit_order into ranking, crawl_only jumps above 0.0 and this test
    fails."""
    flat_top = _note(kb, "flattop", "postgres tuning notes")
    kb.set_embedding(flat_top.id, _vec(0), embedded_hash="hf")
    crawl_only = _note(kb, "crawlonly", "marmalade lorem ipsum")   # NO embedding
    make_concept("Postgres", [(crawl_only.id, "memory", 1.0)], embedding=_vec(0))

    res = get_strategy("cluster-crawl", kb=kb).search(
        Query(text="postgres", embedding=_vec(0)))

    assert [r.id for r in res.rows] == [flat_top.id, crawl_only.id]
    assert round(res.rows[0].rank, 5) == 0.03279
    assert round(res.rows[1].rank, 5) == 0.0
    # ...and it is still RETURNED. Nothing admitted is ever removed -- now via the
    # pool floor rather than (as before this fix) via L_sim's or L_admit's leak.
    assert res.rows[1].via == ["concept"]


def test_permuting_the_frontier_menu_order_does_not_change_crawl_row_order(
        kb, make_concept):
    """Menu-order mutation guard. Two independent pairs of crawl-only rows -- no embedding
    (so no L_sim), text that does not match the query (so no L_flat), concept-family
    only (so no L_corrob) -- so with l_admit correctly gated off, EVERY row in this
    test lands on assemble()'s pool floor at fused score 0.0, and the only thing
    left to decide order is `path_order` (spec-permitted, server-derived).

    Pair A: a1 (created/path FIRST) also has the HIGHER concept_members.score.
    Pair B: b1 (created/path FIRST) has the LOWER concept_members.score -- b2 (path
    SECOND) has the higher one. b1 and b2 are the DIAGNOSTIC pair: hop 1's frontier
    menu is `ORDER BY cm.score DESC`, so b2 -- the higher-scored member -- is
    admitted BEFORE b1. If l_admit still fed assemble() unconditionally (the
    menu-order bug), that earlier admit position would make b2 outrank b1, i.e. permuting
    which row scored higher would permute the final order too. With l_admit
    correctly gated off for DeterministicHopJudge, b1 must win in BOTH pairs --
    path_order, not concept score, decides -- and reverting the gate flips it."""
    a1 = _note(kb, "a1", "unrelated marmalade lorem")     # path/created FIRST
    a2 = _note(kb, "a2", "unrelated fig conserve ipsum")  # path/created SECOND
    b1 = _note(kb, "b1", "unrelated quince paste dolor")  # path/created FIRST
    b2 = _note(kb, "b2", "unrelated damson jam sit amet")  # path/created SECOND
    make_concept("PairA", [(a1.id, "memory", 0.95), (a2.id, "memory", 0.15)],
                embedding=_vec(0))
    make_concept("PairB", [(b2.id, "memory", 0.95), (b1.id, "memory", 0.15)],
                embedding=_vec(0))

    res = get_strategy("cluster-crawl", kb=kb).search(
        Query(text="postgres", embedding=_vec(0)))
    order = [r.id for r in res.rows]

    # Non-diagnostic on its own (score and path agree), kept for a readable pair.
    assert order.index(a1.id) < order.index(a2.id)
    # THE DIAGNOSTIC ASSERTION: b1 has the LOWER concept score but must still win,
    # because concept score (via admit_order's menu position) must not rank anything.
    # Kept ABOVE the floor check below: under the reverted menu-order
    # bug (`l_admit = admit_order` unconditionally) THIS is the assertion that fails
    # -- the headline claim the test is named after -- rather than pytest stopping
    # earlier on the floor check, which would only show "some list changed."
    assert order.index(b1.id) < order.index(b2.id)
    # Confirms WHY path_order was the only thing available to decide the above: every
    # row genuinely landed on assemble()'s pool floor. Placed last on purpose
    # -- under the reverted gate this assertion is the FIRST to fail if
    # it runs first, which demonstrates only "some list changed," not the headline
    # claim ("b1 must win in BOTH pairs").
    assert all(round(r.rank, 5) == 0.0 for r in res.rows)


def test_permuting_the_frontier_menu_order_DOES_change_crawl_row_order_for_an_llm_judge(
        kb, make_concept, monkeypatch):
    """The MIRROR of the menu-order guard above -- and the gap the l_admit gate fix
    would otherwise leave. The gate strategy.py ships today is PER HOP, at the
    ACCUMULATION site inside the hop loop: `if hop_agentic and it.node_id not in
    admit_order`, where `hop_agentic = decision.by == "llm" or (judge.uses_llm and
    not decision.by)`. `l_admit = admit_order` at the `assemble()` call, a few lines
    later, is UNCONDITIONAL -- it only ever reads what the accumulation site already
    decided to keep. The test above only ever exercises the `hop_agentic is False`
    case (capability absent), and so does every other strategy-level test
    (DeterministicHopJudge and test_crawl_strategy.py's `_FakeJudge` both have
    `uses_llm = False`). Deleting `L_admit` outright -- replacing the accumulation
    site's condition with "never append" -- keeps the test above (and the rest of
    the non-doctrine suite) green, because none of them can tell "gated off for THIS
    judge" apart from "gone for every judge". Only a judge whose hop is actually
    agentic can.

    Identical PairA/PairB fixture to the menu-order test -- same unembedded, non-matching-
    text, concept-family-only rows, so l_flat, l_sim and l_corrob are empty for all
    four exactly as before -- run through `_AdmitAllLLMJudge` instead of
    DeterministicHopJudge. `_AdmitAllLLMJudge` declares `uses_llm = True` and leaves
    `by` unset, so hop 1 IS agentic (the fallback half of `hop_agentic`), and its
    admissions get appended into `admit_order` at the accumulation site. From there,
    `l_admit = admit_order` at the `assemble()` call is the exact same unconditional
    line the menu-order test above runs through too -- the two tests differ only in what the
    accumulation site put INTO `admit_order` before that line ever executes, never
    in the call site itself. So every id's fused score here comes ONLY from its
    position in admit_order, i.e. from hop 1's frontier-menu order (`ORDER BY
    cm.score DESC`): b2, the higher-scored member, is admitted before b1, so b2 now
    gets the better RRF position -- flipping b1/b2's order relative to the menu-order test
    above, where the identical fixture makes b1 win under a non-LLM judge.

    Three tests pin this gate, not two -- this one, the menu-order test above, and
    `test_a_fallen_back_llm_hop_does_not_let_a_menu_score_rank_anything` below (a
    judge with `uses_llm = True` whose hop still must NOT be agentic, because it fell
    back). All three must pass together: replacing `hop_agentic` at the accumulation
    site with plain `judge.uses_llm` passes this test and the menu-order test above but fails
    the fallback one -- it cannot tell "capability exercised" from "capability
    present, hop fell back", exactly the menu-order bug re-entering through the quota path;
    never appending (or deleting `L_admit`) fails THIS test, because b2 would then
    have no way to outrank b1. DO NOT "fix" either failure by moving a capability
    check back to the `assemble()` call, e.g. `l_admit = admit_order if
    judge.uses_llm else []` -- `l_admit = admit_order`, UNCONDITIONAL, at that call
    site (in ClusterCrawlStrategy.search) IS the shipped, correct code, not a reverted bug waiting
    to be restored. The gate lives entirely at the accumulation site, one screen up;
    the call site has nothing left to gate."""
    monkeypatch.setitem(HOP_JUDGES, "llm-fake", _AdmitAllLLMJudge)

    a1 = _note(kb, "a1", "unrelated marmalade lorem")     # path/created FIRST
    a2 = _note(kb, "a2", "unrelated fig conserve ipsum")  # path/created SECOND
    b1 = _note(kb, "b1", "unrelated quince paste dolor")  # path/created FIRST
    b2 = _note(kb, "b2", "unrelated damson jam sit amet")  # path/created SECOND
    make_concept("PairA", [(a1.id, "memory", 0.95), (a2.id, "memory", 0.15)],
                embedding=_vec(0))
    make_concept("PairB", [(b2.id, "memory", 0.95), (b1.id, "memory", 0.15)],
                embedding=_vec(0))

    res = get_strategy("cluster-crawl", kb=kb, judge="llm-fake").search(
        Query(text="postgres", embedding=_vec(0)))
    order = [r.id for r in res.rows]

    # THE FLIP: b2 -- admitted BEFORE b1 because it has the higher concept score --
    # now outranks it. The menu-order test above proves the opposite outcome from the SAME
    # fixture under a non-LLM judge.
    assert order.index(b2.id) < order.index(b1.id)
    # And it is a REAL score now, not the pool floor -- confirming l_admit actually
    # fed the fusion rather than something else (e.g. tiebreak order) moving it.
    assert any(round(r.rank, 5) != 0.0 for r in res.rows)


def test_the_same_query_three_times_returns_the_same_order(kb, make_concept):
    """The design: the deterministic control arm must be byte-reproducible across
    repeats, or the benchmark cannot separate a real effect from run-to-run noise.
    The corpus is built with TIED cosines on purpose -- that is where an unstable
    order comes from, and where the draft's no-op tiebreak would have bitten, since
    strategy.py feeds similarity_rank_list a Python set.

    Asserted on the id ORDER, not on the rank floats: record_access is a write on the
    read path (it feeds the importance/recency multiplier the FTS arm orders by), so
    byte-identical SCORES across back-to-back calls need the access snapshot/restore
    dance test_equivalence.py does. Order is what reproducibility means here."""
    members = []
    for i in range(6):
        n = _note(kb, f"tied{i}", "postgres tuning notes")
        kb.set_embedding(n.id, _vec(0), embedded_hash=f"h{i}")   # identical vectors
        members.append((n.id, "memory", 0.9))                    # identical scores
    make_concept("Postgres", members, embedding=_vec(0))

    s = get_strategy("cluster-crawl", kb=kb)
    q = Query(text="postgres", embedding=_vec(0))
    first = [r.id for r in s.search(q).rows]
    assert len(first) == 6
    for _ in range(2):
        assert [r.id for r in s.search(q).rows] == first


class _FellBackLLMJudge:
    """An LLM-capable judge whose hop FELL BACK -- exactly what LLMHopJudge returns
    after a quota wall, a parse failure, an open circuit or the deadline pre-check.
    uses_llm is True (the capability is real, the model is just unreachable), and the
    admissions are the DETERMINISTIC rule's output, so admit_order is once again hop
    1's frontier MENU order."""
    name = "llm-fellback"
    uses_llm = True

    def decide(self, q, frontier, hop, *, admitted=()):
        return HopDecision(admit=list(range(len(frontier))), stop=True,
                           note="llm fell back: unavailable (quota)",
                           by="deterministic", llm_status="unavailable (quota)",
                           degraded_key="llm_quota")


def test_a_fallen_back_llm_hop_does_not_let_a_menu_score_rank_anything(
        kb, make_concept, monkeypatch):
    """THE DOCTRINE, on the fail-soft path -- the gap the capability gate leaves.

    `l_admit = admit_order if self._judge.uses_llm else []` is right for a judge that
    always calls out and WRONG for one that fell back: uses_llm is still True, but
    admit_order is now hop 1's frontier menu order (concept_frontier's `ORDER BY
    cm.score DESC, cm.node_id`), so feeding it to assemble() lets
    concept_members.score set RRF positions -- the menu-order bug, re-entering through the
    quota path, on the exact code path a quota wall makes the COMMON one.

    Same PairA/PairB fixture as the two tests above, so the three form a set:
      - DeterministicHopJudge  -> b1 wins (capability absent)
      - _AdmitAllLLMJudge      -> b2 wins (capability present AND exercised)
      - _FellBackLLMJudge      -> b1 wins (capability present, NOT exercised)
    Only a gate on the hop's ACTUAL tier passes all three. Gating on uses_llm alone
    fails this one; deleting L_admit fails the middle one."""
    monkeypatch.setitem(HOP_JUDGES, "llm-fellback", _FellBackLLMJudge)

    a1 = _note(kb, "a1", "unrelated marmalade lorem")     # path/created FIRST
    a2 = _note(kb, "a2", "unrelated fig conserve ipsum")  # path/created SECOND
    b1 = _note(kb, "b1", "unrelated quince paste dolor")  # path/created FIRST
    b2 = _note(kb, "b2", "unrelated damson jam sit amet")  # path/created SECOND
    make_concept("PairA", [(a1.id, "memory", 0.95), (a2.id, "memory", 0.15)],
                embedding=_vec(0))
    make_concept("PairB", [(b2.id, "memory", 0.95), (b1.id, "memory", 0.15)],
                embedding=_vec(0))

    res = get_strategy("cluster-crawl", kb=kb, judge="llm-fellback").search(
        Query(text="postgres", embedding=_vec(0)))
    order = [r.id for r in res.rows]

    assert order.index(b1.id) < order.index(b2.id)
    assert all(round(r.rank, 5) == 0.0 for r in res.rows)
    assert res.trace.crawl_mode == "deterministic"
    assert res.cost.llm_calls == 0
