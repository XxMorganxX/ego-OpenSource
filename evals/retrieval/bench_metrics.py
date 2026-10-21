"""Pure scoring/protocol functions for the 2x2 benchmark. No DB, no MCP,
no network imports — bench.py, report.py and ablations.py all import from here, and
the tests run offline.

Vocabulary note: a plan step is a RESOLUTION step (kept verbatim:
find_entity resolves names; list_concepts resolves the catalog) or a MEMORY-PRODUCING
step (substituted with search_graph so both columns run the SAME substituted plans:
search_memories, entity_activity, concept, expand). Entity/concept recall are
reference-run metrics only — a substituted plan no longer exercises those routes, so
the grid scores memories (recall/precision/neg_hits) and nothing else."""

# The six run configurations. Repeats: agentic 3x (mean ± spread),
# deterministic/hybrid 2x with the second byte-identical.
CELLS = {
    "cell1":     {"set": "flat",    "strategy": "hybrid",        "judge": None,   "repeats": 2},
    "cell2":     {"set": "package", "strategy": "hybrid",        "judge": None,   "repeats": 2},
    "cell3-det": {"set": "flat",    "strategy": "cluster-crawl", "judge": "none", "repeats": 2},
    "cell3-ag":  {"set": "flat",    "strategy": "cluster-crawl", "judge": "llm",  "repeats": 3},
    "cell4-det": {"set": "package", "strategy": "cluster-crawl", "judge": "none", "repeats": 2},
    "cell4-ag":  {"set": "package", "strategy": "cluster-crawl", "judge": "llm",  "repeats": 3},
}

MEMORY_PRODUCERS = {"search_memories", "entity_activity", "concept", "expand"}
ANCHOR_WANTERS = {"entity_activity", "expand"}
# search_memories args that search_graph accepts 1:1 (KB_MCP server.py's search_graph).
CARRIED_ARGS = ("query", "under", "source", "type", "tags", "kind",
                "since", "until", "include_dead", "limit")


def substitute_plan(item, strategy, judge):
    """Return (substituted_plan, plan_substitution records)."""
    plan = item.get("tool_plan") or [
        {"tool": "search_memories", "args": {"query": item["request"]}}]
    out, subs = [], []
    for step in plan:
        tool, args = step.get("tool"), dict(step.get("args") or {})
        if tool in MEMORY_PRODUCERS:
            new = {k: args[k] for k in CARRIED_ARGS if k in args}
            new.setdefault("query", item["request"])
            new.setdefault("limit", 20)
            new["strategy"] = strategy
            if strategy == "cluster-crawl":
                new["judge"] = judge
            out.append({"tool": "search_graph", "args": new})
            subs.append({"from": tool, "to": "search_graph",
                         "wants_anchor": tool in ANCHOR_WANTERS})
        else:
            out.append({"tool": tool, "args": args})
    return out, subs


def precision_at_k(top_ids, must, may, k):
    """|top-k ∩ (must ∪ may)| / k, with |returned| reported beside it so
    a strategy returning 9 rows is not flattered by a smaller denominator."""
    gold = set(must) | set(may)
    return {"precision": round(sum(1 for i in top_ids[:k] if i in gold) / k, 3),
            "returned": len(top_ids)}


# trace.llm degradation strings start with these (contract.py Trace.llm docstring).
_DEGRADED_PREFIXES = ("unavailable", "deterministic (")


def call_is_clean_agentic(trace):
    """True iff nothing about this call fell back: every hop that ran was agentic,
    and trace.llm is clean. A ZERO-hop crawl is clean: its crawl_mode reads
    "deterministic" and llm reads "unused (no llm hop ran)", but nothing degraded —
    the MIXED gate defends against silent fallback, not against short crawls."""
    if any(not h.get("agentic") for h in (trace.get("hops") or [])):
        return False
    llm = trace.get("llm")
    return llm is None or not llm.startswith(_DEGRADED_PREFIXES)


def agentic_fraction(traces):
    """Across a cell's search_graph calls (agentic cells only)."""
    if not traces:
        return 0.0
    return round(sum(call_is_clean_agentic(t) for t in traces) / len(traces), 3)


def cell_agentic_label(fraction):
    """Below 1.0 the cell is MIXED and may not be reported agentic."""
    return "agentic" if fraction == 1.0 else "MIXED"


def degeneration(traces):
    """degeneration_rate: fraction of calls with any degraded key,
    plus the per-key counts so 'embedder down' and 'quota wall' are not conflated."""
    by_key: dict = {}
    hit = 0
    for t in traces:
        d = t.get("degraded") or {}
        if d:
            hit += 1
        for k in d:
            by_key[k] = by_key.get(k, 0) + 1
    return {"rate": round(hit / len(traces), 3) if traces else 0.0,
            "by_key": by_key}


def flat_displaced_gold(hybrid_top, crawl_top, must, k):
    """Displacement is measured, not prevented — and trace.flat_displaced
    carries only a count, so the GOLD accounting is cross-cell: must-gold ids the
    hybrid cell surfaced in top-k that the crawl cell's top-k displaced."""
    return sorted((set(hybrid_top[:k]) & set(must)) - set(crawl_top[:k]))


def byte_identical(runs):
    """The deterministic-repeat check over {item_id: [top ids]} maps. Returns
    (all_identical, item_ids_that_diverge_from_run_0)."""
    if len(runs) < 2:
        return True, []
    diffs = sorted({iid for other in runs[1:]
                    for iid in set(runs[0]) | set(other)
                    if runs[0].get(iid) != other.get(iid)})
    return not diffs, diffs
