"""Rank assembly -- the ONE place a final order is produced.

THE DOCTRINE: "Structure may ADMIT candidates. Only the query -- and
query-conditioned judgment -- may RANK." entry.py, frontier.py and judge.py decide
MEMBERSHIP: which ids reach assemble() at all, and (via the judge) which subset was
admitted. This module decides ORDER, and every input it takes is one of four things:
the query's own cosine (l_sim), a fusion the query already ranked (l_flat), the
judge's query-conditioned call (l_admit), or a plain structural COUNT of distinct
bridge parents (l_corrob). The last is admissible per the design precisely because a
count only ever sets ONE list POSITION among four equally weighted lists -- it is
not a query-blind score smuggled into the sort.

The doctrine is made STRUCTURAL here, not merely documented: these functions do not
accept a menu_score parameter at all, so "concept_members.score promoted this row"
is unrepresentable rather than untested. That matters because both forbidden
currencies look plausible up close. concept_members.score is SATURATED -- most rows
the read path touches sit at or near 1.0 -- and orthogonal to query relevance.
entity_links.weight is almost entirely a function of link_type (NULL exactly where
link_type is belongs_to), i.e. ~zero information, and weight-ordered selection
measured ADVERSE (it picked lower-importance memories). Neither reaches this module.

All weights are 1.0 and RRF_K is imported, never re-declared. There are no weight
constants in this file; adding one reintroduces exactly the tuning surface RRF was
chosen to avoid.

`assemble()` also takes an optional `pool` -- NOT a fifth ranking input, and not one
of the "four things" above. `l_admit` is gated PER HOP by strategy.py, on
`decision.by` -- the tier that actually decided that hop -- falling back to the
judge's declared capability only when that hop's own decision names no tier
(`decision.by` empty); never on capability alone once a hop has actually decided.
See crawl/strategy.py's `hop_agentic` (`decision.by == "llm" or (judge.uses_llm and
not decision.by)`). When it is gated off for a hop, an admitted, scoreless,
non-bridge row can end up in none of the four lists; `pool` guarantees it still gets
a 0.0 entry rather than silently vanishing, which cannot move it above any
id a real list actually ranked.
"""

from uuid import UUID

from kb_core.knowledge_base import RRF_K

from kb_retrieval.fusion import rrf


def similarity_rank_list(ids: "list[UUID]",
                         similarity: "dict[UUID, float | None]") -> "list[UUID]":
    """L_sim: cosine-to-query rank over every row THIS RULER HAS AN OPINION ON.

    A row is OMITTED -- not tailed on in some tiebreak order -- when it is
    scoreless, which happens for three innocent reasons: the query had no
    embedding, the note has neither a whole-note nor a chunk vector, or
    similarity_for() itself omitted it (a facet- or scaffolding-excluded id; see
    that primitive's docstring). rrf() already treats absence from a list as a 0
    contribution, so tailing scoreless rows on in `str(ident)` order was buying
    nothing -- exactly corrob_rank_list's reasoning for omitting zero-count rows,
    26 lines below, which this used to contradict: with no query embedding (or for
    any pool row similarity_for returns None for) the omitted alternative put the
    WHOLE unscored remainder of the pool in UUID order at full RRF weight. The design
    is explicit that an embedder outage means "no L_sim", not "L_sim in id
    order" -- degrade to mode A's FTS-only path. Pool coverage does not depend on
    this list: `pool_ids` is `flat ∪ admitted`, covered either by `l_flat ∪ l_admit`
    (an agentic hop) or by `assemble()`'s `pool=` floor (a hop whose admission was
    not query-conditioned judgment: `l_admit` is gated PER HOP by strategy.py, on
    `decision.by` -- the tier that actually decided that hop -- falling back to the
    judge's declared capability only when that hop's own decision names no tier
    (`decision.by` empty); never on capability alone once a hop has actually
    decided. See crawl/strategy.py's `hop_agentic`.), so an omitted-from-
    l_sim row is never dropped from the fused order, it just does not ALSO collect
    a position from a ruler that has nothing to say about it.

    Ties among SCORED rows break on `str(id)`. strategy.py passes this a Python
    `set`, whose iteration order is not stable across runs, so without an explicit
    tiebreak two rows with equal cosine take different POSITIONS on different
    runs -- and RRF turns a position into a score. "Byte-identical on repeat" is a
    stated benchmark requirement.
    """
    scored = [ident for ident in ids if similarity.get(ident) is not None]
    return sorted(scored, key=lambda ident: (-similarity[ident], str(ident)))


def corrob_rank_list(bridge_hits: "dict[UUID, set[UUID]]") -> "list[UUID]":
    """L_corrob: the count of DISTINCT non-class bridge (entity) parents that
    reached each row, ranked.

    `bridge_hits[node_id]` is the set of bridge entity ids that reached `node_id`,
    built by strategy.py from admitted family="bridge" frontier items only --
    concept-family admissions never populate it, per the design's "non-class bridge
    parents". Multi-path credit is this distinct-parent COUNT, never a sum of path
    scores, so hub attraction cannot re-enter through accounting.

    A node with zero bridge parents is omitted: rrf() already treats absence as a 0
    contribution, and an included zero-count tail would need an arbitrary tie order
    for no benefit. Ties break on `str(node_id)` rather than dict/set iteration
    order, for the same reproducibility reason as similarity_rank_list.

    The design names this the list most likely to be wrong and the one the
    aboutness thesis leans on hardest -- two bridges are not independent evidence
    when both entities came from the same sentence via the same enrichment pass.
    That is settled by the L_corrob ablation, not here.
    """
    counted = [(nid, len(parents)) for nid, parents in bridge_hits.items() if parents]
    counted.sort(key=lambda pair: (-pair[1], str(pair[0])))
    return [nid for nid, _ in counted]


def assemble(*, l_flat: "list[UUID]", l_sim: "list[UUID]", l_admit: "list[UUID]",
             l_corrob: "list[UUID]", similarity: "dict[UUID, float | None]",
             path_order: "dict[UUID, int]", pool: "list[UUID] | None" = None) -> tuple:
    """Fuse the (up to) four rank lists at weight 1.0 and return
    `(ordered_ids, fused_scores)`.

    The scores come back as well as the order because Row.rank must carry the fused
    RRF value in the embedding branch -- exactly as SearchResult.rank does
    (domain.SearchResult) -- and re-deriving it from a position would silently change
    what MCP puts on the wire.

    `active` drops empty lists before calling rrf(). An absent list already
    contributes 0 per id, but filtering here makes the 4-way-vs-3-way ablation that
    the design calls for a one-line change AT THE CALL SITE: comment out a list,
    nothing else moves. `l_sim` no longer covers the whole pool unconditionally --
    similarity_rank_list omits any row it has no score for (see that function's
    docstring) -- and `l_admit` is gated PER HOP by strategy.py, on `decision.by` --
    the tier that actually decided that hop -- falling back to the judge's declared
    capability only when that hop's own decision names no tier (`decision.by`
    empty); never on capability alone once a hop has actually decided. See
    crawl/strategy.py's `hop_agentic`. So an id admitted on a hop whose admission
    was not query-conditioned judgment, if also scoreless AND not a bridge parent,
    can now be a member of NONE of the four lists.

    `pool=` is the floor that keeps "nothing admitted is ever removed" true whenever
    L_admit is inactive for a hop. That is NOT only "a non-LLM judge": it
    is any hop whose admission was not query-conditioned judgment -- including an
    LLMHopJudge hop that FELL BACK after a quota wall, a parse failure, an open circuit
    or the deadline pre-check. Those were the common cases while the LLM tier existed,
    so this floor carries more weight than the original wording implied, not less.

    `pool`, if given, is the caller's full "nothing admitted is ever removed" floor:
    every id in it gets AT LEAST a 0.0 entry in `fused` if the four lists
    left it out entirely. This is not a fifth ranking currency -- 0.0 is exactly
    what rrf() already documents absence-from-every-list as worth, and every id it
    actually touches keeps its real (necessarily positive) score untouched; `pool`
    only stops that implicit 0 from also meaning "silently dropped from the
    returned order". Ids that land on this floor tie with each other at that 0.0
    and break on `path_order` alone (`sort_key` below already treats a missing
    `similarity` entry as NULLS LAST, so no special-casing is needed for them).
    `path_order` is a TOTAL order over these floored rows only because `nodes.path`
    is `UNIQUE` (the `nodes` table in `KB_Core/src/kb_core/migrations/0001_init.sql`); if that
    constraint were ever dropped, a floored tie would fall back to dict insertion
    order, silently breaking byte-identical-on-repeat exactly where this suite is
    least likely to catch it.
    Callers that never pass `pool` (every existing test, and any future ablation
    that wants the OLD "only what the active lists carry" behaviour) see no change.

    Tiebreak, on an EXACT fused-score tie ONLY: similarity DESC NULLS LAST, then
    order_by_path. Both are server-derived. `path_order` MUST come from
    KnowledgeBase.order_by_path() -- taking it from the server removes a dependency
    on the coincidence that ltree's native ordering happens to equal byte order on
    this charset, and avoids `ORDER BY path::text`, which genuinely diverges.
    """
    active = [lst for lst in (l_flat, l_sim, l_admit, l_corrob) if lst]
    fused = rrf(active, k=RRF_K)
    if pool is not None:
        for ident in pool:
            fused.setdefault(ident, 0.0)

    def sort_key(ident):
        sim = similarity.get(ident)
        sim_key = (0, -sim) if sim is not None else (1, 0.0)
        return (-fused.get(ident, 0.0), sim_key, path_order.get(ident, 1 << 30))

    return sorted(fused, key=sort_key), fused
