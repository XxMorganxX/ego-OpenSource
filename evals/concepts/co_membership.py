"""Score a concept partition by pairwise co-membership against a relabelled null.

Headline statistic is LIFT, not raw recall:

    lift = pos_recall(partition) - mean pos_recall(relabelled partitions)

Lift is degeneracy-proof, which is the job the rejected negative arm was meant to do. A
single all-encompassing concept scores pos_recall 1.0 -- and its null scores 1.0 too, so
lift collapses to 0. A shattered partition scores near 0 on both. No separate precision
term is needed.

The null relabels memory identities across the corpus by a random bijection. It holds
community sizes and each memory's membership count exactly fixed, destroying only the
association between a specific memory and a specific community.
"""
from __future__ import annotations

import random
import statistics
from collections import defaultdict

MEMORY = "memory"


def membership(communities: "list[dict]") -> "dict[str, set[str]]":
    """Memory id -> set of community keys it belongs to. Entity members are ignored."""
    out: "dict[str, set[str]]" = defaultdict(set)
    for comm in communities:
        for node_id, kind, _score in comm["members"]:
            if kind == MEMORY:
                out[str(node_id)].add(comm["key"])
    return dict(out)


def _hits(pairs, memb: "dict[str, set[str]]") -> int:
    """Pairs whose two memories share at least one concept."""
    empty: "set[str]" = set()
    return sum(1 for a, b in pairs if memb.get(a, empty) & memb.get(b, empty))


def missed(pairs, memb: "dict[str, set[str]]") -> "list[list[str]]":
    """Pairs whose two memories share no concept -- the headroom. Same predicate as
    `_hits` (share-at-least-one-concept), exposed for callers that need the pair-level
    detail rather than just the count -- e.g. the sweep, which reports headroom pairs by
    id triple, not just as an aggregate."""
    empty: "set[str]" = set()
    return [[a, b] for a, b in pairs if not (memb.get(a, empty) & memb.get(b, empty))]


def pos_recall(pairs, memb: "dict[str, set[str]]") -> float:
    """Fraction of asserted pairs whose two memories share at least one concept."""
    if not pairs:
        return float("nan")
    return _hits(pairs, memb) / len(pairs)


def _null_samples(pairs, memb, universe, *, n_perm: int, seed: int) -> "list[float]":
    rng = random.Random(seed)
    items = list(memb.items())
    pool = sorted(universe)          # sorted: determinism does not survive set order
    out: "list[float]" = []
    for _ in range(n_perm):
        shuffled = pool[:]
        rng.shuffle(shuffled)
        mapping = dict(zip(pool, shuffled))
        relabelled = {mapping.get(node, node): keys for node, keys in items}
        out.append(pos_recall(pairs, relabelled))
    return out


def null_mean(pairs, memb, universe, *, n_perm: int = 2000, seed: int = 42) -> float:
    return statistics.fmean(_null_samples(pairs, memb, universe, n_perm=n_perm, seed=seed))


def score(pairs, memb, universe, *, n_perm: int = 2000, seed: int = 42) -> dict:
    """Full scorecard for one partition."""
    samples = _null_samples(pairs, memb, universe, n_perm=n_perm, seed=seed)
    mean = statistics.fmean(samples)
    sd = statistics.pstdev(samples)
    real = pos_recall(pairs, memb)
    return {
        "n_pairs": len(pairs),
        "hits": _hits(pairs, memb),
        "pos_recall": real,
        "null_mean": mean,
        "null_sd": sd,
        "null_max": max(samples),
        "lift": real - mean,
        "z": (real - mean) / sd if sd else float("nan"),
    }
