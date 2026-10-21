"""ClusterCrawlStrategy end to end -- deterministic, no LLM, no network.

Several tests swap in a fake judge via HOP_JUDGES. That is not a testing hack: it is
the same seam the deleted LLMHopJudge used, and exercising it is how "the same pipeline
with one function swapped" stops being a claim.
"""

from kb_core.domain import NodeRecord

from kb_retrieval.contract import CRAWL_DETERMINISTIC_CAPS, FILTER_NAMES, Query
from kb_retrieval.crawl.judge import HOP_JUDGES, HopDecision
from kb_retrieval.crawl.strategy import ClusterCrawlStrategy
from kb_retrieval.registry import get_strategy


def _note(kb, ext, content, **kw):
    return kb.upsert(NodeRecord(source="agent", external_id=ext, type="note",
                                content=content, slug=ext, **kw))


def _vec(hot):
    v = [0.0] * 1024
    v[hot] = 1.0
    return v


class _FakeJudge:
    """Configured through CLASS attributes because get_judge() constructs with no
    arguments -- the same shape the deleted LLMHopJudge had."""
    name = "fake"
    uses_llm = False
    admit_indices = None     # None -> admit everything offered
    stop = False
    seen: list = []          # [(hop, [node_id, ...])] -- what was OFFERED

    def decide(self, q, frontier, hop, *, admitted=()):
        type(self).seen.append((hop, [it.node_id for it in frontier]))
        idx = (list(range(len(frontier))) if self.admit_indices is None
               else self.admit_indices)
        return HopDecision(admit=idx, stop=self.stop, note="fake")


def _install_fake(monkeypatch, *, admit_indices=None, stop=False):
    monkeypatch.setitem(HOP_JUDGES, "fake", _FakeJudge)
    monkeypatch.setattr(_FakeJudge, "seen", [])
    monkeypatch.setattr(_FakeJudge, "admit_indices", admit_indices)
    monkeypatch.setattr(_FakeJudge, "stop", stop)


class _ScriptedLLMJudge:
    """A uses_llm=True registry fake with SCRIPTED per-hop HopDecisions -- no real
    LLM machinery, no client, no subprocess. Configured through CLASS attributes
    because get_judge() constructs with no arguments, the same shape _FakeJudge
    above and the deleted real LLMHopJudge both have.

    `script` is a list of HopDecision keyword-dicts, one per hop (1-indexed by
    `hop`). `admit: None` means "admit everything offered" (mirrors
    `_FakeJudge.admit_indices is None`) rather than hardcoding indices that would
    silently go stale if a frontier's size changed.
    """
    name = "llm-fake"
    uses_llm = True
    script: list = []
    calls: list = []          # [(hop, admitted), ...] -- the digest kwarg RECEIVED

    def decide(self, q, frontier, hop, *, admitted=()):
        type(self).calls.append((hop, admitted))
        kw = dict(self.script[hop - 1])
        if kw.get("admit") is None:
            kw["admit"] = list(range(len(frontier)))
        return HopDecision(**kw)


def _install_scripted_llm(monkeypatch, script):
    monkeypatch.setitem(HOP_JUDGES, "llm-fake", _ScriptedLLMJudge)
    monkeypatch.setattr(_ScriptedLLMJudge, "calls", [])
    monkeypatch.setattr(_ScriptedLLMJudge, "script", script)


def _seed(kb, make_concept):
    """A flat hit and a genuinely crawl-only note.

    crawl_only is left UNEMBEDDED on purpose. knn_candidates has no similarity floor
    -- deliberately, it is a measured negative -- so ANY embedded note lands in the
    flat pool no matter how distant, and a "crawl-only" note with a vector would not
    be crawl-only at all. Without a vector it can be reached only through concept
    membership, which is exactly the arm under test."""
    flat = _note(kb, "flat", "postgres tuning notes")
    kb.set_embedding(flat.id, _vec(0), embedded_hash="hf")
    crawl_only = _note(kb, "crawlonly", "unrelated marmalade lorem")
    cid = make_concept("Postgres", [(crawl_only.id, "memory", 1.0)],
                       embedding=_vec(0))
    return flat, crawl_only, cid


def test_cluster_crawl_is_registered_and_names_itself_on_the_class(kb):
    """register() inspects the CLASS object, so `name` must be a class attribute --
    setting self.name in __init__ is rejected at registration time."""
    assert ClusterCrawlStrategy.name == "cluster-crawl"
    assert get_strategy("cluster-crawl", kb=kb).name == "cluster-crawl"


def test_capabilities_report_the_deterministic_path_as_cheap_and_llm_free(kb):
    s = get_strategy("cluster-crawl", kb=kb)
    assert s.capabilities() == CRAWL_DETERMINISTIC_CAPS
    assert s.capabilities().filters == FILTER_NAMES | {"anchor_entity"}
    assert s.capabilities().uses_llm is False


def test_llm_calls_is_always_zero(kb, make_concept):
    """The machine-checkable "this was not an agentic run" flag, and the
    property that makes this whole plan CI-testable."""
    _seed(kb, make_concept)
    res = get_strategy("cluster-crawl", kb=kb).search(
        Query(text="postgres", embedding=_vec(0)))
    assert res.cost.llm_calls == 0
    assert res.cost.tokens_returned > 0
    assert res.trace.crawl_mode == "deterministic"


def test_include_dead_true_discloses_the_concept_arm_blind_spot(kb, make_concept):
    """Consolidation builds its graph dead-free, so include_dead=True cannot do what
    the caller asked on the concept arm -- the flag can only fail to REMOVE rows,
    never ADD the ones consolidation never wrote. Measured cost: 9 of 57 must-gold
    are dead and unreachable that way. The flat arm still reaches them."""
    _seed(kb, make_concept)
    s = get_strategy("cluster-crawl", kb=kb)
    on = s.search(Query(text="postgres", embedding=_vec(0), include_dead=True))
    off = s.search(Query(text="postgres", embedding=_vec(0), include_dead=False))
    assert on.ignored_filters == ["include_dead@concept-arm"]
    assert off.ignored_filters == []
    assert off.applied_filters == {"include_dead": False}


def test_anchor_entity_is_echoed_in_applied_filters(kb, make_entity):
    """CRAWL_CAPS.filters is FILTER_NAMES | {"anchor_entity"} (contract.CRAWL_CAPS), and
    the contract (RetrievalStrategy's docstring) requires every filter the query SETS to
    appear in applied_filters or ignored_filters. anchor_entity satisfied neither:
    `applied` was built from FILTER_FIELDS, which deliberately excludes it (that
    tuple is shared with HybridStrategy, which has no such field at all), and no
    "anchor_entity@..." was ever added to `ignored`. It IS genuinely honoured --
    entry_step seeds the crawl from it unconditionally (crawl.entry.entry_step) -- so
    the fix is disclosure, not behaviour: echo it in applied_filters like every
    other filter the query sets."""
    ent_id = make_entity("Pgvector")
    s = get_strategy("cluster-crawl", kb=kb)
    res = s.search(Query(text="postgres", anchor_entity=ent_id))
    assert res.applied_filters["anchor_entity"] == ent_id
    unset = s.search(Query(text="postgres"))
    assert "anchor_entity" not in unset.applied_filters


def test_nothing_admitted_is_ever_removed_from_the_pool(kb, make_concept):
    """The design's honest floor #1: the pool is always flat u crawl, so pool recall is
    >= max(flat, crawl) by construction. flat_reserve is NOT how that is achieved --
    reserving half the slots would bound crawl@20 below by flat@10 and sell a
    handicap as a floor."""
    flat, crawl_only, _cid = _seed(kb, make_concept)
    res = get_strategy("cluster-crawl", kb=kb).search(
        Query(text="postgres", embedding=_vec(0), limit=20))
    ids = {r.id for r in res.rows}
    assert flat.id in ids
    assert crawl_only.id in ids


def test_a_crawl_only_row_is_tagged_by_the_arm_that_reached_it(kb, make_concept):
    """`via` is per-row provenance, not a global mode flag: a row can be reached by
    the flat arm, the concept arm, or both, and the benchmark reads it to attribute
    displacement."""
    flat, crawl_only, _cid = _seed(kb, make_concept)
    res = get_strategy("cluster-crawl", kb=kb).search(
        Query(text="postgres", embedding=_vec(0)))
    by_id = {r.id: r for r in res.rows}
    assert by_id[flat.id].via == ["flat"]
    assert by_id[crawl_only.id].via == ["concept"]


def test_no_entry_route_degenerates_to_the_flat_pool_and_says_so(kb):
    """The design: if every family is empty the cell still fills, with the hybrid rows
    verbatim -- disclosed, not silently identical."""
    flat = _note(kb, "flat", "postgres tuning notes")
    kb.set_embedding(flat.id, _vec(0), embedded_hash="hf")
    res = get_strategy("cluster-crawl", kb=kb).search(
        Query(text="postgres", embedding=_vec(0)))
    assert res.trace.entry == []
    assert "entry" in res.trace.degraded
    assert [r.id for r in res.rows] == [flat.id]
    assert res.trace.stop_reason is None      # no hop ran at all


def test_no_embedding_reports_the_embedder_degraded(kb, make_concept):
    """Existing precedent (KB_MCP's search_memories). With no vector there is no entry
    step and no L_sim, so the crawl degenerates to the flat pool -- which is the
    point of hop 0 being non-negotiable rather than a nicety."""
    _seed(kb, make_concept)
    res = get_strategy("cluster-crawl", kb=kb).search(Query(text="postgres"))
    assert res.trace.degraded["embedder"] == "unavailable"
    assert all(r.similarity is None for r in res.rows)
    assert res.rows


def test_no_embedding_mode_b_order_matches_mode_a_fts_only_order(kb, make_concept):
    """The design's degradation rule: "Embedder down | No entry step, no L_sim;
    degrade to mode A's FTS-only path." With q.embedding is None, strategy.py used
    to set every pool row's similarity to None and hand the WHOLE pool to
    similarity_rank_list, which tailed every one of them on sorted by `str(ident)`
    -- a second, non-trivial active rank list (UUID order) fused against L_flat's
    real FTS relevance order, which is how mode B's order came to diverge from mode
    A's on a 20-row pool with FTS #3 landing at position 11. With no concept/entity
    seed possible without an embedding either (entry_step's concept_candidates and
    find_entities' embedding-routed rungs both need one), hop 1 never runs, so the
    crawl's only admitted rows here are the flat pool's -- meaning L_flat is the
    SAME list HybridStrategy.flat_pool() itself produces, and if L_sim is correctly
    EMPTY (all scoreless, all omitted), RRF over a single list cannot reorder it.
    Reverting the omission-not-tail fix in similarity_rank_list should reintroduce
    the divergence -- but only if FTS relevance is genuinely DIFFERENTIATED across
    rows: fts_candidates' own SQL breaks a RELEVANCE TIE on `id` too (`ORDER BY
    score DESC, id`), so tied content would make mode A's order equal id order on
    its own, masking the bug. Repeating "postgres" a DIFFERENT number of times per
    note forces real ts_rank separation, so mode A's true order tracks relevance,
    not id -- letting a UUID-ordered L_sim actually shuffle mode B away from it."""
    notes = [_note(kb, f"pg{i}", " ".join(["postgres"] * (8 - i)) + " tuning notes")
            for i in range(8)]
    q = Query(text="postgres")   # no embedding at all -- embedder down

    mode_a = get_strategy("hybrid", kb=kb).search(q)
    mode_b = get_strategy("cluster-crawl", kb=kb).search(q)

    assert len(mode_a.rows) == len(notes)
    # Pin the discriminator, the way test_crawl_frontier.py's similarity test already
    # does with `assert expected[hi.id] != expected[lo.id]`. Without this, a corpus where
    # the repeat-count fixture above ever failed to produce real ts_rank separation
    # would make mode A's order collapse onto fts_candidates' own `ORDER BY score
    # DESC, id` tiebreak -- i.e. onto id order -- and the final assertion below would
    # then pass for the WRONG reason (both modes agreeing on id order, not on
    # relevance order), silently stop detecting the bug this test exists for. That is
    # exactly the trap the implementer fell into on the first attempt at this
    # fixture.
    ranks = [r.rank for r in mode_a.rows]
    assert len(set(ranks)) == len(ranks)          # genuinely differentiated ts_rank
    ids = [r.id for r in mode_a.rows]
    assert ids != sorted(ids, key=str)             # relevance-driven, not id-driven
    assert [r.id for r in mode_b.rows] == [r.id for r in mode_a.rows]


def test_an_empty_result_is_not_an_error(kb):
    res = get_strategy("cluster-crawl", kb=kb).search(
        Query(text="nothingmatchesthisatall", embedding=_vec(0)))
    assert res.rows == []
    assert res.confidence["level"] == "none"
    assert res.cost.llm_calls == 0


def test_out_of_range_admissions_are_dropped_and_counted(kb, make_concept,
                                                         monkeypatch):
    """A judge cannot emit a node id, so an out-of-range index is the ONLY shape a
    malformed or hostile admission can take. Dropping it silently would hide exactly
    the signal an LLM judge needs monitored."""
    _seed(kb, make_concept)
    _install_fake(monkeypatch, admit_indices=[0, 999, -5], stop=True)
    res = get_strategy("cluster-crawl", kb=kb, judge="fake").search(
        Query(text="postgres", embedding=_vec(0)))
    assert res.trace.rejected_admissions == 2
    assert res.trace.hops[0]["admitted"] == 1
    assert res.trace.stop_reason == "judge-stop"


def test_marginal_yield_ends_a_crawl_the_judge_did_not_stop(kb, make_concept,
                                                            monkeypatch):
    """Stop #3: a hop admitting fewer than 3 rows not already in the pool
    ends the crawl. A convergence test on SET GROWTH, not a similarity threshold on
    rows. Unreachable with the deterministic judge (it always stops first), which is
    why it needs a judge that returns stop=False -- and why stop_reason had to exist
    for it to be observable at all."""
    _seed(kb, make_concept)
    _install_fake(monkeypatch, stop=False)
    res = get_strategy("cluster-crawl", kb=kb, judge="fake").search(
        Query(text="postgres", embedding=_vec(0)))
    assert res.trace.stop_reason == "marginal-yield"
    assert res.trace.hops[0]["by"] == "fake"


def test_swapping_the_judge_changes_admission_not_what_was_offered(kb, make_concept,
                                                                   monkeypatch):
    """"The same pipeline with decide() replaced by a rule", made
    checkable: two judges that admit different subsets must still be OFFERED the
    identical hop-1 frontier."""
    _seed(kb, make_concept)
    _install_fake(monkeypatch, admit_indices=[], stop=True)
    a = get_strategy("cluster-crawl", kb=kb, judge="fake").search(
        Query(text="postgres", embedding=_vec(0)))
    offered_a = list(_FakeJudge.seen)

    _install_fake(monkeypatch, admit_indices=None, stop=True)
    b = get_strategy("cluster-crawl", kb=kb, judge="fake").search(
        Query(text="postgres", embedding=_vec(0)))
    offered_b = list(_FakeJudge.seen)

    assert offered_a == offered_b            # identical menus
    assert a.trace.hops[0]["admitted"] == 0
    assert b.trace.hops[0]["admitted"] >= 1  # different admissions
    assert len(b.rows) > len(a.rows)


def test_a_flat_only_row_still_carries_a_similarity(kb, make_concept):
    """pool_ids is seeded from the flat pool (hop 0) precisely so a flat-only row
    still reaches similarity_for. assemble() would still admit that row via l_flat
    even without the seed -- l_flat is passed straight through to assemble(),
    independent of pool_ids -- so results look superficially intact if the seed is
    dropped. What actually breaks is silent: the row is excluded from `pool`, so
    similarity_for never scores it and Row.similarity comes back None."""
    flat, crawl_only, _cid = _seed(kb, make_concept)
    res = get_strategy("cluster-crawl", kb=kb).search(
        Query(text="postgres", embedding=_vec(0)))
    by_id = {r.id: r for r in res.rows}
    assert by_id[flat.id].via == ["flat"]
    assert by_id[flat.id].similarity is not None


from kb_retrieval.crawl import strategy as strategy_mod            # noqa: E402
from kb_retrieval import hybrid as hybrid_mod                      # noqa: E402


def _two_hop_corpus(kb, make_concept, make_entity, monkeypatch):
    """A corpus where hop 2 is actually REACHABLE end to end.

    VERIFIED BY PRE-FLIGHT REVIEW -- but confirm it cheaply before
    building on it. This fixture was the plan's flagged highest-risk artefact; an
    independent executability review traced it and found the arithmetic SOUND:
    `flat_pool` computes `pool = max(q.limit, HYBRID_ARM_POOL)` = max(1, 1) = 1;
    `knn_candidates` ends `ORDER BY ordinal LIMIT %(pool)s`, so exactly ONE row comes
    back however the two-leg ANN shape behaves internally; the other three embedded
    notes therefore stay out of visited_nodes and are bridge-reachable on hop 2; and
    the FTS arm contributes nothing because "postgres" matches none of these bodies
    and a ONE-WORD query skips the OR-fallback (which needs `1 < len(words)`). All
    three dependent tests' hop arithmetic checks out.

    That is a careful reading, not an execution. test_stop_false_never_buys_a_third_hop
    -- the only test below still built on this fixture; its two former co-dependents,
    the mixed-mode and hop-2-digest tests, were LLM-tier tests deleted with that tier --
    fails on `len(res.trace.hops) == 2` if this arithmetic is wrong, so the cheap
    confirmation is worth 30 seconds: run it and print `res.trace.hops` /
    `res.trace.stop_reason`. IF IT IS WRONG, THE FIX IS TO RAISE THE NOTE COUNT, not
    to change the strategy.

    The obstacle is structural, not incidental: knn_candidates has no similarity
    floor (a measured negative), so EVERY embedded note lands in the flat pool, and
    strategy.py seeds visited_nodes with the flat pool -- so in a small synthetic
    corpus every bridge target is already visited and hop 2's frontier is always
    empty. Shrinking HYBRID_ARM_POOL to 1 (with limit=1, since flat_pool takes
    `max(q.limit, HYBRID_ARM_POOL)`) leaves exactly one embedded note in the flat
    pool and makes the other three bridge-reachable. That is a fixture device, not a
    behaviour change: nothing about the hop loop depends on the pool's size.

    Hop 1 offers four UNEMBEDDED concept members (unembedded so they are not in the
    flat pool at all) and harvests the entity member as a bridge seed. Hop 2 offers
    the entity's three remaining embedded mentions. Both hops clear
    MARGINAL_YIELD_FLOOR, so only the budget rules can end this crawl.
    """
    monkeypatch.setattr(hybrid_mod, "HYBRID_ARM_POOL", 1)
    members = []
    for i in range(4):
        n = _note(kb, f"cm{i}", "unrelated marmalade lorem", title=f"Member {i}")
        members.append((n.id, "memory", 0.9 - i * 0.05))
    bridged = []
    for i in range(4):
        n = _note(kb, f"br{i}", "unrelated damson conserve", title=f"Bridged {i}")
        kb.set_embedding(n.id, _vec(0), embedded_hash=f"hb{i}")
        bridged.append(n.id)
    eid = make_entity("Keystone", mentions=bridged)
    make_concept("Keystone package", members + [(eid, "entity", 0.8)],
                 embedding=_vec(0))
    return Query(text="postgres", embedding=_vec(0), limit=1)


def test_the_deterministic_crawl_reports_no_provider_cost_at_all(kb, make_concept):
    """None, not 0: the design puts provider_* in the agentic cells only, and a reported
    0 would be averaged into the benchmark's cost column as a measured zero.
    llm_calls stays a hard 0 -- the flag the whole 2x2 leans on."""
    _seed(kb, make_concept)
    res = get_strategy("cluster-crawl", kb=kb).search(
        Query(text="postgres", embedding=_vec(0)))
    assert res.cost.llm_calls == 0
    assert res.cost.provider_tokens is None
    assert res.cost.provider_cost_usd is None
    assert res.trace.llm is None                    # no LLM tier: nothing to report


# ---- agentic disclosure, restored via registry fake ------------------------------
#
# The real LLMHopJudge (+prompts.py, +breaker.py) was deleted,
# and 11 of its own tests went with it -- necessarily, since
# they constructed the crawl against the real HOP_JUDGES registry with `judge="llm"`,
# which no longer resolves. That left six invariants unguarded: crawl_mode's
# "agentic" and "mixed" branches, trace.llm's "ok" and degradation-string branches,
# cost.llm_calls/provider_tokens accumulating only the agentic hop (not doubled by a
# fallback hop), and the admitted-titles digest actually reaching hop 2. The two
# tests below restore them with a uses_llm=True registry FAKE -- no client, no
# subprocess, no real inference -- exactly the seam _FakeJudge above already
# exercises for the deterministic side, and the same discipline
# test_crawl_doctrine.py uses for its own fakes.


def test_a_scripted_llm_hop_reports_agentic_mode_and_disclosure(kb, make_concept,
                                                                 monkeypatch):
    """A single hop whose decision carries by="llm" must flip crawl_mode to
    "agentic", trace.llm to "ok", and accumulate cost.llm_calls / provider_tokens --
    all previously proven only against the real (now-deleted) LLMHopJudge."""
    _seed(kb, make_concept)
    _install_scripted_llm(monkeypatch, [
        dict(admit=None, stop=True, by="llm", llm_calls=1,
             provider_tokens=185, provider_cost_usd=0.000603, llm_status="ok"),
    ])
    res = get_strategy("cluster-crawl", kb=kb, judge="llm-fake").search(
        Query(text="postgres", embedding=_vec(0)))

    assert res.trace.crawl_mode == "agentic"
    assert res.trace.llm == "ok"
    assert res.cost.llm_calls == 1
    assert res.cost.provider_tokens == 185
    assert res.cost.provider_cost_usd == 0.000603
    assert res.trace.hops[0]["agentic"] is True
    assert res.trace.hops[0]["by"] == "llm"          # decision.by, not the judge's
                                                      # registry name ("llm-fake")


def test_a_mixed_crawl_reports_the_fallback_and_does_not_double_count(
        kb, make_concept, make_entity, monkeypatch):
    """Two hops, the second one falls back deterministic: crawl_mode must read
    "mixed" (neither "agentic" nor "deterministic"), trace.llm must carry hop 2's
    degradation string -- first-degradation-wins, the `llm_status != "ok"` branch --
    trace.degraded must carry the degraded_key entry, and cost.llm_calls /
    provider_tokens must accumulate ONLY the agentic hop: hop 2's fallback decision
    reports llm_calls=0 and no provider_tokens, so a naive accumulation that didn't
    gate on the decision would double- or half-count rather than land on hop 1's
    185 alone. hops[].agentic must discriminate the two hops. Also proves the
    admitted-titles digest thread: hop 2 must RECEIVE hop 1's admitted titles.

    DEPENDS ON _two_hop_corpus -- verified by pre-flight review; see its
    docstring for the reasoning and the cheap confirmation."""
    q = _two_hop_corpus(kb, make_concept, make_entity, monkeypatch)
    _install_scripted_llm(monkeypatch, [
        dict(admit=None, stop=False, by="llm", llm_calls=1,
             provider_tokens=185, provider_cost_usd=0.000603, llm_status="ok"),
        dict(admit=None, stop=True, by="deterministic", llm_calls=0,
             llm_status="unavailable (hourly budget 20/h exhausted)",
             degraded_key="llm_budget"),
    ])
    res = get_strategy("cluster-crawl", kb=kb, judge="llm-fake").search(q)

    assert res.trace.crawl_mode == "mixed"
    assert res.trace.llm == "unavailable (hourly budget 20/h exhausted)"
    assert (res.trace.degraded["llm_budget"]
            == "unavailable (hourly budget 20/h exhausted)")
    assert res.cost.llm_calls == 1                  # hop 2 added nothing
    assert res.cost.provider_tokens == 185          # NOT doubled, NOT halved
    assert res.trace.hops[0]["agentic"] is True
    assert res.trace.hops[1]["agentic"] is False
    assert res.trace.hops[0]["by"] == "llm"
    assert res.trace.hops[1]["by"] == "deterministic"

    assert _ScriptedLLMJudge.calls[0] == (1, ())
    hop2_hop, hop2_admitted = _ScriptedLLMJudge.calls[1]
    assert hop2_hop == 2
    assert hop2_admitted == ("Member 0", "Member 1", "Member 2", "Member 3")


def test_the_call_budget_precheck_skips_the_judge_and_builds_the_fallback_itself(
        kb, make_concept, make_entity, monkeypatch):
    """Stop #1, the call-budget pre-check: "checked BEFORE the call, never
    after -- a check that spends the call's latency and then discards the answer is
    not a deadline, it is an audit" (strategy.py's own comment). Same house pattern
    as test_the_crawl_deadline_stops_mid_loop_before_any_hop_runs: monkeypatch the
    constant down to a value hop 2 can actually reach rather than trusting an
    unreachable branch (MAX_LLM_CALLS == MAX_HOPS == 2 today).

    Hop 1 is agentic and asks to keep going (stop=False); by the time hop 2 starts,
    llm_calls (1) already meets the patched MAX_LLM_CALLS (1), so hop 2 must be
    built from `_DETERMINISTIC`'s own decision without ever asking the scripted
    judge -- the load-bearing assertion is `_ScriptedLLMJudge.calls`' length staying
    1, not 2: a pre-check that called the judge and discarded the answer would still
    pass every OTHER assertion here.

    DEPENDS ON _two_hop_corpus -- verified by pre-flight review; see its
    docstring for the reasoning and the cheap confirmation."""
    q = _two_hop_corpus(kb, make_concept, make_entity, monkeypatch)
    monkeypatch.setattr(strategy_mod, "MAX_LLM_CALLS", 1)
    _install_scripted_llm(monkeypatch, [
        dict(admit=None, stop=False, by="llm", llm_calls=1,
             provider_tokens=185, provider_cost_usd=0.000603, llm_status="ok"),
    ])
    res = get_strategy("cluster-crawl", kb=kb, judge="llm-fake").search(q)

    assert res.trace.hops[1]["by"] == "deterministic"
    assert res.trace.hops[1]["reason"] == "llm_calls"
    assert res.trace.hops[1]["agentic"] is False
    assert len(_ScriptedLLMJudge.calls) == 1     # the pre-check SKIPPED the judge


def test_stop_false_never_buys_a_third_hop(kb, make_concept, make_entity,
                                           monkeypatch):
    """ASYMMETRIC JUDGE AUTHORITY, the half nothing pinned before: a judge
    may only SHRINK the budget. Both hops here admit well above MARGINAL_YIELD_FLOOR
    and the judge asks to keep going every time, so MAX_HOPS is the only thing that
    can end this crawl -- and it must.

    DEPENDS ON _two_hop_corpus -- verified by pre-flight review;
    see its docstring for the reasoning and the cheap confirmation."""
    q = _two_hop_corpus(kb, make_concept, make_entity, monkeypatch)
    _install_fake(monkeypatch, stop=False)
    res = get_strategy("cluster-crawl", kb=kb, judge="fake").search(q)

    assert len(res.trace.hops) == 2
    assert res.trace.hops[0]["admitted"] >= 3
    assert res.trace.hops[1]["admitted"] >= 3
    assert res.trace.stop_reason == "max-hops"


def test_a_cold_concept_layer_is_disclosed_and_the_query_still_answers(kb,
                                                                       make_entity):
    """The design's "Cold / stale concept layer" row: concept families empty, so the
    flat seed and the name anchor carry the query. Disclosed via stale_concepts
    beside consolidated_at -- an empty trace.entry looks identical whether the crawl
    found nothing or the layer was never built."""
    note = _note(kb, "n", "postgres tuning notes")
    kb.set_embedding(note.id, _vec(0), embedded_hash="h")
    make_entity("Postgres", mentions=[note.id])

    res = get_strategy("cluster-crawl", kb=kb).search(
        Query(text="Postgres", embedding=_vec(0)))

    assert res.trace.stale_concepts is True
    assert res.trace.consolidated_at is None        # never consolidated
    assert [r.id for r in res.rows] == [note.id]    # the query still answers


def test_a_warm_concept_layer_is_not_reported_as_stale(kb, make_concept):
    """The discriminator. Without this, `stale_concepts = True` unconditionally
    passes the test above and discloses nothing."""
    _seed(kb, make_concept)
    res = get_strategy("cluster-crawl", kb=kb).search(
        Query(text="postgres", embedding=_vec(0)))
    assert res.trace.stale_concepts is False
    assert res.trace.consolidated_at is not None


def test_confidence_declares_which_pool_it_was_computed_over(kb, make_concept):
    """A design requirement discharged rather than deferred. It is marked "Not
    optional" and offers two ways to close it in the same change: re-run the
    off-corpus probes against mode B, or ship `confidence.basis`. This is the second.

    The hazard it discloses is real and one-directional: the crawl's pool is the flat
    pool UNIONED with admitted rows, so best_similarity is monotone NON-DECREASING
    against the flat pool's -- it can only ever push a query toward "strong".
    CONFIDENCE_WEAK is a thin margin over the worst off-corpus top1 across BOTH
    modes (kb_core.knowledge_base's calibration note; re-derived for
    instructed query vectors) -- the union's upward push is exactly why mode B is in
    that derivation.
    `basis` does not fix the calibration; it makes a consumer able to tell the two
    populations apart instead of averaging them.

    BOTH modes carry the key. A key on one mode and not the other would make the
    2x2's confidence column non-comparable -- the same defect in a different place."""
    _seed(kb, make_concept)
    q = Query(text="postgres", embedding=_vec(0))

    crawl = get_strategy("cluster-crawl", kb=kb).search(q)
    assert crawl.confidence["basis"] == "crawl-pool"

    flat = get_strategy("hybrid", kb=kb).search(q)
    assert flat.confidence["basis"] == "flat"

    # The calibrated fields are UNCHANGED in shape by the addition -- `basis` is a
    # fourth key beside them, not a replacement for any of them.
    for key in ("level", "best_similarity", "n"):
        assert key in crawl.confidence and key in flat.confidence


def test_the_crawl_deadline_stops_mid_loop_before_any_hop_runs(kb, make_concept,
                                                                monkeypatch):
    """The mid-loop `CRAWL_DEADLINE_MS` stop -- `if (time.perf_counter() -
    t0) * 1000 >= CRAWL_DEADLINE_MS: stop_reason = "deadline"; break`, checked at the
    TOP of every hop iteration, before a frontier is even built. Needs no clock shim
    at all: zeroing the constant is enough, because elapsed time since `t0` is never
    negative, so the very first hop iteration trips it before touching the judge."""
    _seed(kb, make_concept)
    monkeypatch.setattr(strategy_mod, "CRAWL_DEADLINE_MS", 0)
    res = get_strategy("cluster-crawl", kb=kb).search(
        Query(text="postgres", embedding=_vec(0)))
    assert res.trace.stop_reason == "deadline"
    assert res.trace.hops == []
    assert res.rows                                   # the flat pool still returns


def test_a_zero_hop_crawl_reports_deterministic_not_agentic(kb, monkeypatch):
    """`crawl_mode`'s `hops_trace and` empty-list guard --
    `"agentic" if hops_trace and agentic_hops == len(hops_trace) else ...`. With no
    concept/entity route the hop loop's `if not concept_seeds: continue` (hop 1) and
    empty `seed_bridges` (hop 2) mean hops_trace stays `[]` (see
    test_no_entry_route_degenerates_to_the_flat_pool_and_says_so); `agentic_hops ==
    len(hops_trace)` is then vacuously true (0 == 0). Without the explicit
    `hops_trace and` guard that vacuous truth reports "agentic" for a crawl that made
    no LLM call at all -- and zero-hop is the COMMON shape (many notes may sit in no
    active concept), so this is the field the design's
    agentic_fraction gate
    depends on getting right on the ordinary path, not an edge case.

    Also pins `trace.llm`'s `"unused (no llm hop ran)"` branch (contract.py's
    Trace.llm docstring): the SAME zero-route corpus, run through a uses_llm=True
    judge that never gets a hop to decide, must report "unused", not "ok" (which
    would claim a call succeeded) and not None (which would claim no LLM tier was
    selected at all -- it was, it just never ran)."""
    flat = _note(kb, "flat", "postgres tuning notes")
    kb.set_embedding(flat.id, _vec(0), embedded_hash="hf")
    res = get_strategy("cluster-crawl", kb=kb).search(
        Query(text="postgres", embedding=_vec(0)))
    assert res.trace.hops == []
    assert res.trace.crawl_mode == "deterministic"

    _install_scripted_llm(monkeypatch, [])
    llm_res = get_strategy("cluster-crawl", kb=kb, judge="llm-fake").search(
        Query(text="postgres", embedding=_vec(0)))
    assert llm_res.trace.hops == []
    assert llm_res.trace.crawl_mode == "deterministic"
    assert llm_res.trace.llm == "unused (no llm hop ran)"
    assert llm_res.cost.llm_calls == 0
