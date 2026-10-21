#!/usr/bin/env python3
"""Sweep the entity:embedding mixing ratio and score each arm by co-membership lift.

Read-only and LLM-free. Reads one pinned snapshot, rebuilds and re-partitions the
consolidation graph in-process per arm, and scores against the golden set's co-membership
assertions. Never calls run_once, never writes.

    uv run --project KB_Core python -m evals.concepts.sweep \
        --snapshot evals/concepts/results/snapshot.json \
        --out evals/concepts/results/sweep.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from kb_core.concepts import mention_edge_weight

from evals.concepts.arms import (blocked_mention_rows, level0_count, level0_sizes,
                                 match_resolution, partition)
from evals.concepts.co_membership import membership, missed, score
from evals.concepts.golden_pairs import authored_pairs, usable_pairs


def _eval_data(rel: str, default: Path) -> Path:
    root = os.environ.get("KB_EVAL_DATA_DIR")
    return Path(root) / rel if root else default


ALPHAS = [0.0, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 4.0]
BLOCKLIST = ["as-is", "restored"]
VARIANTS = ["raw", "granularity-matched"]
N_PERM = 2000
SEED = 42

# Recorded in every sweep.json so the caveat travels with the numbers, not just this file's
# comments -- the report reads sweep.json, not this module.
CAVEATS = [
    "entity_alpha=0 zeroes mention-edge WEIGHT, but it is not structural ablation: "
    "build_graph registers an entity vertex and a zero-weight edge for every mention row "
    "before any weight check runs, so entity vertices stay in the graph and shift the "
    "name-sorted canonical vertex order Leiden's local-moving visits by. That is why the "
    "as-is and restored blocklist settings disagree at alpha=0 despite both carrying "
    "exactly zero entity edge weight.",
    "Consequently this sweep has a vertex-ordering noise floor of roughly one usable golden "
    "pair of pos_recall -- arm-to-arm deltas at or below that magnitude "
    "are not interpretable as real effects, only as which local optimum Leiden happened to "
    "land in.",
    "The true_ablation arms (partition([], knn, ...) -- no mention rows, so no entity "
    "vertices at all) are the structurally comparable counterpart to a 'drop all "
    "entities' measurement. alpha=0 is not a substitute for them.",
    "Post-decay (content-age decay) snapshots embed now()-relative "
    "decayed weight/age_factor values baked in at snapshot time, so a sweep comparing "
    "arms across snapshot vintages is comparing different weight semantics even when the "
    "underlying content is identical -- pin sweeps to a single snapshot.",
]


def _deduped_knn(knn: "list[dict]") -> "dict[tuple[str, str], float]":
    """Canonical-ordered pair key, keep the max sim -- mirrors build_graph's own dedup
    exactly (see build_graph's knn loop in kb_core.concepts). memory_knn_edges returns one
    row PER DIRECTION, so summing raw rows double-counts every mutual pair, and mutual
    pairs are common. Do not reimplement more of build_graph than this dedup step."""
    weights: "dict[tuple[str, str], float]" = {}
    for e in knn:
        a, b = sorted((str(e["src"]), str(e["dst"])))
        weights[(a, b)] = max(weights.get((a, b), 0.0), float(e["sim"]))
    return weights


def entity_share(mention, knn, alpha) -> float:
    """Fraction of total graph weight carried by mention edges at this alpha."""
    m = sum(mention_edge_weight(e["weight"], e["support"], alpha=alpha) for e in mention)
    k = sum(_deduped_knn(knn).values())
    return m / (m + k) if (m + k) else 0.0


def run(snapshot: dict, *, snapshot_sha256: str, n_perm: int = N_PERM) -> dict:
    live = set(snapshot["live_ids"])
    pairs = sorted(usable_pairs(authored_pairs(), live))
    universe = snapshot["live_ids"]
    knn = snapshot["knn"]
    mention_by_blocklist = {
        "as-is": snapshot["mention"],
        "restored": snapshot["mention"] + blocked_mention_rows(
            snapshot["blocked_candidates"], live,
            min_memories=snapshot["params"]["blocked_min_memories"]),
    }

    base_comms = partition(snapshot["mention"], knn, entity_alpha=1.0, resolution=1.0)
    target = level0_count(base_comms)
    base_memb = membership(base_comms)
    # _LIVE_SQL (snapshot.py) pins `universe` to type='note' rows. build_graph's memory
    # vertices are whatever the mention/kNN edges reference -- nothing enforces that the
    # two agree. If they ever diverge, co_membership's null model silently mismaps the
    # stray id (identity fallback in `_null_samples`), biasing the null rather than
    # failing. Fail loudly if they ever diverge.
    assert base_memb.keys() <= set(universe), (
        "membership includes memory ids outside the pinned live-id universe -- "
        "_LIVE_SQL's type='note' filter no longer covers everything the graph treats "
        "as a memory")
    baseline = score(pairs, base_memb, universe, n_perm=n_perm, seed=SEED)
    baseline["n_concepts"] = target
    baseline["level0_sizes"] = level0_sizes(base_comms)
    baseline["missed_pairs"] = missed(pairs, base_memb)

    arms = []
    for blocklist in BLOCKLIST:
        mention = mention_by_blocklist[blocklist]
        for alpha in ALPHAS:
            for variant in VARIANTS:
                if variant == "raw":
                    resolution = 1.0
                else:
                    resolution, _ = match_resolution(mention, knn, target=target,
                                                     entity_alpha=alpha)
                comms = partition(mention, knn, entity_alpha=alpha, resolution=resolution)
                memb = membership(comms)
                row = score(pairs, memb, universe, n_perm=n_perm, seed=SEED)
                row.update(alpha=alpha, blocklist=blocklist, variant=variant,
                           resolution=resolution, n_concepts=level0_count(comms),
                           level0_sizes=level0_sizes(comms),
                           entity_share=entity_share(mention, knn, alpha),
                           missed_pairs=missed(pairs, memb), true_ablation=False)
                arms.append(row)
                print(f"a={alpha:<5} {blocklist:<8} {variant:<20} "
                      f"res={resolution:.3f} n={row['n_concepts']:<3} "
                      f"recall={row['pos_recall']:.3f} lift={row['lift']:.3f}", flush=True)

    # True structural ablation, distinct from alpha=0: no mention rows at all means no
    # entity vertices and no zero-weight edges, so this is not subject to the vertex-order
    # noise floor above -- it is the counterpart a "drop all entities" figure needs for a
    # fair comparison. entity_alpha is irrelevant with an empty mention list
    # (there is nothing for it to scale) so its value here is arbitrary.
    for variant in VARIANTS:
        if variant == "raw":
            resolution = 1.0
        else:
            resolution, _ = match_resolution([], knn, target=target, entity_alpha=1.0)
        comms = partition([], knn, entity_alpha=1.0, resolution=resolution)
        memb = membership(comms)
        row = score(pairs, memb, universe, n_perm=n_perm, seed=SEED)
        row.update(alpha=None, blocklist="n/a", variant=variant, resolution=resolution,
                   n_concepts=level0_count(comms), level0_sizes=level0_sizes(comms),
                   entity_share=0.0,
                   missed_pairs=missed(pairs, memb), true_ablation=True)
        arms.append(row)
        print(f"a=None  n/a      {variant:<20} "
              f"res={resolution:.3f} n={row['n_concepts']:<3} "
              f"recall={row['pos_recall']:.3f} lift={row['lift']:.3f}  [true ablation]",
              flush=True)

    return {"snapshot": snapshot["params"] | {"captured_at": snapshot["captured_at"]},
            "snapshot_sha256": snapshot_sha256, "caveats": CAVEATS,
            "n_pairs": len(pairs), "baseline": baseline, "arms": arms}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--snapshot", default=str(_eval_data(
        "concepts/results/snapshot.json", Path("evals/concepts/results/snapshot.json"))))
    p.add_argument("--out", default="evals/concepts/results/sweep.json")
    p.add_argument("--n-perm", type=int, default=N_PERM,
                   help="Null permutations per arm (default 2000). Lower for smoke tests -- "
                        "a forgotten low value would silently produce a low-precision null "
                        "across all 34 arms, so this is a flag, not a module constant to "
                        "hand-edit.")
    args = p.parse_args()
    snapshot_bytes = Path(args.snapshot).read_bytes()
    snapshot_sha256 = hashlib.sha256(snapshot_bytes).hexdigest()
    result = run(json.loads(snapshot_bytes), snapshot_sha256=snapshot_sha256,
                 n_perm=args.n_perm)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, indent=2))
    print(f"\nwrote {args.out}  ({len(result['arms'])} arms, {result['n_pairs']} pairs)")


if __name__ == "__main__":
    main()
