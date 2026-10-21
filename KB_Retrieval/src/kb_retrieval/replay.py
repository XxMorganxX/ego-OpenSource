"""Replay capture -- a faithful, per-stage record of one search for the dashboard's
retrieval replay.

Pure functions over values each strategy ALREADY holds in memory: no queries, no
SQL (this repo's rule), nothing that can change what the search returns. Memory ids
are stored bare and labelled at read time; concept/entity labels are frozen here
because a concept rebuild replaces concept ids. Everything is JSON-native (str /
float / bool / None) so the dict goes straight into mcp_trace.replay.

`guarded` is the only way a strategy calls in: replay is advisory, so a builder bug
must surface in trace.degraded["replay"], never as a failed or altered search.
"""

import math

REPLAY_VERSION = 1


def _f(x) -> "float | None":
    if x is None:
        return None
    v = float(x)
    return v if math.isfinite(v) else None   # jsonb rejects NaN/Infinity


def flat_stage(*, ordered, fused, fts, knn, fts_q, query_text, embedded) -> dict:
    """The flat pool, from HybridStrategy.flat_pool()'s own return values: both arm
    lists in arm order, and the FULL fused order (not truncated to the page)."""
    return {
        "fts_query": fts_q,
        "or_fallback": fts_q != query_text,
        "fts": [{"id": str(r["id"]), "score": _f(r["score"])} for r in fts],
        "knn": [{"id": str(r["id"]), "sim": _f(r["similarity"])} for r in knn],
        "fused": [{"id": str(i), "rrf": _f(fused.get(i))} for i in ordered],
        "embedder": "ok" if embedded else "unavailable",
    }


def hop_record(*, hop, by, note, frontier, admitted_idx, labels) -> dict:
    """One crawl hop: every OFFERED FrontierItem, its parents, and whether the judge
    admitted it. `admitted_idx` are positions into `frontier` (the judge's in-range
    indices). Hop-1 parents are concepts, hop-2 parents bridge entities; a parent
    with no label in `labels` (an entity harvested mid-crawl) is stored as None and
    labelled at read time."""
    admitted = set(admitted_idx)
    parent_kind = {"concept": "concept", "bridge": "entity"}
    return {
        "hop": hop, "by": by, "note": note,
        "offered_count": len(frontier), "admitted_count": len(admitted),
        "offered": [
            {"id": str(it.node_id), "family": it.family, "admitted": pos in admitted,
             "parents": [{"kind": parent_kind.get(it.family, it.family),
                          "id": str(p), "label": labels.get(p)}
                         for p in it.parents]}
            for pos, it in enumerate(frontier)],
    }


def crawl_stage(*, entry, hops, l_flat, l_sim, l_admit, l_corrob, fused_order,
                fused, similarity, stop_reason) -> dict:
    """The crawl as it ran: entry seeds (already label-carrying trace dicts), hop
    records, the four lists EXACTLY as passed to assemble() (an ablated or gated list
    is recorded empty), and the full assembled order."""
    ids = lambda xs: [str(i) for i in xs]  # noqa: E731
    return {
        "entry": entry,
        "hops": hops,
        "lists": {"flat": ids(l_flat), "sim": ids(l_sim), "admit": ids(l_admit),
                  "corrob": ids(l_corrob)},
        "final": [{"id": str(i), "rrf": _f(fused.get(i)), "sim": _f(similarity.get(i))}
                  for i in fused_order],
        "stop_reason": stop_reason,
    }


def build(*, limit, flat, crawl=None) -> dict:
    out = {"v": REPLAY_VERSION, "limit": limit, "flat": flat}
    if crawl is not None:
        out["crawl"] = crawl
    return out


def guarded(fn, degraded: dict) -> "dict | None":
    """Run a replay builder; on ANY exception record it in `degraded` and return None.
    The search result is never touched."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 -- advisory capture must not raise
        degraded["replay"] = f"{type(exc).__name__}: {exc}"
        return None
