"""Mode A -- the measured hybrid ranker, served through the contract.

Behaviour is identical to KnowledgeBase.search() by construction and by proof
(tests/test_equivalence.py). What moved is WHERE fusion happens: the two arms are
now fetched as candidate lists and fused in Python, so a second strategy can reuse
exactly the same fuse() and the same arms.

The OR-fallback gate lives here rather than in KB_Core because it is POLICY -- a
query-rewriting decision -- while the probe it needs is SQL. KB_Core owns the
probe; this module owns the rule.
"""

import time

from kb_core.knowledge_base import (HYBRID_ARM_POOL, or_fallback_eligible,
                                    search_confidence)

from kb_retrieval.contract import (FILTER_FIELDS, HYBRID_CAPS, Capabilities, Cost,
                                   Query, RetrievalResult, Row, Trace,
                                   estimate_tokens)
from kb_retrieval import replay as replay_mod
from kb_retrieval.fusion import rrf
from kb_retrieval.registry import register


class HybridStrategy:
    name = "hybrid"

    def __init__(self, kb, **_kw):
        # **_kw is swallowed on purpose -- registry.get_strategy forwards kwargs to
        # every strategy by design, and mode A takes none. That also means a
        # typo'd kwarg (e.g. judge=... aimed at a different strategy) vanishes
        # silently rather than raising. Harmless with one strategy; worth a
        # stricter signature (or a "did you mean" check in the registry) once a
        # second one exists to typo *for*.
        self._kb = kb

    def capabilities(self) -> Capabilities:
        return HYBRID_CAPS

    def _fts_query(self, q: Query, conn) -> str:
        """websearch_to_tsquery is AND-semantics, so a keyword bag that never
        co-occurs verbatim silently empties the FTS arm and hybrid degrades to
        vector-only. Fall back to OR-joined terms -- but only for SHORT bags: a long
        natural-language request would OR-match half the corpus and flood the fusion
        (measured on a long golden query). Long queries are the vector arm's job -- unless there is
        no vector arm (no query embedding), where the cap is far higher. The length
        rule is KB_Core's or_fallback_eligible, shared with KnowledgeBase.search().
        """
        if or_fallback_eligible(q.text, embedded=q.embedding is not None):
            if not self._kb.fts_matches_any(q.text, conn=conn):
                return " OR ".join(q.text.split())
        return q.text

    def flat_pool(self, q: Query, conn) -> tuple:
        """The flat arm's FULL candidate pool -- up to HYBRID_ARM_POOL ids, fused and
        ordered -- i.e. exactly what search() computes before slicing to q.limit.

        Extracted so ClusterCrawlStrategy can use it BOTH as hop 0 (the
        non-negotiable third entry seed, the design: the concept layer is the ROUTER,
        never the reachable set) and as L_flat, without a second
        implementation of "what is the flat pool". A second implementation is
        exactly how the two strategies would drift out of comparability while both
        still passing their own tests -- and the 2x2 benchmark's whole premise is
        that its two columns share an arm.

        Returns `(ordered_ids, fused, fts, knn, fts_q)`:
          ordered_ids  the fused order, NOT truncated to q.limit
          fused        {id: rrf score} -- Row.rank's hybrid-branch value
          fts, knn     the raw candidate rows, so a caller can rebuild
                       via/score/relevance/similarity without refetching
          fts_q        the query text ACTUALLY searched. The OR-fallback may have
                       rewritten it, and hydrate()'s ts_headline must be given the
                       same text or the snippet highlights terms nothing matched.
        """
        pool = max(q.limit, HYBRID_ARM_POOL)
        filters = {f: getattr(q, f) for f in FILTER_FIELDS}
        fts_q = self._fts_query(q, conn)
        fts = self._kb.fts_candidates(
            fts_q, pool=pool, include_dead=q.include_dead, conn=conn, **filters)
        knn = ([] if q.embedding is None else self._kb.knn_candidates(
            q.embedding, pool=pool, include_dead=q.include_dead, conn=conn,
            **filters))
        fused = rrf([[r["id"] for r in fts], [r["id"] for r in knn]])
        # Mode A's final ordering: `ORDER BY fused.rrf DESC, nodes.path`. The path
        # tiebreak comes from the server via order_by_path -- see that primitive's
        # docstring; the trap is `ORDER BY path::text`, which _COLUMNS already
        # selects, NOT a Python sort.
        by_path = {ident: i
                   for i, ident in enumerate(
                       self._kb.order_by_path(list(fused), conn=conn))}
        ordered = sorted(fused, key=lambda i: (-fused[i], by_path.get(i, 1 << 30)))
        return ordered, fused, fts, knn, fts_q

    def _flat_replay(self, q: Query, ordered, fused, fts, knn, fts_q,
                     degraded: dict) -> "dict | None":
        # Built from values flat_pool() already returned -- no query, and guarded so
        # a builder bug lands in `degraded`, never in the rows.
        return replay_mod.guarded(lambda: replay_mod.build(
            limit=q.limit,
            flat=replay_mod.flat_stage(
                ordered=ordered, fused=fused, fts=fts, knn=knn, fts_q=fts_q,
                query_text=q.text, embedded=q.embedding is not None)), degraded)

    def capture_replay(self, q: Query) -> "tuple[dict | None, dict]":
        """The flat stage's replay WITHOUT running the page: no hydrate, no
        record_access. For callers that serve rows from KnowledgeBase.search()
        itself (search_memories, whose wire format is pinned to that function's
        output) but still want the arms recorded. Run it BEFORE that search --
        record_access feeds the FTS score, so the reverse order could rank
        differently. Returns (replay or None, degraded)."""
        degraded: dict = {}
        with self._kb.read_snapshot() as conn:
            ordered, fused, fts, knn, fts_q = self.flat_pool(q, conn)
        return self._flat_replay(q, ordered, fused, fts, knn, fts_q, degraded), degraded

    def search(self, q: Query) -> RetrievalResult:
        t0 = time.perf_counter()
        filters = {f: getattr(q, f) for f in FILTER_FIELDS}
        degraded: dict = {}

        with self._kb.read_snapshot() as conn:
            ordered, fused, fts, knn, fts_q = self.flat_pool(q, conn)
            page = ordered[:q.limit]
            rows_by_id = self._kb.hydrate(page, fts_q, conn=conn)

        sim = {r["id"]: r["similarity"] for r in knn}
        score = {r["id"]: r["score"] for r in fts}
        # Row.rank must keep SearchResult.rank's exact two-branch meaning
        # (domain.SearchResult, contract.py's Row docstring): raw ts_rank when the
        # caller supplied no embedding, the fused RRF score otherwise. That
        # dispatch is on q.embedding, same as legacy search() -- NOT on whether
        # `knn` happened to come back empty, which can also happen with an
        # embedding supplied (e.g. nothing in the filtered scope has a vector),
        # and legacy still uses fused.rrf as `rank` in that case.
        relevance = {r["id"]: r["relevance"] for r in fts}
        rows, tokens = [], 0
        for ident in page:
            hit = rows_by_id.get(ident)
            if hit is None:
                continue
            node, snippet = hit
            via = [v for v, present in
                   (("flat-fts", ident in score), ("flat-knn", ident in sim)) if present]
            rank = relevance[ident] if q.embedding is None else fused[ident]
            rows.append(Row(
                id=node.id, path=node.path, title=node.title,
                similarity=sim.get(ident), rank=rank, score=score.get(ident, 0.0),
                via=via, snippet=snippet, doc_kind=node.doc_kind,
                event_at=node.event_at, lifecycle=node.lifecycle,
                content_chars=len(node.content or ""),
                # Display metadata, from the Node hydrate() already returned. Free:
                # no extra query, no extra column fetch. See contract.Row.
                type=node.type, tags=list(node.tags or []), source=node.source,
                uri=node.uri, summary=node.summary, importance=node.importance,
                dead_reason=node.dead_reason))
            tokens += estimate_tokens(snippet, node.title)

        self._kb.record_access([r.id for r in rows])
        applied = {k: v for k, v in filters.items() if v is not None}
        if q.include_dead is False:
            applied["include_dead"] = False
        # anchor_entity is CRAWL_CAPS' one filter beyond HYBRID_CAPS (contract.CRAWL_CAPS)
        # -- mode A has no crawl and nothing to seed with an entity anchor, so a
        # caller who sets it here gets nothing for it. RetrievalStrategy's contract requires
        # every filter the query SETS to land in applied_filters or ignored_filters;
        # this is the twin of the anchor_entity disclosure gap ClusterCrawlStrategy
        # had (in ClusterCrawlStrategy.search) -- that one was genuinely honoured and just
        # unreported, this one is never honoured at all, so "@hybrid" in
        # ignored_filters is the correct half of the pair rather than
        # applied_filters.
        ignored = ["anchor_entity@hybrid"] if q.anchor_entity is not None else []
        # A hard facet selected these rows, so cosine is the wrong yardstick.
        scoped = any(v is not None for v in
                     (q.under, q.kind, q.tags, q.since, q.until))
        # Built from values flat_pool() already returned -- no query, and guarded so
        # a builder bug lands in trace.degraded, never in the rows.
        replay = self._flat_replay(q, ordered, fused, fts, knn, fts_q, degraded)
        return RetrievalResult(
            rows=rows,
            # Row exposes `.similarity`, which is the only attribute
            # search_confidence reads -- so the calibrated function is reused
            # directly, with no adapter and no reimplementation.
            #
            # `basis` is the design's disclosure key, and mode A carries it too --
            # ON PURPOSE: the CONFIDENCE_WEAK/STRONG calibration was
            # measured against BOTH modes' pools, not mode A alone
            # (kb_core.knowledge_base's calibration note). A key present on one mode
            # and absent on the other makes the 2x2's confidence column
            # non-comparable, which is the same defect the key exists to prevent.
            # "flat" is what an unmodified flat pool has always meant.
            confidence=dict(search_confidence(rows, scoped=scoped), basis="flat"),
            applied_filters=applied,
            ignored_filters=ignored,
            cost=Cost(tokens_returned=tokens,
                      latency_ms=int((time.perf_counter() - t0) * 1000)),
            trace=Trace(crawl_mode=None, time_basis="event_at, else source_created_at",
                        degraded=degraded),
            replay=replay)


register("hybrid", HybridStrategy)
