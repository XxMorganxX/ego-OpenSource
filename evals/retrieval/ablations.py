#!/usr/bin/env python3
# evals/retrieval/ablations.py
"""In-process knob sweeps (the crawl strategy's constructor knobs are not on the MCP surface —
deliberately). Bootstrap mirrors KB_Retrieval/scripts/prove_equivalence.py:
open_pool + KnowledgeBase(pool, default_registry()) + the live /embed endpoint +
scoped access-state restore.

Sweeps (all judge='none' — reach and ordering are structural; quota is spent only
by the 2x2 proper):
  flat_reserve {0,10}      on SET-FLAT and SET-PACKAGE   (two-point ablation)
  bridge_fanout {8,16}     on SET-PACKAGE
  ablate l_corrob          on SET-PACKAGE
  ablate label-cosine      on SET-PACKAGE                (the constrained entry family)
  ablate l_admit           — SKIPPED deterministically: l_admit is only populated by
                             agentic hops (gated per hop), so its deterministic
                             ablation is a no-op by construction; its agentic
                             ablation would spend provider quota and is listed in
                             the report as NOT RUN unless explicitly requested.
Also computes the in-sample honesty check on SET-PACKAGE: entry-family
top-1/top-3 accuracy (label-cosine vs evidence-vote), mirroring the
in-sample entry-family measurement — does the A-vs-C ordering reproduce out of sample? This is always computed
from the BASELINE sweep's entry trace (kw={}, no ablation) — `ablate={"label-cosine"}`
threads a skip_label_cosine flag into entry_step itself (crawl/entry.py), so under
that sweep the A rung never runs at all and its seeds never appear in the trace with
via="label-cosine"; concepts it would have claimed can still show up under
via="evidence-vote" if the C rung independently qualifies them. That sweep's own
entry trace is therefore not read here — entry_hit_rates only ever sees the
unablated baseline's seeds, so via values are read exactly as entry_step actually
emits them ("label-cosine" | "evidence-vote" | "name" | "fuzzy-name" |
"anchor_entity"), never post-filtered.

Scores must_recall@20 per configuration via an inline must_recall_at_k (run.py's
score_item is NOT importable here — see the comment below); writes
results/2x2/ablations.json.

    cd KB_Retrieval && KB_DATABASE_URL=postgres://kb_app:...@localhost:5433/kb_mcp \
        uv run python ../evals/retrieval/ablations.py
"""
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).parent


def _eval_data(rel: str, default: Path) -> Path:
    root = os.environ.get("KB_EVAL_DATA_DIR")
    return Path(root) / rel if root else default


# NOT `from run import score_item`: run.py imports `mcp` at module top and this
# script runs in KB_Retrieval's venv, which has no mcp. The one metric needed here
# is must_recall@20; its arithmetic is mirrored from run.py's score_item (three lines)
# and pinned by the smoke step against a run.py-scored item.
def must_recall_at_k(item, ordered_ids, k=20):
    must = [m["id"] for m in item["expected"].get("memories", [])
            if m.get("must", True)]
    if not must:
        return None
    top = ordered_ids[:k]
    return round(sum(m in top for m in must) / len(must), 3)

from kb_core.db import open_pool                          # noqa: E402
from kb_core.embedding import instruct_query               # noqa: E402
from kb_core.ingest.inference import HTTPInferenceClient   # noqa: E402
from kb_core.knowledge_base import KnowledgeBase           # noqa: E402
from kb_core.registry import default_registry              # noqa: E402
from kb_retrieval.contract import Query                     # noqa: E402
from kb_retrieval.registry import get_strategy               # noqa: E402

SETS = {"flat": _eval_data("retrieval/golden.jsonl", HERE / "golden.jsonl"),
        "package": _eval_data("retrieval/golden_package.jsonl", HERE / "golden_package.jsonl")}
SWEEPS = [
    {"name": "flat_reserve=0",  "set": "flat",    "kw": {"flat_reserve": 0}},
    {"name": "flat_reserve=10", "set": "flat",    "kw": {"flat_reserve": 10}},
    {"name": "flat_reserve=0",  "set": "package", "kw": {"flat_reserve": 0}},
    {"name": "flat_reserve=10", "set": "package", "kw": {"flat_reserve": 10}},
    {"name": "bridge_fanout=8",  "set": "package", "kw": {"bridge_fanout": 8}},
    {"name": "bridge_fanout=16", "set": "package", "kw": {"bridge_fanout": 16}},
    {"name": "ablate=l_corrob", "set": "package",
     "kw": {"ablate": frozenset({"l_corrob"})}},
    {"name": "ablate=label-cosine", "set": "package",
     "kw": {"ablate": frozenset({"label-cosine"})}},
    {"name": "baseline", "set": "package", "kw": {}},
]


def items_for(set_name):
    return [json.loads(line) for line in SETS[set_name].read_text().splitlines()
            if line.strip()]


def main():
    pool = open_pool(os.environ["KB_DATABASE_URL"])
    # KnowledgeBase's registry arg is NOT optional (no default) — mirrors
    # prove_equivalence.py's construction verbatim; a bare `KnowledgeBase(pool)`
    # would TypeError.
    kb = KnowledgeBase(pool, default_registry())
    print(f"policy: {kb.policy.source_path or 'shipped defaults (no policy.toml)'}")
    if kb.policy.is_default:
        print("WARNING: running on shipped defaults (no decay rules, no [projects]) "
              "— set KB_POLICY_CONFIG to mirror production")
    # The stack runs KB_Inference on :8001 (KB_INFERENCE_PORT; the dashboard holds
    # :8000) — pass KB_INFERENCE_URL to point anywhere else.
    embed = HTTPInferenceClient(
        os.environ.get("KB_INFERENCE_URL", "http://127.0.0.1:8001"), timeout=30)
    out = {"sweeps": [], "entry_families": None}
    with pool.connection() as conn:
        snapshot = conn.execute(
            "SELECT id, access_count, last_accessed_at FROM nodes").fetchall()
    try:
        vectors = {}
        for cfg in SWEEPS:
            unknown = set(cfg["kw"]) - {"flat_reserve", "bridge_fanout", "ablate"}
            if unknown:
                raise SystemExit(f"sweep {cfg['name']!r} has unknown knob keys "
                                 f"{sorted(unknown)} — the strategy's **_kw would "
                                 f"swallow them silently and the cell would report "
                                 f"as ablated while running un-knobbed")
            strat = get_strategy("cluster-crawl", kb, judge="none", **cfg["kw"])
            # Knob-swallow guard: guards against typo'd SWEEPS keys.
            # The whitelist above catches unknown knob keys in cfg["kw"] before
            # construction (a sweep config typo). These asserts below guard against
            # constructor-signature drift — if the strategy's __init__ signature
            # changes, these would still fail. Two different failure modes, both named.
            assert strat._flat_reserve == cfg["kw"].get("flat_reserve", 0), cfg
            assert strat._ablate == frozenset(cfg["kw"].get("ablate", ())), cfg
            assert strat._bridge_fanout == cfg["kw"].get("bridge_fanout"), cfg
            scores, entry_trace = [], []
            for it in items_for(cfg["set"]):
                vecs = vectors.get(it["id"])
                if vecs is None:
                    text = it["request"][:6000]
                    inst, bare = embed.embed([instruct_query(text)[:6000], text])
                    vecs = vectors[it["id"]] = (inst, bare)
                res = strat.search(Query(text=it["request"], embedding=vecs[0],
                                         label_embedding=vecs[1], limit=20))
                scores.append(must_recall_at_k(it, [str(r.id) for r in res.rows]))
                entry_trace.append({"id": it["id"], "entry": res.trace.entry})
            vals = [s for s in scores if s is not None]
            out["sweeps"].append({
                **{k: v for k, v in cfg.items() if k != "kw"},
                "kw": {k: sorted(v) if isinstance(v, frozenset) else v
                       for k, v in cfg["kw"].items()},
                "must_recall": round(sum(vals) / len(vals), 3) if vals else None,
                "n_scored": len(vals)})
            if cfg["name"] == "baseline":
                out["entry_families"] = entry_hit_rates(kb, entry_trace,
                                                        items_for("package"))
    finally:
        with pool.connection() as conn:
            conn.cursor().executemany(
                "UPDATE nodes SET access_count = %s, last_accessed_at = %s "
                "WHERE id = %s",
                # dict-row access (mirrors prove_equivalence.py's
                # _snapshot_access/_restore_access), NOT tuple-unpacking: pool
                # connections use dict_row (kb_core/db.py open_pool), unlike
                # run.py/off_corpus.py's bare psycopg.connect(), which defaults to
                # tuple rows. `for nid, ac, la in snapshot` on a list of DICT rows
                # unpacks each row's KEYS ("id", "access_count",
                # "last_accessed_at"), not its values -- confirmed live: psycopg
                # raised "invalid input syntax for type integer: access_count".
                [(r["access_count"], r["last_accessed_at"], r["id"])
                 for r in snapshot])
    path = HERE / "results" / "2x2" / "ablations.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=1))
    print(json.dumps(out["sweeps"], indent=1))
    return 0


def entry_hit_rates(kb, entry_trace, items):
    """Per entry family, the fraction of items whose top-1 (and any-of-top-3)
    seed of that family leads to a concept containing >=1 must-gold memory.

    `entry_trace` here always comes from the unablated baseline sweep (kw={}), so
    `via` reads exactly what crawl/entry.py's entry_step emits for a normal run --
    "label-cosine" and "evidence-vote" are the only two families that can carry
    kind="concept" (the name-anchor rungs are kind="entity"). Nothing here needs to
    special-case the ablate=label-cosine sweep: that sweep's own trace is never
    passed to this function."""
    import psycopg
    gold = {it["id"]: {m["id"] for m in it["expected"].get("memories", [])
                       if m.get("must", True)} for it in items}
    fams = {"label-cosine": {"top1": 0, "top3": 0, "n": 0},
            "evidence-vote": {"top1": 0, "top3": 0, "n": 0}}
    with psycopg.connect(os.environ["KB_DATABASE_URL"]) as conn:
        for rec in entry_trace:
            must = gold.get(rec["id"], set())
            for fam, stats in fams.items():
                seeds = [e for e in rec["entry"] if e["via"] == fam
                         and e["kind"] == "concept"]
                if not seeds or not must:
                    continue
                stats["n"] += 1
                hits = []
                for s in seeds[:3]:
                    members = {str(r[0]) for r in conn.execute(
                        "SELECT node_id FROM concept_members "
                        "WHERE concept_id = %s AND member_kind = 'memory'",
                        (s["id"],))}
                    hits.append(bool(members & must))
                stats["top1"] += hits[0]
                stats["top3"] += any(hits)
    for stats in fams.values():
        n = stats.pop("n") or 1
        stats["top1"] = round(stats["top1"] / n, 3)
        stats["top3"] = round(stats["top3"] / n, 3)
        stats["n"] = n
    return fams


if __name__ == "__main__":
    sys.exit(main())
