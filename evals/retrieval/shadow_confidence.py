#!/usr/bin/env python3
"""Derive the CONFIDENCE_* pair for the shipped query instruction -- write-free.

House rule (the CONFIDENCE_* note in kb_core/knowledge_base.py):
  WEAK   = worst junk top-1 across BOTH modes + 0.003 (rounded UP to 3 dp)
  STRONG = 0.904 x the lower of the two modes' gold top-1 medians
Junk = the permanent off_corpus.py PROBES. Gold = the authored search-first items + the
adjudicated traffic items. Mode A = KnowledgeBase.search (search_memories' path);
mode B = cluster-crawl with judge="none" (search_graph's path), in-process on shadow's
read-only pool with access bumps no-op'd. Today's corpus (no as-of): this calibrates
against the corpus as it is now. Pass bar: every junk probe 'none' in both modes and <= 2 gold 'none'
labels per mode under the derived pair.

Run from evals/retrieval with KB_Retrieval's venv (it carries kb_core AND kb_retrieval):
  KB_DATABASE_URL=... KB_EVAL_DATA_DIR=... ../../KB_Retrieval/.venv/bin/python shadow_confidence.py
Exit: 0 derived and passes; 1 derived but fails the bar; 2 could not run."""
import argparse
import ast
import json
import math
import statistics
import sys
from datetime import datetime, timezone

import shadow as S

JUNK_MARGIN = 0.003
STRONG_RATIO = 0.904
MAX_GOLD_NONE = 2
SCOPED_KEYS = ("under", "kind", "tags", "since", "until")


def derive_pair(junk_a, junk_b, gold_a, gold_b) -> tuple:
    weak = math.ceil((max(max(junk_a), max(junk_b)) + JUNK_MARGIN) * 1000) / 1000
    strong = round(STRONG_RATIO * min(statistics.median(gold_a),
                                      statistics.median(gold_b)), 3)
    return weak, strong


def level(best, scoped: bool, weak: float, strong: float) -> str:
    """search_confidence's levels for one result set (no rows -> callers pass best=None
    only for keyword-only sets).

    Production (`search_confidence`) labels an empty result set 'none' even when
    scoped -- an empty set is not evidence of a match. This helper does not see the
    row count, only `scoped` and `best`, so it reports an empty-but-scoped set as
    'scoped' (or 'keyword' when unscoped). That has occurred among measured probes
    (an empty facet set): callers must check for an empty result set themselves and
    treat it as 'none', as the calibration note in `knowledge_base.py` does."""
    if scoped:
        return "scoped"
    if best is None:
        return "keyword"
    return "strong" if best >= strong else "weak" if best >= weak else "none"


def load_probes() -> list:
    """off_corpus.PROBES without importing off_corpus (it imports the MCP client)."""
    tree = ast.parse((S.HERE / "off_corpus.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "PROBES"
                                                for t in node.targets):
            return ast.literal_eval(node.value)
    raise S.CannotRun("off_corpus.py no longer defines PROBES")


def _queries(tier: str) -> list:
    """(id, query, tool args) for junk + gold."""
    out = [(f"junk-{i}", p, {"limit": 20}) for i, p in enumerate(load_probes())]
    for it, a, _ in S.authored_search_items(S.load_authored_gold()):
        a = dict(a)
        out.append((it["id"], a.pop("query"), a))
    for it in S.load_traffic_gold(tier):
        a = dict(it["args"])
        a.pop("query", None)
        out.append((it["id"], it["query"], a))
    return out


def measure(kb, qid, query, args) -> dict:
    from kb_core.knowledge_base import search_confidence
    from kb_retrieval.contract import Query
    from kb_retrieval.registry import get_strategy
    kw, limit = S.norm_args(args)
    scoped = any(kw.get(k) is not None for k in SCOPED_KEYS)
    inst = S.embedder()(query, S.QUERY_INSTRUCTION)
    bare = S.embedder()(query, S.BARE)
    a = search_confidence(kb.search(query, embedding=inst, limit=limit, **kw), scoped=scoped)
    res = get_strategy("cluster-crawl", kb, judge="none").search(
        Query(text=query, embedding=inst, label_embedding=bare, limit=limit, **kw))
    return {"id": qid, "scoped": scoped, "a": a.get("best_similarity"),
            "b": res.confidence.get("best_similarity")}


def main(argv=None) -> int:
    return S.run_cli("shadow_confidence", _main, argv)


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tier", default="adjudicated")
    ap.add_argument("--max-embeds", type=int, default=50)
    a = ap.parse_args(argv)
    kb = S.kb()
    print("readonly:", S.assert_readonly(kb))
    S.embedder().max_calls = a.max_embeds
    qs = _queries(a.tier)
    S.check_embed_budget([(q, p) for _, q, _ in qs for p in (S.QUERY_INSTRUCTION, S.BARE)],
                         a.max_embeds)
    rows = [measure(kb, *q) for q in qs]
    junk = [r for r in rows if r["id"].startswith("junk-")]
    gold = [r for r in rows if not r["id"].startswith("junk-")]
    pick = lambda rs, m: [r[m] for r in rs if r[m] is not None]   # noqa: E731
    weak, strong = derive_pair(pick(junk, "a"), pick(junk, "b"),
                               pick(gold, "a"), pick(gold, "b"))
    report = {}
    for m in ("a", "b"):
        jl = [level(r[m], False, weak, strong) for r in junk]
        gl = [level(r[m], r["scoped"], weak, strong) for r in gold]
        report[m] = {"junk": {x: jl.count(x) for x in set(jl)},
                     "gold": {x: gl.count(x) for x in set(gl)},
                     "gold_none_ids": [r["id"] for r, x in zip(gold, gl) if x == "none"]}
    ok = all(report[m]["junk"].get("none", 0) == len(junk)
             and len(report[m]["gold_none_ids"]) <= MAX_GOLD_NONE for m in ("a", "b"))
    out = {"started_at": datetime.now(timezone.utc).isoformat(), "git": S.git_state(),
           "instruction_version": S.QUERY_INSTRUCTION_VERSION,
           "derived": {"weak": weak, "strong": strong}, "pass": ok, "modes": report,
           "n": {"junk": len(junk), "gold": len(gold)}, "rows": rows}
    path = S.RESULTS_DIR / f"confidence-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=1, default=str))
    print(f"derived WEAK={weak} STRONG={strong} | pass={ok} | junk n={len(junk)} gold n={len(gold)}")
    for m in ("a", "b"):
        print(f"  mode {m}: junk {report[m]['junk']}  gold {report[m]['gold']}  "
              f"gold none: {report[m]['gold_none_ids']}")
    print(f"wrote {path.relative_to(S.HERE)}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
