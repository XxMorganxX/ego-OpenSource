"""Per-arm graph inputs for the entity-weighting sweep.

Two transformations live here. Blocked-name synthesis answers "is the entity layer starved
or genuinely unhelpful?"; granularity matching separates a ratio effect from a
resolution effect.
"""
from __future__ import annotations

from collections import defaultdict

from kb_core.concepts import build_graph, detect_communities
from evals.concepts.co_membership import MEMORY

BLOCKED_PREFIX = "blocked:"


def blocked_mention_rows(candidates: "list[dict]", live_ids: "set[str]", *,
                         min_memories: int = 10) -> "list[dict]":
    """Mention edges for names the blocklist permanently suppressed.

    Blocked names never became entities, so there are no `entities` or `entity_links` rows
    to re-enable -- a real restore would mean promoting them, which is a write. Instead each
    blocked name becomes a pseudo-entity vertex built straight from `entity_candidates`,
    scored by the same formula as a real one.

    This is a SIMULATION, not a restoration: it skips the meaning judge, alias resolution
    and merge folding. It is a directional signal on whether the starved layer is the
    binding constraint, not a forecast of what un-blocking would actually yield.
    """
    by_name: "dict[str, dict[str, float]]" = defaultdict(dict)
    for cand in candidates:
        mid = str(cand["memory_id"])
        if mid not in live_ids:
            continue
        name = cand["name"]
        conf = float(cand["confidence"]) if cand["confidence"] is not None else 0.5
        by_name[name][mid] = max(by_name[name].get(mid, 0.0), conf)

    rows: "list[dict]" = []
    for name in sorted(by_name):
        mems = by_name[name]
        if len(mems) < min_memories:
            continue
        support = len(mems)
        for mid in sorted(mems):
            rows.append({"memory_id": mid, "entity_id": f"{BLOCKED_PREFIX}{name}",
                         "weight": mems[mid], "support": support})
    return rows


def level0_count(communities: "list[dict]") -> int:
    """Top-level concepts only. Children are sub-structure, not separate concepts."""
    return sum(1 for c in communities if c["level"] == 0)


def level0_sizes(communities: "list[dict]") -> "list[int]":
    """Sorted level-0 community sizes, by memory-member count (kind == MEMORY, matching
    what `co_membership.membership()` counts -- entity members are not a community's
    "size" for this purpose).

    Two arms can report the same `level0_count` while partitioning the corpus into
    completely different cells (same number, not the same communities) -- recorded per
    arm so that class of error is checkable from `sweep.json` directly, not just from the
    scalar count. Equal counts must never be read as equal partitions.
    """
    return sorted(
        sum(1 for _node_id, kind, _score in c["members"] if kind == MEMORY)
        for c in communities if c["level"] == 0)


def partition(mention: "list[dict]", knn: "list[dict]", *, entity_alpha: float = 1.0,
              resolution: float = 1.0, **detect_kwargs) -> "list[dict]":
    """One arm's partition. hub_share=None matches production defaults -- both hub
    defenses are off (coherence gate defaulted off, HUB_SHARE=0)."""
    # seed and hub_share are not caller-tunable: run-to-run reproducibility is a
    # correctness requirement of the sweep, and hub_share=None is what matches
    # production. Fail loudly rather than silently de-randomising.
    for reserved in ("seed", "hub_share"):
        if reserved in detect_kwargs:
            raise TypeError(f"{reserved} is fixed by partition() and cannot be overridden")
    graph = build_graph(mention, knn, hub_share=None, entity_alpha=entity_alpha)
    return detect_communities(graph, resolution=resolution, **detect_kwargs)


def match_resolution(mention: "list[dict]", knn: "list[dict]", *, target: int,
                     entity_alpha: float, lo: float = 0.2, hi: float = 6.0,
                     max_iter: int = 20, **detect_kwargs) -> "tuple[float, int]":
    """Find the resolution whose level-0 community count is closest to `target`.

    This is the control that separates a RATIO effect from a RESOLUTION effect. Scaling one
    edge family changes total graph weight, which is modularity's null-model term, so a
    higher alpha can produce larger communities for reasons that have nothing to do with
    entities being more informative -- the same mechanism can make the coherence gate look
    like it fuses two unrelated communities. Comparing arms at matched granularity removes it.

    Community count rises with resolution, so this is a bisection. The count is a step
    function, so an exact hit is not guaranteed; the closest reachable count is returned,
    ties broken toward the lower resolution for reproducibility.
    """
    best: "tuple[int, float, int] | None" = None   # (distance, resolution, count)

    def probe(res: float) -> int:
        nonlocal best
        count = level0_count(partition(mention, knn, entity_alpha=entity_alpha,
                                       resolution=res, **detect_kwargs))
        dist = abs(count - target)
        if best is None or (dist, res) < (best[0], best[1]):
            best = (dist, res, count)
        return count

    probe(lo)
    probe(hi)
    for _ in range(max_iter):
        if best is not None and best[0] == 0:
            break
        mid = (lo + hi) / 2
        count = probe(mid)
        if count < target:
            lo = mid
        elif count > target:
            hi = mid
        else:
            break
    assert best is not None
    return best[1], best[2]
