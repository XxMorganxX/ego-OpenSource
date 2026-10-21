"""Score NAMED counterfactual variants of search_memories against the gold sets, write-free.

    ../../KB_Core/.venv/bin/python shadow_bench.py --sets authored,traffic \\
        --variants baseline,p1,p1_bm25_orIE,p1_bm25_orIE_noaccess [--k 20] \\
        [--tier adjudicated|single-annotator|all] [--out results/shadow] [--refresh-df] \\
        [--max-embeds 50]

Sets
  authored  $KB_EVAL_DATA_DIR/retrieval/golden.jsonl, run.py semantics: the plan's single
            search_memories step (query = the item's request when the step has none), gold =
            `must` ids, dead gold KEPT (include_dead defaults True), ids absent from `nodes`
            dropped. Reports must_recall@10/@20/@30, MRR@20, and the exact ids each variant
            recovers / displaces at --k relative to `baseline`.
  traffic   $KB_EVAL_DATA_DIR/retrieval/traffic_gold.jsonl (see shadow.load_traffic_gold),
            --tier adjudicated by default; non-search_memories items are skipped (reported).
            Replayed with the trace's own arguments and `asof` = its timestamp (nodes that did
            not exist then -- shadow.admissible_at -- are excluded from every arm, the probe,
            and the denominators). Reports grade-2 GROUP recall at the trace's own k (pooled,
            capped-at-k, per-query mean), pooled recall@20, graded nDCG@k, grade-1 recall@k,
            recovered/displaced groups, and the hub / short-note share of the top-k. Items
            with no admissible grade-2 gold are left out of the per-query means (named).

Every results JSON records what decides a result: git HEAD + a hash of the uncommitted
KB_Core/src diff + the harness files' hashes, the gold files' hashes, the policy file, the
database, the DF snapshot, the live embedding model and the command line. Compare two runs
only when those match. Exit codes: 0 ran, 2 could not run (shadow.run_cli).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import shadow as S  # noqa: E402

RETRIEVE = 50   # fused order is identical for every limit <= HYBRID_ARM_POOL; slice per k
AUTHORED_LIMIT = 20   # run.py's limit = max(k, 20): the `or_few_k` an authored item runs with

_OIE = {"or_max_words": 10_000}                 # production fallback, no length cap
_BM25 = {"fts_score": "bm25"}                   # rarity-weighted OR arm (AND arm untouched)
_ENT = {"w_ent": 0.5, "ent_caps_only": True}    # entity arm on proper-noun anchors

# ------------------------------------------------------------------ the variant registry
# name -> {"knobs": {...}, "note": why it exists}. Knob keys are shadow.KNOB_DEFAULTS keys
# with allowed values (shadow.validate_knobs) -- the bench always forces the replica path, so
# a production constant (shadow.PROD_CONSTANT_KNOBS) is NOT allowed here; use its replica
# twin (rrfk / pool / fanout / or_max_words). tests/test_shadow.py checks the registry.
VARIANTS = {
    "baseline": {"knobs": {}, "note": "production as shipped: instructed query vector (proven == prod)"},
    "bare": {"knobs": {"query_prefix": S.BARE}, "note": "production before the query instruction shipped: raw query vector"},
    # --- query-side instruction (vector only)
    "p1": {"knobs": {"query_prefix": S.PREFIX_P1}, "note": "alias of baseline since the instruction shipped"},
    "p2": {"knobs": {"query_prefix": S.PREFIX_P2}, "note": "wording alternate: personal notes"},
    "p3": {"knobs": {"query_prefix": S.PREFIX_P3}, "note": "wording alternate: knowledge base"},
    "p1_knn_only": {"knobs": {"query_prefix": S.PREFIX_P1, "knn_only": True}, "note": "p1 with the lexical arm removed"},
    "p1_exact": {"knobs": {"query_prefix": S.PREFIX_P1, "exact": True}, "note": "p1 with exhaustive vector scans (ANN ceiling)"},
    # --- lexical arm (bare query vector -- these predate the instruction and pair with
    # the p1_* variants below, which stack the same lever on the instructed vector)
    "or_if_empty": {"knobs": {"query_prefix": S.BARE, **_OIE}, "note": "OR fallback at any query length (bare query vector)"},
    "or_always": {"knobs": {"query_prefix": S.BARE, "fts_mode": "or_all"}, "note": "OR form always (ts_rank, bare query vector)"},
    "bm25_or_only": {"knobs": {"query_prefix": S.BARE, **_BM25}, "note": "BM25-scored OR fallback, production gate (bare query vector)"},
    "bm25_orIE": {"knobs": {"query_prefix": S.BARE, **_BM25, **_OIE}, "note": "BM25 OR arm + any-length fallback (bare query vector)"},
    "or_if_few_scoped_bm25": {"knobs": {"query_prefix": S.BARE, "fts_mode": "or_if_few_scoped", **_BM25}, "note": "fallback when AND matches < k inside the scope (bare query vector)"},
    "lexnorm_both": {"knobs": {"query_prefix": S.BARE, "lexnorm": "both", **_OIE}, "note": "unaccent docs + split -/_ in the query (on the fly; slow; bare query vector)"},
    # --- fusion / arms
    "p1_bm25": {"knobs": {"query_prefix": S.PREFIX_P1, **_BM25}, "note": "p1 + BM25 OR arm, production gate"},
    "p1_bm25_orIE": {"knobs": {"query_prefix": S.PREFIX_P1, **_BM25, **_OIE}, "note": "p1 + BM25 OR arm + any-length fallback"},
    "p1_bm25_orIE_noaccess": {"knobs": {"query_prefix": S.PREFIX_P1, **_BM25, **_OIE, "importance": "no_access"},
                              "note": "FINALIST: + access term frozen at 0 in the FTS multiplier"},
    "finalist_dedup": {"knobs": {"query_prefix": S.PREFIX_P1, **_BM25, **_OIE, "importance": "no_access", "dedup": 0.97},
                       "note": "finalist + near-duplicate collapse (cos >= 0.97) in the fused pool"},
    "p1_or_always_bm25": {"knobs": {"query_prefix": S.PREFIX_P1, "fts_mode": "or_all", **_BM25}, "note": "p1 + BM25 OR form always"},
    "entity_w05": {"knobs": {"query_prefix": S.BARE, **_ENT}, "note": "entity-anchored third RRF list, weight 0.5 (bare query vector)"},
    "p1_entity_w05": {"knobs": {"query_prefix": S.PREFIX_P1, **_ENT}, "note": "p1 + entity arm"},
    # --- single levers (bare query vector -- see the "lexical arm" note above)
    "dedup097": {"knobs": {"query_prefix": S.BARE, "dedup": 0.97}, "note": "near-duplicate collapse only (bare query vector)"},
    "knn_only": {"knobs": {"query_prefix": S.BARE, "knn_only": True}, "note": "vector arm alone (bare query vector)"},
    "importance_off": {"knobs": {"query_prefix": S.BARE, "importance": "off"}, "note": "FTS arm without the importance multiplier (bare query vector)"},
    "no_access": {"knobs": {"query_prefix": S.BARE, "importance": "no_access"}, "note": "access modifier frozen at 0, other modifiers kept (bare query vector)"},
    "stub_guard": {"knobs": {"query_prefix": S.BARE, "stub_guard": "always"}, "note": "whole-note similarity x0.9 for notes under 700 chars (bare query vector)"},
    "exact_scan": {"knobs": {"query_prefix": S.BARE, "exact": True}, "note": "exhaustive vector scans (ANN ceiling, bare query vector)"},
    "pool_200": {"knobs": {"query_prefix": S.BARE, "pool": 200}, "note": "arm pools x4 (bare query vector)"},
}
REQUIRED_VARIANTS = ("baseline", "bare", "p1", "p2", "p3", "p1_bm25_orIE", "p1_bm25_orIE_noaccess",
                     "finalist_dedup", "bm25_orIE", "entity_w05", "p1_entity_w05", "or_always",
                     "dedup097", "knn_only", "p1_knn_only")


def knobs_for(name: str, k: int) -> dict:
    kn = dict(VARIANTS[name]["knobs"])
    if kn.get("fts_mode") == "or_if_few_scoped" and kn.get("or_few_k") is None:
        kn["or_few_k"] = k
    return kn


# ------------------------------------------------------------------ authored set
def score_authored(name: str, items: list, missing: set) -> dict:
    per = {}
    for it, a, must in items:
        a = dict(a); a["limit"] = RETRIEVE
        r = S.shadow_search(overrides=knobs_for(name, AUTHORED_LIMIT), path="replica", **a)
        gold = [m for m in must if m not in missing]
        if not gold:
            continue
        fh = S.first_hit_rank(r["ids"], gold, 20)
        per[it["id"]] = {"ids": r["ids"][:30], "gold": gold, "r10": S.must_recall(r["ids"], gold, 10),
                         "r20": S.must_recall(r["ids"], gold, 20), "r30": S.must_recall(r["ids"], gold, 30),
                         "rr20": 1 / fh if fh else 0.0, "ms": r["latency_ms"], "or_fired": r["or_fired"]}
    return per


def authored_summary(per: dict, base: dict | None, k: int) -> dict:
    rec, dis = [], []
    if base:
        for iid, v in per.items():
            b = base.get(iid)
            if not b:
                continue
            for m in v["gold"]:
                now, was = m in v["ids"][:k], m in b["ids"][:k]
                if now and not was:
                    rec.append(f"{iid}:{m}")
                if was and not now:
                    dis.append(f"{iid}:{m}")
    return {"n": len(per), "r10": S.mean(v["r10"] for v in per.values()), "r20": S.mean(v["r20"] for v in per.values()),
            "r30": S.mean(v["r30"] for v in per.values()), "mrr20": S.mean(v["rr20"] for v in per.values()),
            "recovered": rec, "displaced": dis, "ms": S.mean(v["ms"] for v in per.values())}


# ------------------------------------------------------------------ traffic set
def traffic_admissible(kb, items: list) -> dict:
    """(item id, member id) -> admissible: exists in nodes AND existed at the trace's time
    (shadow.admissible_at, the same rule -- and the same hidden set -- the replica's as-of
    clause applies to every arm)."""
    ids = {m["id"] for it in items for m in it["expected"].get("memories", [])}
    meta = S.node_meta(kb, sorted(ids))
    out = {}
    for it in items:
        hidden = set(S.asof_hidden_ids(kb, it["at"]))
        for m in it["expected"].get("memories", []):
            out[(it["id"], m["id"])] = m["id"] in meta and m["id"] not in hidden
    return out


def item_admissible(it: dict, adm: dict) -> dict:
    return {m["id"]: adm[(it["id"], m["id"])] for m in it["expected"].get("memories", [])}


def score_traffic(name: str, items: list, adm: dict, hubs: set, short: set) -> dict:
    per = {}
    for it in items:
        args = {kk: v for kk, v in it["args"].items() if kk != "query"}
        k = int(args.get("limit") or S.DEFAULT_LIMIT)
        a = dict(args); a["limit"] = RETRIEVE
        kn = knobs_for(name, k); kn["asof"] = it["at"]
        r = S.shadow_search(it["query"], overrides=kn, path="replica", **a)
        g2, gains, member_group, g1 = S.traffic_groups(it, item_admissible(it, adm))
        ids = r["ids"]
        hits_k = [g for g, mem in g2.items() if any(m in ids[:k] for m in mem)]
        hits_20 = [g for g, mem in g2.items() if any(m in ids[:20] for m in mem)]
        g1_hits = [g for g, mem in g1.items() if any(m in ids[:k] for m in mem)]
        per[it["id"]] = {"k": k, "tier": it["tier"], "ids": ids[:20], "n_g2": len(g2), "n_g1": len(g1),
                         "hits_k": sorted(hits_k), "hits_20": sorted(hits_20), "g1_hits_k": sorted(g1_hits),
                         "capped": S.capped_recall(ids, g2, k), "ndcg": S.graded_ndcg(ids, gains, member_group, k),
                         "hub_k": sum(1 for i in ids[:k] if i in hubs) / k,
                         "short_k": sum(1 for i in ids[:k] if i in short) / k,
                         "ms": r["latency_ms"], "or_fired": r["or_fired"]}
        if "or_few_k" in kn:
            per[it["id"]]["or_few_k"] = kn["or_few_k"]          # what ran: the trace's own k
    return per


def traffic_summary(per: dict, base: dict | None) -> dict:
    """Pooled figures sum over every item; per-query means use only items with admissible
    grade-2 gold (`no_gold` names the rest) -- an item with nothing to find would otherwise
    enter capped recall / nDCG / shares as a 0.0 or a meaningless share."""
    scored = {t: v for t, v in per.items() if v["n_g2"]}
    n_g2 = sum(v["n_g2"] for v in per.values()); n_g1 = sum(v["n_g1"] for v in per.values())
    rec, dis = [], []
    if base:
        for tid, v in per.items():
            b = base.get(tid)
            if not b:
                continue
            rec += [f"{tid}:{g}" for g in v["hits_k"] if g not in b["hits_k"]]
            dis += [f"{tid}:{g}" for g in b["hits_k"] if g not in v["hits_k"]]
    return {"n": len(per), "n_scored": len(scored), "no_gold": sorted(set(per) - set(scored)),
            "n_g2_groups": n_g2,
            "pooled_k": sum(len(v["hits_k"]) for v in per.values()) / n_g2 if n_g2 else 0.0,
            "capped_k": S.mean(v["capped"] for v in scored.values()),
            "mean_k": S.mean(len(v["hits_k"]) / v["n_g2"] for v in scored.values()),
            "pooled_20": sum(len(v["hits_20"]) for v in per.values()) / n_g2 if n_g2 else 0.0,
            "ndcg": S.mean(v["ndcg"] for v in scored.values()),
            "g1_k": sum(len(v["g1_hits_k"]) for v in per.values()) / n_g1 if n_g1 else 0.0,
            "recovered": rec, "displaced": dis,
            "hub": S.mean(v["hub_k"] for v in scored.values()),
            "short": S.mean(v["short_k"] for v in scored.values()),
            "ms": S.mean(v["ms"] for v in per.values())}


def uses_df(knobs: dict) -> bool:
    """Whether a variant reads the DF cache (BM25 / idf scoring, or OR-form coverage)."""
    return (knobs.get("fts_score", "prod") != "prod" or knobs.get("or_min_cov") is not None
            or knobs.get("or_rank", "rank") != "rank")


# ------------------------------------------------------------------ CLI
def main(argv=None) -> int:
    return S.run_cli("shadow_bench", _main, argv)


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sets", default="authored,traffic")
    ap.add_argument("--variants", default="baseline,p1,p1_bm25_orIE,p1_bm25_orIE_noaccess")
    ap.add_argument("--k", type=int, default=20, help="headline k for authored recovered/displaced")
    ap.add_argument("--tier", default="adjudicated", choices=["adjudicated", "single-annotator", "all"],
                    help="traffic tier (default adjudicated)")
    ap.add_argument("--out", default=None, help=f"results dir (default {RESULTS_DIR_REL})")
    ap.add_argument("--refresh-df", action="store_true",
                    help="rebuild the lexeme DF cache from ts_stat, then run the bench as usual")
    ap.add_argument("--max-embeds", type=int, default=50,
                    help="budget of FRESH embedding calls (checked before any search)")
    ap.add_argument("--list", action="store_true", help="list the variant registry and exit")
    a = ap.parse_args(argv)
    if a.list:
        for name, v in VARIANTS.items():
            print(f"{name:26s} {v['note']}\n{'':26s} {v['knobs']}")
        return 0
    names = [n.strip() for n in a.variants.split(",") if n.strip()]
    unknown = [n for n in names if n not in VARIANTS]
    if unknown:
        raise SystemExit(f"unknown variants {unknown}; see --list")
    sets = [s.strip() for s in a.sets.split(",") if s.strip()]
    kb = S.kb()
    print("readonly:", S.assert_readonly(kb))
    S.embedder().max_calls = a.max_embeds
    if a.refresh_df:
        d = S.build_df_cache(kb); S._DF = d
        print(f"DF cache rebuilt: {len(d['df'])} lexemes, N={d['N']}, avglen={d['avglen']:.0f}")
    out_dir = Path(a.out) if a.out else S.RESULTS_DIR
    authored = traffic = None
    missing = set()
    if "authored" in sets:
        authored = S.authored_search_items(S.load_authored_gold())
        ids = {m for _, _, must in authored for m in must}
        missing = ids - set(S.node_meta(kb, sorted(ids)))
        print(f"authored: {len(authored)} search items, {len(ids)} must ids, {len(missing)} absent from nodes (dropped)")
    if "traffic" in sets:
        skipped = []
        traffic = S.load_traffic_gold(a.tier, skipped=skipped)
        adm = traffic_admissible(kb, traffic)
        hubs, short = S.corpus_flags(kb)
        print(f"traffic: {len(traffic)} items (tier={a.tier}); members admissible at trace time: "
              f"{sum(adm.values())}/{len(adm)}")
        if skipped:
            print(f"  skipped {len(skipped)} non-search_memories items (the replica models "
                  f"search_memories only): {skipped}")
        no_gold = [it["id"] for it in traffic if not S.traffic_groups(it, item_admissible(it, adm))[0]]
        if no_gold:
            print(f"  warning: {len(no_gold)} items have no admissible grade-2 gold; left out of "
                  f"the per-query means: {no_gold}")
    queries = ([a_["query"] for _, a_, _ in authored] if authored else []) + \
              ([it["query"] for it in traffic] if traffic else [])
    need = S.check_embed_budget([(q, VARIANTS[n]["knobs"].get("query_prefix", S.QUERY_INSTRUCTION))
                                 for n in names for q in queries], a.max_embeds)
    df_info = None
    if any(uses_df(VARIANTS[n]["knobs"]) for n in names):
        S.df_cache(kb)   # builds it on a fresh clone, so the snapshot is recorded below
        df_info = S.df_cache_info(kb)
    if df_info:
        print(f"DF cache: N={df_info['N']} (live {df_info['live_N']}), {df_info['lexemes']} lexemes, "
              f"built_at={df_info['built_at']}, sha256={df_info['sha256'][:12]}")
        if df_info["N"] != df_info["live_N"]:
            print(f"  warning: the DF cache is stale (N={df_info['N']} vs {df_info['live_N']} live "
                  "notes); BM25 per-item ranks depend on it -- --refresh-df to rebuild")
    emb = S.embedder()
    gold_dir = S.eval_data_dir() / "retrieval"
    result = {"started_at": datetime.now(timezone.utc).isoformat(), "git": S.git_state(),
              "env": {"eval_data_dir": str(S.eval_data_dir()), "inference_url": emb.url,
                      "embed_model": emb.model, "embed_model_live": emb.live_model,
                      "embed_model_cache": emb.cache_model, "tier": a.tier, "k": a.k,
                      "gold": {"authored": S.file_digest(gold_dir / "golden.jsonl") if authored else None,
                               "traffic": S.file_digest(gold_dir / "traffic_gold.jsonl") if traffic else None},
                      "df_cache": df_info, "fresh_embeds_needed": need,
                      **S.run_provenance(argv)},
              "sets": sets, "variants": {}}
    out_dir.mkdir(parents=True, exist_ok=True)
    hdr = f"{'variant':26s}"
    if authored:
        hdr += f" | {'a@10':>5} {'a@20':>5} {'a@30':>5} {'MRR':>5} {'+/-@k':>6}"
    if traffic:
        hdr += f" | {'pool@k':>6} {'cap@k':>5} {'mean':>5} {'R@20':>5} {'nDCG':>5} {'g1@k':>5} {'+/-k':>6} {'hub':>4} {'shrt':>4}"
    hdr += f" | {'ms':>5}"
    print(hdr)
    base_a = base_t = None
    for name in names:
        t0 = time.time()
        # the registry entry; the k-dependent fill (or_few_k) actually used is recorded per set
        row = {"knobs": dict(VARIANTS[name]["knobs"]), "note": VARIANTS[name]["note"]}
        line = f"{name:26s}"
        if authored:
            per = score_authored(name, authored, missing)
            if base_a is None:
                base_a = per
            summ = authored_summary(per, base_a, a.k)
            row["authored"] = {"summary": summ, "knobs": knobs_for(name, AUTHORED_LIMIT), "items": per}
            line += (f" | {summ['r10']:5.3f} {summ['r20']:5.3f} {summ['r30']:5.3f} {summ['mrr20']:5.3f} "
                     f"{('+%d/-%d' % (len(summ['recovered']), len(summ['displaced']))):>6}")
        if traffic:
            per = score_traffic(name, traffic, adm, hubs, short)
            if base_t is None:
                base_t = per
            summ = traffic_summary(per, base_t)
            row["traffic"] = {"summary": summ, "items": per}
            line += (f" | {summ['pooled_k']:6.3f} {summ['capped_k']:5.3f} {summ['mean_k']:5.3f} {summ['pooled_20']:5.3f} "
                     f"{summ['ndcg']:5.3f} {summ['g1_k']:5.3f} {('+%d/-%d' % (len(summ['recovered']), len(summ['displaced']))):>6} "
                     f"{summ['hub']:4.2f} {summ['short']:4.2f}")
        ms = S.mean(v["ms"] for key in ("authored", "traffic") if key in row for v in row[key]["items"].values())
        line += f" | {ms:5.0f}"
        row["wall_s"] = time.time() - t0
        result["variants"][name] = row
        print(line, flush=True)
    print("\nrecovered(+) / displaced(-) vs the first variant")
    for name, row in result["variants"].items():
        for key in ("authored", "traffic"):
            if key in row and (row[key]["summary"]["recovered"] or row[key]["summary"]["displaced"]):
                print(f"  {name:26s} {key:8s} +{row[key]['summary']['recovered']} -{row[key]['summary']['displaced']}")
    result["fresh_embed_calls"] = S.embedder().calls
    out_path = out_dir / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    with open(out_path, "w") as fh:
        json.dump(result, fh, indent=1, default=str)
    print(f"\nwrote {out_path.relative_to(S.REPO_ROOT) if out_path.is_relative_to(S.REPO_ROOT) else out_path} "
          f"| fresh embed calls {S.embedder().calls} | HEAD {result['git']['head'][:12]}")
    return 0


RESULTS_DIR_REL = "evals/retrieval/results/shadow/"

if __name__ == "__main__":
    sys.exit(main())
