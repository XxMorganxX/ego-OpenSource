#!/usr/bin/env python3
# evals/retrieval/report.py
"""Fold results/2x2/*.json into the grid, applying the gates:
MIXED labelling, byte-identical verification, cross-cell flat_displaced_gold, and
gold_concept_coverage. Prints markdown for the benchmark report.

    cd KB_MCP && uv run python ../evals/retrieval/report.py \
        [--dir results/2x2] [--reference results/<ref>.json] [--k 20]"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).parent))
import bench_metrics as bm

DSN = os.environ.get("KB_DATABASE_URL", "postgresql://kb_app@localhost:5433/kb_mcp")
SETS = {"flat": "golden.jsonl", "package": "golden_package.jsonl"}


def _eval_data(rel: str, default: Path) -> Path:
    root = os.environ.get("KB_EVAL_DATA_DIR")
    return Path(root) / rel if root else default
# hybrid cells must precede crawl cells for the same set, so hybrid_tops is populated
# before a crawl cell's flat_displaced_gold cross-reference reads it.
# A cell added to bench_metrics.CELLS but not listed here is silently omitted from the grid.
CELL_ORDER = ["cell1", "cell2", "cell3-det", "cell3-ag", "cell4-det", "cell4-ag"]


def load_cell(dir_, cell):
    runs = sorted(dir_.glob(f"{cell}-r*.json"),
                  key=lambda p: int(re.search(r"-r(\d+)\.json$", p.name).group(1)))
    return [json.loads(p.read_text()) for p in runs]


def must_gold(set_name):
    path = _eval_data(f"retrieval/{SETS[set_name]}", Path(__file__).parent / SETS[set_name])
    out = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        it = json.loads(line)
        out[it["id"]] = {
            "must": [m["id"] for m in it["expected"].get("memories", [])
                     if m.get("must", True)],
            "may": [m["id"] for m in it["expected"].get("memories", [])
                    if not m.get("must", True)]}
    return out


def concept_coverage(all_must_ids):
    """gold_concept_coverage: fraction of must-gold ids that are a
    memory member of >=1 ACTIVE concept."""
    with psycopg.connect(DSN) as conn:
        covered = {str(r[0]) for r in conn.execute(
            "SELECT DISTINCT cm.node_id FROM concept_members cm "
            "JOIN concepts c ON c.id = cm.concept_id AND c.status = 'active' "
            "WHERE cm.member_kind = 'memory' AND cm.node_id = ANY(%s::uuid[])",
            (list(all_must_ids),))}
    return covered


def agg(items, key):
    vals = [i[key] for i in items if i.get(key) is not None]
    return round(sum(vals) / len(vals), 3) if vals else None


def cell_summary(runs, gold, k, hybrid_top_by_item=None):
    """One row of the grid from a cell's repeat files."""
    first = runs[0]["items"]
    top_maps = [{i["id"]: i["top_ids"] for i in r["items"]} for r in runs]
    traces = [c["trace_lite"] for r in runs for i in r["items"]
              for c in i["calls"] if c["tool"] == "search_graph"]
    latencies = sorted(c["latency_ms"] for r in runs for i in r["items"]
                       for c in i["calls"])
    costs = [c.get("cost") or {} for r in runs for i in r["items"]
             for c in i["calls"] if c["tool"] == "search_graph"]
    per_repeat_recall = [agg(r["items"], "must_recall") for r in runs]
    prec = [bm.precision_at_k(i["top_ids"], gold[i["id"]]["must"],
                              gold[i["id"]]["may"], k) for i in first]
    out = {
        "must_recall_per_repeat": per_repeat_recall,
        "must_recall_mean": round(sum(x for x in per_repeat_recall if x is not None)
                                  / len(per_repeat_recall), 3),
        "must_recall_spread": (round(max(per_repeat_recall) - min(per_repeat_recall), 3)
                               if len(per_repeat_recall) > 1 else 0.0),
        "n_repeats": len(runs),
        "precision_mean": round(sum(p["precision"] for p in prec) / len(prec), 3),
        "returned_mean": round(sum(p["returned"] for p in prec) / len(prec), 1),
        "neg_hits": sum(i.get("neg_hits", 0) for i in first),
        "latency_ms_p50": latencies[len(latencies) // 2] if latencies else None,
        "latency_ms_p90": (latencies[int(len(latencies) * 0.9)] if latencies else None),
        "tokens_returned_mean": (round(sum(c.get("tokens_returned", 0) for c in costs)
                                       / len(costs), 1) if costs else None),
        "llm_calls": sum(c.get("llm_calls", 0) for c in costs),
        "provider_tokens": sum(c["provider_tokens"] for c in costs
                               if c.get("provider_tokens")) or None,
        "degeneration": bm.degeneration(traces) if traces else None,
    }
    judge = runs[0]["stamp"]["cell"].get("judge")
    if judge == "llm":
        frac = bm.agentic_fraction(traces)
        out["agentic_fraction"] = frac
        out["label"] = bm.cell_agentic_label(frac)
    else:
        same, diffs = bm.byte_identical(top_maps)
        out["byte_identical"] = same
        if not same:
            out["diverging_items"] = diffs
    if hybrid_top_by_item:
        displaced = [bm.flat_displaced_gold(hybrid_top_by_item.get(i["id"], []),
                                            i["top_ids"], gold[i["id"]]["must"], k)
                     for i in first]
        out["flat_displaced_gold"] = sum(len(d) for d in displaced)
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dir", default=str(Path(__file__).parent / "results" / "2x2"))
    p.add_argument("--reference", default=None,
                   help="run.py result file for the legacy anchor row")
    p.add_argument("--k", type=int, default=20)
    args = p.parse_args()
    d = Path(args.dir)

    print("# 2x2 grid\n")
    hybrid_tops = {}
    for cell in CELL_ORDER:
        runs = load_cell(d, cell)
        if not runs:
            print(f"## {cell}: NOT RUN")
            continue
        set_name = bm.CELLS[cell]["set"]
        gold = must_gold(set_name)
        ref = hybrid_tops.get(set_name)
        s = cell_summary(runs, gold, args.k, hybrid_top_by_item=ref)
        if bm.CELLS[cell]["strategy"] == "hybrid":
            hybrid_tops[set_name] = {i["id"]: i["top_ids"] for i in runs[0]["items"]}
        print(f"## {cell} ({set_name}, {bm.CELLS[cell]['strategy']}, "
              f"judge={bm.CELLS[cell]['judge']})")
        print(json.dumps(s, indent=1), "\n")
        all_must = {m for g in gold.values() for m in g["must"]}
        covered = concept_coverage(all_must)
        print(f"gold_concept_coverage[{set_name}]: {len(covered)}/{len(all_must)}\n")
    if args.reference:
        ref = json.loads(Path(args.reference).read_text())
        print(f"## reference (legacy plans via search_memories — NOT part of the 2x2)\n"
              f"overall must_recall@{ref['k']}: {ref['overall_must_recall']} | "
              f"MRR {ref['mrr']} (file: {args.reference})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
