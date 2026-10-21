"""ClusterCrawlStrategy -- the concept/entity crawl, through the same contract as
HybridStrategy.

HOP 0 IS NOT A HOP THIS MODULE RUNS. It is the flat HybridStrategy pool
(HybridStrategy.flat_pool()), the non-negotiable THIRD entry seed: the
concept layer is the ROUTER, never the reachable set. It seeds L_flat AND is unioned
into the final pool unconditionally, so nothing admitted is ever removed and pool
recall is >= max(flat, crawl) by construction. That union -- not a
flat_reserve -- is the honest no-regression floor: reserving half the slots for flat
would bound crawl@20 below by flat@10 while the cell is scored against flat@20,
i.e. a handicap sold as a floor.

THREE VISITED SETS, each with a distinct job:
  visited_nodes   -- a node is offered at most once, EVER. SEEDED WITH THE FLAT POOL,
                     so a flat row never also collects an admission vote (L_admit is
                     crawl-rows-only by design). It is also the seam a future hop 3
                     (expand from an admitted memory's own entities) would need in
                     order not to re-expand a node.
  visited_pairs   -- keyed (parent_id, node_id, family). Prevents the SAME edge being
                     counted into a frontier -- and via bridge_hits, into L_corrob --
                     more than once. Redundant TODAY given visited_nodes (stated
                     plainly in frontier._mark_offered); kept because it is the guard
                     that survives if a later hop relaxes visited_nodes.
  visited_bridges -- keyed on entity id. An entity, once used as a bridge, is never
                     re-expanded: "a mid-degree instance cannot be traversed on both
                     hops".

The deterministic judge always stops after hop 1, so THIS strategy never reaches hop
2 in production. Hop 2's machinery is still built and still exercised by tests,
because swapping in a judge that returns stop=False changes nothing else here --
which is precisely the property the design asserts and the judge seam depends on.

TIME IS A FILTER AND APPEARS IN NO ORDER BY. In practice most notes may be undated,
and source_created_at can collapse to a few identical timestamps after bulk copies,
so ordering by it can be ordering by noise. trace.time_basis and
trace.undated_count disclose that instead.
"""

import time
from dataclasses import replace

from kb_core.knowledge_base import search_confidence

from kb_retrieval.contract import (CRAWL_CAPS, CRAWL_DETERMINISTIC_CAPS,
                                   FILTER_FIELDS, Capabilities, Cost, Query,
                                   RetrievalResult, Row, Trace, estimate_tokens)
from kb_retrieval.crawl.entry import entry_step
from kb_retrieval.crawl.frontier import BRIDGE_FANOUT, hop1_frontier, hop2_frontier
from kb_retrieval.crawl.judge import DeterministicHopJudge, get_judge
from kb_retrieval.crawl.rank import assemble, corrob_rank_list, similarity_rank_list
from kb_retrieval import replay as replay_mod
from kb_retrieval.hybrid import HybridStrategy
from kb_retrieval.registry import register

MAX_HOPS = 2
POOL_CAP = 200
# A hop admitting fewer NEW rows than this ends the crawl (stop #3). A
# convergence test on SET GROWTH, not a similarity threshold on rows -- the latter
# would be a new uncalibrated threshold, which is a recorded measured negative.
MARGINAL_YIELD_FLOOR = 3
# One batched decision per hop. Equal to MAX_HOPS today, so it can only
# bite if MAX_HOPS grows -- which is why
# test_the_call_budget_precheck_skips_the_judge_and_builds_the_fallback_itself
# monkeypatches this down to 1 rather than trusting an unreachable branch.
MAX_LLM_CALLS = 2
# The whole crawl's wall clock.
CRAWL_DEADLINE_MS = 45_000

# Stateless and shared: the fallback rule for a hop whose LLM call is skipped by the
# call budget. Delegating rather than reimplementing "first N in menu order" is
# what keeps ONE admission rule in the codebase.
_DETERMINISTIC = DeterministicHopJudge()


def _add(total, part):
    """Sum two optional provider numbers, keeping None as "nothing was reported".

    The design puts provider_tokens / provider_cost_usd in the agentic cells only, so a
    crawl the provider never answered must report None, not 0 -- a 0 claims a
    measured zero and gets averaged into the benchmark's cost column as one.
    """
    if part is None:
        return total
    return part if total is None else total + part


class ClusterCrawlStrategy:
    name = "cluster-crawl"   # a CLASS attribute -- the registry validates the class

    def __init__(self, kb, *, judge: "str | None" = None, judge_client=None,
                 flat_reserve: int = 0, bridge_fanout: "int | None" = None,
                 ablate: "frozenset[str] | None" = None, **_kw):
        self._kb = kb
        self._hybrid = HybridStrategy(kb)
        # judge_client is the injection seam: registry.get_strategy forwards **kw, so
        # get_strategy("cluster-crawl", kb=kb, judge="llm", judge_client=fake) reaches
        # here. Named explicitly rather than left to **_kw, because **_kw SWALLOWS an
        # unknown kwarg silently (HybridStrategy.__init__ documents the same trap) -- a test
        # that meant to inject a fake would instead build a real client and spawn
        # `claude -p`.
        self._judge = get_judge(judge, client=judge_client)
        # Benchmark knobs. All defaults are behaviour-preserving. Named
        # explicitly because **_kw swallows unknown kwargs silently (the
        # judge_client comment above documents the same trap).
        self._flat_reserve = int(flat_reserve)
        if self._flat_reserve < 0:
            raise ValueError(f"flat_reserve must be >= 0, got {self._flat_reserve}")
        self._bridge_fanout = bridge_fanout
        self._ablate = frozenset(ablate or ())
        unknown = self._ablate - {"l_admit", "l_corrob", "label-cosine"}
        if unknown:
            raise ValueError(f"unknown ablate members {sorted(unknown)}; "
                             f"valid: l_admit, l_corrob, label-cosine")

    def capabilities(self) -> Capabilities:
        """Derived from the judge, not hard-coded.

        CRAWL_CAPS declares uses_llm=True / cost_class="expensive", which is false
        for a judge="none" instance that makes zero LLM calls -- and contract.py's
        own docstring says capabilities() exists so "a benchmark can plan a valid
        matrix before spending expensive calls". A harness sweeping only cheap
        strategies would otherwise skip the deterministic crawl, which is half of
        the 2x2. HopJudge.uses_llm is the planning-time twin of the runtime flag
        cost.llm_calls == 0; the deleted LLMHopJudge set it True and nothing else here
        changes.
        """
        return CRAWL_CAPS if self._judge.uses_llm else CRAWL_DETERMINISTIC_CAPS

    def search(self, q: Query) -> RetrievalResult:
        t0 = time.perf_counter()
        degraded: dict = {}
        if q.embedding is None:
            degraded["embedder"] = "unavailable"   # precedent: KB_MCP server.py search_memories

        with self._kb.read_snapshot() as conn:
            flat_ids, flat_fused, fts, knn, fts_q = self._hybrid.flat_pool(q, conn)
            flat_id_set = set(flat_ids)

            pool_ids: set = set(flat_ids)
            visited_nodes: set = set(flat_ids)
            visited_pairs: set = set()
            visited_bridges: set = set()
            bridge_hits: dict = {}
            admit_order: list = []
            admit_family: dict = {}
            admitted_titles: list = []
            hops_trace: list = []
            hops_replay: list = []
            bridge_capped: list = []
            rejected = 0
            stop_reason = None
            llm_calls = 0
            provider_tokens = None
            provider_cost = None
            llm_status = None

            # The entry-family ablation: skip the A rung (label-cosine) so it
            # never runs, rather than filtering its output after the fact.
            # entry_step's `_add` is first-wins on (kind, id): a post-hoc filter
            # keyed on `via == "label-cosine"` would also remove a concept the C
            # rung (evidence-vote) independently earned, whenever the A rung ran
            # first and claimed it -- ablating part of the damped evidence vote
            # too, contrary to this comment's own promise. Threading the ablation
            # into entry_step lets the C rung (or the name anchor) claim that
            # concept under its own `via` instead. Disclosed via trace.entry,
            # which is built from the returned seeds below.
            seeds = entry_step(self._kb, q, conn,
                               skip_label_cosine=("label-cosine" in self._ablate))
            entry_trace = [{"kind": s.kind, "id": str(s.id), "via": s.via,
                            "label": s.label, "rank": i + 1}
                           for i, s in enumerate(seeds)]
            seed_labels = {s.id: s.label for s in seeds}
            if not seeds:
                degraded["entry"] = "no concept/entity route; degenerated to flat"

            concept_seeds = [s.id for s in seeds if s.kind == "concept"]
            bridge_seeds = {s.id for s in seeds if s.kind == "entity"}
            harvested: list = []

            for hop in range(1, MAX_HOPS + 1):
                if len(pool_ids) >= POOL_CAP:
                    stop_reason = "pool-cap"
                    break
                if (time.perf_counter() - t0) * 1000 >= CRAWL_DEADLINE_MS:
                    stop_reason = "deadline"
                    break
                if hop == 1:
                    if not concept_seeds:
                        continue
                    frontier, harvested = hop1_frontier(
                        self._kb, concept_seeds, q, conn=conn,
                        visited_nodes=visited_nodes, visited_pairs=visited_pairs)
                else:
                    seed_bridges = (bridge_seeds | set(harvested)) - visited_bridges
                    if not seed_bridges:
                        continue
                    # sorted() because seed_bridges is a set: nothing downstream
                    # depends on its order (the SQL supplies its own ordinal), but
                    # bridge_capped's list order would otherwise vary run to run, and
                    # "byte-identical on repeat" is a stated benchmark requirement.
                    frontier, capped = hop2_frontier(
                        self._kb, sorted(seed_bridges, key=str), q, conn=conn,
                        visited_nodes=visited_nodes, visited_pairs=visited_pairs,
                        visited_bridges=visited_bridges,
                        fanout=(BRIDGE_FANOUT if self._bridge_fanout is None
                               else self._bridge_fanout))
                    bridge_capped.extend(capped)
                if not frontier:
                    continue

                # STOP #1, the call-budget pre-check. Checked BEFORE the
                # call, never after: a check that spends the call's latency and then
                # discards the answer is not a deadline, it is an audit. The
                # LLM-latency-vs-remaining-budget estimate this used to layer on top
                # (a p90-based "will another LLM hop fit") was deleted with the LLM
                # tier -- the plain wall-clock check at the top of this
                # loop (`CRAWL_DEADLINE_MS`) already wraps every hop, LLM or not, so
                # nothing here needs to re-estimate it. The read path never blocks on
                # hope.
                skip = None
                if self._judge.uses_llm:
                    if llm_calls >= MAX_LLM_CALLS:
                        skip = ("deterministic (llm call budget spent)", "llm_calls")

                if skip is None:
                    decision = self._judge.decide(q, frontier, hop,
                                                  admitted=tuple(admitted_titles))
                else:
                    decision = replace(_DETERMINISTIC.decide(q, frontier, hop),
                                       by="deterministic", llm_status=skip[0],
                                       degraded_key=skip[1])

                llm_calls += decision.llm_calls
                provider_tokens = _add(provider_tokens, decision.provider_tokens)
                provider_cost = _add(provider_cost, decision.provider_cost_usd)
                if decision.llm_status and decision.llm_status != "ok":
                    # FIRST degradation wins: it is the one that explains the others.
                    llm_status = llm_status or decision.llm_status
                if decision.degraded_key:
                    degraded.setdefault(decision.degraded_key,
                                        decision.llm_status or "degraded")

                # Was this hop's admission QUERY-CONDITIONED JUDGMENT, or a rule?
                # `decision.by` is the tier that actually decided, so an LLMHopJudge
                # that fell back reports "deterministic" and its admissions do NOT
                # feed L_admit (see the l_admit comment below). A judge that declares
                # no tier falls back to its declared CAPABILITY, which is what keeps
                # test_crawl_doctrine's _AdmitAllLLMJudge -- and any future fake --
                # working without opting in. This is capability-then-exercise, never
                # judge identity.
                hop_agentic = (decision.by == "llm"
                               or (self._judge.uses_llm and not decision.by))

                in_range = [i for i in decision.admit if 0 <= i < len(frontier)]
                # Indices outside the offered frontier are DROPPED and COUNTED. A
                # judge cannot emit a node id at all, so an out-of-range index
                # is the only shape a malformed or hostile admission can take.
                rejected += len(decision.admit) - len(in_range)
                admitted = [frontier[i] for i in in_range]
                new_ids = [it.node_id for it in admitted if it.node_id not in pool_ids]

                for it in admitted:
                    pool_ids.add(it.node_id)
                    admit_family.setdefault(it.node_id, set()).add(it.family)
                    if it.family == "bridge":
                        # DISTINCT parents, never a sum of path scores -- multi-path
                        # credit is a count, so hub attraction cannot re-enter
                        # through accounting.
                        bridge_hits.setdefault(it.node_id, set()).update(it.parents)
                    if hop_agentic and it.node_id not in admit_order:
                        admit_order.append(it.node_id)
                    if it.title:
                        admitted_titles.append(it.title)

                # "by" is decision.by, NOT self._judge.name. On a fallback hop the
                # judge's NAME is still "llm" while the tier that decided was the
                # deterministic rule, so reporting the name here would make the trace
                # lie about the one thing the design's agentic_fraction gate reads.
                # The design's own disclosure example already shows two hops carrying
                # DIFFERENT `by` values ("llm", then "deterministic" with
                # "reason":"deadline"), so this is what that example assumed all
                # along. The `or self._judge.name` fallback is for judges (test fakes)
                # that declare no tier.
                hops_trace.append({"hop": hop, "by": decision.by or self._judge.name,
                                   "agentic": hop_agentic,
                                   "offered": len(frontier),
                                   "admitted": len(admitted),
                                   "note": decision.note,
                                   "reason": decision.degraded_key})
                hops_replay.append((hop, decision.by or self._judge.name,
                                    decision.note, frontier, in_range))

                # Stops in the design's order. A judge may only SHRINK the budget:
                # stop=True is honoured immediately, stop=False buys no extra hop
                # beyond what these checks already allow.
                if decision.stop:
                    stop_reason = "judge-stop"
                    break
                if len(new_ids) < MARGINAL_YIELD_FLOOR:
                    stop_reason = "marginal-yield"
                    break
            else:
                if hops_trace:
                    stop_reason = "max-hops"

            # sorted() rather than list(): every consumer below is order-independent
            # (similarity_for returns a dict, order_by_path is server-ordered,
            # similarity_rank_list has an explicit tiebreak), but making the input
            # deterministic means "byte-identical on repeat" is inspectable rather
            # than argued.
            pool = sorted(pool_ids, key=str)
            # No filters passed: every id in the pool already passed its own arm's
            # SEARCH_FILTERS, and the only clause that would still apply -- the
            # scaffolding exclusion -- matches what put them there.
            similarity = ({i: None for i in pool} if q.embedding is None
                          else self._kb.similarity_for(pool, q.embedding, conn=conn))
            path_order = {ident: i for i, ident in
                          enumerate(self._kb.order_by_path(pool, conn=conn))}

            l_sim = similarity_rank_list(pool, similarity)
            l_corrob = corrob_rank_list(bridge_hits)
            # Gated PER HOP, at the accumulation site above, on whether that hop's
            # admission was query-conditioned judgment -- not here on the judge's
            # name, and no longer here on judge.uses_llm alone.
            #
            # Under any non-LLM decision, `admit` is hop 1's frontier MENU order
            # (concept_frontier's `ORDER BY cm.score DESC, cm.node_id`), so letting it
            # reach assemble() would let concept_members.score set RRF positions among
            # crawl rows. The design says the deterministic crawl fuses "minus L_admit";
            # the design says concept_members.score "orders the frontier menu; it never
            # enters the fusion". An LLM judge's admit_order IS query-conditioned
            # judgment (the RANK half of the doctrine) and stays in for that tier.
            #
            # This moved from `l_admit = admit_order if self._judge.uses_llm else []`
            # because uses_llm is the CAPABILITY, and a capability that was not
            # exercised is not judgment: after a quota wall LLMHopJudge returns the
            # deterministic rule's output while uses_llm stays True, which put the
            # menu order straight back into the fusion on the exact path a quota wall
            # makes the common one. hop_agentic reads decision.by, falling back to
            # uses_llm only when a judge declares no tier. Three doctrine tests pin
            # all three cases; see test_crawl_doctrine.py.
            l_admit = admit_order
            # pool=pool is the design's floor, not a ranking input: with l_admit gated
            # off above, an id that is admitted but has no query-conditioned signal
            # at all (unembedded, and never a bridge parent) would otherwise be a
            # member of zero active lists and vanish from fused_order entirely --
            # not merely rank low, disappear -- contradicting "nothing admitted is
            # ever removed" (this module's own docstring) and this repo's own
            # test_nothing_admitted_is_ever_removed_from_the_pool.
            l_admit_used = [] if "l_admit" in self._ablate else l_admit
            l_corrob_used = [] if "l_corrob" in self._ablate else l_corrob
            fused_order, fused = assemble(
                l_flat=flat_ids, l_sim=l_sim,
                l_admit=l_admit_used,
                l_corrob=l_corrob_used,
                similarity=similarity, path_order=path_order,
                pool=pool)

            if self._flat_reserve > 0:
                # Reserve SELECTS which ids enter the page; it never re-ranks -- page
                # order stays fused order (the flat_reserve ablation knob).
                r = min(self._flat_reserve, q.limit)
                reserved = set(flat_ids[:r])
                # Slice by len(reserved), not `r`: when the flat arm returns FEWER
                # than `r` ids (len(flat_ids) < r), `reserved` is short of `r` too,
                # and slicing by `r` would under-fill the page by that shortfall --
                # `q.limit - r` rows from `rest` plus `len(reserved) < r` reserved
                # rows is strictly less than q.limit. len(reserved) is what was
                # actually taken, so it is what the remainder must be sized against.
                rest = [i for i in fused_order
                       if i not in reserved][:q.limit - len(reserved)]
                chosen = reserved | set(rest)
                page = [i for i in fused_order if i in chosen][:q.limit]
            else:
                page = fused_order[:q.limit]
            rows_by_id = self._kb.hydrate(page, fts_q, conn=conn)
            consolidated_at = self._kb.last_consolidation(conn=conn)

        fts_score = {r["id"]: r["score"] for r in fts}
        relevance = {r["id"]: r["relevance"] for r in fts}
        rows, tokens, undated = [], 0, 0
        for ident in page:
            hit = rows_by_id.get(ident)
            if hit is None:
                continue
            node, snippet = hit
            via = (["flat"] if ident in flat_id_set else []) \
                + sorted(admit_family.get(ident, ()))
            rows.append(Row(
                id=node.id, path=node.path, title=node.title,
                similarity=similarity.get(ident),
                # Row.rank keeps SearchResult.rank's two-branch meaning
                # (domain.SearchResult): raw ts_rank when the caller supplied no
                # embedding, the fused score otherwise. With no embedding there is no
                # entry step and no kNN arm, so every pool id came from the FTS arm
                # and `relevance` covers the page.
                rank=(relevance.get(ident, 0.0) if q.embedding is None
                      else fused[ident]),
                score=fts_score.get(ident, 0.0), via=via, snippet=snippet,
                doc_kind=node.doc_kind, event_at=node.event_at,
                lifecycle=node.lifecycle, content_chars=len(node.content or ""),
                # Display metadata, from the Node hydrate() already returned. The
                # SAME seven hybrid.py sets, from the same source, so an agent gets
                # the same information whichever tool it called -- which is also what
                # keeps the benchmark's tokens_returned column comparable between the
                # two modes. See contract.Row.
                type=node.type, tags=list(node.tags or []), source=node.source,
                uri=node.uri, summary=node.summary, importance=node.importance,
                dead_reason=node.dead_reason))
            tokens += estimate_tokens(snippet, node.title)
            if node.event_at is None:
                undated += 1

        self._kb.record_access([r.id for r in rows])
        applied = {k: v for k, v in
                   {f: getattr(q, f) for f in FILTER_FIELDS}.items() if v is not None}
        if q.include_dead is False:
            applied["include_dead"] = False
        # anchor_entity is CRAWL_CAPS' one filter beyond FILTER_NAMES (contract.CRAWL_CAPS)
        # and FILTER_FIELDS deliberately excludes it -- that tuple is shared with
        # HybridStrategy, which has no anchor_entity field at all. It is genuinely
        # honoured whenever set: entry_step adds it as a seed unconditionally, first,
        # needing no embedding (crawl.entry.entry_step). Left out here it satisfied
        # neither half of the contract's disclosure pair -- not applied_filters, not
        # ignored_filters -- so a caller had no way to confirm the anchor was used.
        if q.anchor_entity is not None:
            applied["anchor_entity"] = q.anchor_entity

        ignored = []
        if q.include_dead:
            # include_dead IS passed to concept_frontier/bridge_frontier and IS
            # applied there -- but it cannot do what the caller asked. Consolidation
            # builds its graph dead-free, so a retired note is not in concept_members
            # to begin with: the flag can only ever fail to REMOVE rows, never ADD
            # the ones consolidation never wrote, so some must-recall gold notes are
            # dead and unreachable by this arm. The flat arm (hop 0,
            # unioned unconditionally) still reaches them, so nothing is lost overall
            # -- only THIS arm's blind spot is disclosed. That complementarity is the
            # single strongest reason the flat pool is UNIONED rather than reserved.
            ignored.append("include_dead@concept-arm")

        # A hard facet selected these rows, so cosine is the wrong yardstick. Same
        # five fields KB_MCP's search_memories uses.
        scoped = any(v is not None for v in
                     (q.under, q.kind, q.tags, q.since, q.until))
        page_set = set(page)
        # Displacement is MEASURED, not prevented (floor #3). The harness
        # adds the gold accounting.
        flat_displaced = {"n": sum(1 for i in flat_ids[:q.limit] if i not in page_set)}

        # THREE-VALUED, and derived from what each hop actually did rather than from
        # the judge's capability. The design: a cell whose agentic_fraction < 1.0
        # is MIXED and may not be reported as agentic -- that gate is the defence
        # against a quota wall opening mid-run, after which every remaining item
        # silently runs deterministic and the cell reads as a tie with its own
        # control. A crawl with no hops at all is "deterministic": it made no LLM
        # call, which is what cost.llm_calls == 0 already says.
        agentic_hops = sum(1 for h in hops_trace if h["agentic"])
        crawl_mode = ("agentic" if hops_trace and agentic_hops == len(hops_trace)
                      else "mixed" if agentic_hops else "deterministic")
        # None when no LLM tier was selected at all -- distinct from "ok" (a call
        # succeeded) and from a degradation string (a call failed or was skipped).
        llm_trace = None
        if self._judge.uses_llm:
            llm_trace = llm_status or ("ok" if llm_calls else "unused (no llm hop ran)")

        replay = replay_mod.guarded(lambda: replay_mod.build(
            limit=q.limit,
            flat=replay_mod.flat_stage(
                ordered=flat_ids, fused=flat_fused, fts=fts, knn=knn, fts_q=fts_q,
                query_text=q.text, embedded=q.embedding is not None),
            crawl=replay_mod.crawl_stage(
                entry=entry_trace,
                hops=[replay_mod.hop_record(hop=h, by=by, note=note, frontier=fr,
                                            admitted_idx=adm, labels=seed_labels)
                      for (h, by, note, fr, adm) in hops_replay],
                l_flat=flat_ids, l_sim=l_sim, l_admit=l_admit_used,
                l_corrob=l_corrob_used, fused_order=fused_order, fused=fused,
                similarity=similarity, stop_reason=stop_reason)), degraded)

        return RetrievalResult(
            rows=rows,
            # search_confidence's FUNCTION is untouched -- same calibration, same
            # level/best_similarity/n, computed over the final rows' cosines -- so
            # the two modes stay directly comparable. One key is ADDED.
            #
            # THE DESIGN MARKS THIS "NOT OPTIONAL": a union pool makes
            # best_similarity monotone non-decreasing, so it can only ever push a
            # crawl toward "strong" against CONFIDENCE_WEAK -- a thin margin over
            # the worst off-corpus top1 across BOTH modes (kb_core.knowledge_base's
            # calibration note; re-derived for instructed query vectors)
            # -- the union's upward push is exactly why mode B is in that
            # derivation. The spec gives two ways to discharge the union-pool risk
            # IN THE SAME CHANGE: re-run the off-corpus probes against mode B, or ship
            # `confidence.basis`. This is the second. `basis` names WHICH POOL the
            # number was computed over, so a consumer -- an agent reading
            # search_graph, or the benchmark harness reading a result file -- can tell a
            # flat-pool 0.55 from a crawl-pool 0.55 instead of averaging them. It is
            # disclosure, not a new threshold: nothing branches on it here.
            confidence=dict(search_confidence(rows, scoped=scoped),
                            basis="crawl-pool"),
            applied_filters=applied,
            ignored_filters=ignored,
            cost=Cost(tokens_returned=tokens, llm_calls=llm_calls,
                      provider_tokens=provider_tokens,
                      provider_cost_usd=provider_cost,
                      latency_ms=int((time.perf_counter() - t0) * 1000)),
            trace=Trace(
                crawl_mode=crawl_mode,
                hops=hops_trace, entry=entry_trace, bridge_capped=bridge_capped,
                consolidated_at=consolidated_at, undated_count=undated,
                time_basis="event_at, else source_created_at",
                flat_displaced=flat_displaced, degraded=degraded,
                stop_reason=stop_reason, rejected_admissions=rejected,
                llm=llm_trace,
                # The design's "Cold / stale concept layer" row: no concept family
                # routed, so the flat seed and the name anchor carried the query.
                # Many notes may sit in no active concept, so this is a
                # common, disclosed outcome -- not an alarm.
                stale_concepts=not concept_seeds),
            replay=replay)


register("cluster-crawl", ClusterCrawlStrategy)
