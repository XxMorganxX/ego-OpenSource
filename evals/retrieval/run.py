#!/usr/bin/env python3
"""Run the retrieval golden set against the live MCP surface and score it.

Executes each item's tool_plan over MCP streamable-http (the same path agents
use — embedder, ranking, everything), then scores retrieved memories/entities/
concepts against the gold labels:

  must_recall@k   |gold(must) ∩ top-k retrieved memories| / |gold(must)|
  all_recall@k    same over must+may gold
  first_hit_rank  rank of the first must-gold memory (None = missed entirely)
  entity_recall   fraction of expected entities in find_entity's hits
  concept_recall  fraction of expected (name, kind) concepts retrieved
                  (goals items additionally require goal.current = true)
  neg_hits@k      distractors that ranked anyway (want 0)

Observer effect: search/concept reads bump nodes.access_count/last_accessed_at,
which feeds retrieval-recency ranking — so the runner snapshots those columns for
every node before the run and restores them after (skip with --no-restore).
Within-run ordering effects remain but are deterministic.

Usage: run.py [golden.jsonl] [--url http://127.0.0.1:3000/mcp] [--k 10]
              [--items ga-001,ga-002] [--no-restore] [--out results/]
Run with KB_MCP's venv python (needs mcp + psycopg via ego-os-core).
"""
import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

# Read from the environment like validate.py does — a live DB credential does not
# belong in a git-tracked file.
DSN = os.environ.get("KB_DATABASE_URL", "postgresql://kb@localhost:5433/kb_mcp")


def _eval_data(rel: str, default: Path) -> Path:
    root = os.environ.get("KB_EVAL_DATA_DIR")
    return Path(root) / rel if root else default


def tool_result_rows(res) -> list[dict]:
    """The row list from a tool result, whatever envelope it arrived in.

    Three shapes in play: a bare list (most tools), FastMCP's {"result": [...]}
    wrapper for non-object returns, and search_memories' {"confidence", "results"}
    envelope — which exists so a caller can tell "nothing matched" from "here are
    the closest rows". Unwrap to rows here; confidence is read via tool_result_meta.
    """
    sc = getattr(res, "structuredContent", None)
    if isinstance(sc, dict):
        for key in ("results", "result"):
            if key in sc:
                out = sc[key]
                return out if isinstance(out, list) else [out]
    rows = []
    for c in res.content or []:
        try:
            payload = json.loads(c.text)
        except (json.JSONDecodeError, AttributeError):
            continue
        if isinstance(payload, dict) and "results" in payload:
            rows.extend(payload["results"])
        else:
            rows.append(payload)
    return rows


def tool_result_meta(res) -> dict:
    """Non-row fields off a tool result (confidence, embedder). {} when absent."""
    sc = getattr(res, "structuredContent", None)
    if isinstance(sc, dict) and "results" in sc:
        return {k: v for k, v in sc.items() if k != "results"}
    return {}


async def execute_item(session, item: dict, k: int) -> dict:
    """Follow the tool_plan; collect ordered memory ids, entity hits, concepts.

    Memory results from multiple steps are ROUND-ROBIN interleaved (each step's
    best first), not concatenated — an agent reads the top of every tool result
    it requested, so first-call dominance would mis-score multi-step plans."""
    step_memories: list[list[str]] = []   # one bucket per plan step
    entities: list[dict] = []
    concepts: list[dict] = []
    resolved_entity: str | None = None
    plan = item.get("tool_plan") or [
        {"tool": "search_memories", "args": {"query": item["request"]}}]
    for step in plan:
        tool, args = step.get("tool"), dict(step.get("args") or {})
        memories: list[str] = []
        step_memories.append(memories)
        try:
            if tool == "find_entity":
                args.setdefault("name", item["request"])
                rows = tool_result_rows(await session.call_tool("find_entity", args))
                entities.extend(rows[:5])
                if rows and resolved_entity is None:
                    resolved_entity = rows[0]["id"]
            elif tool == "entity_activity":
                args.setdefault("entity_id", resolved_entity)
                if not args.get("entity_id"):
                    continue
                args.setdefault("limit", max(k, 20))
                rows = tool_result_rows(await session.call_tool("entity_activity", args))
                memories.extend(r["id"] for r in rows)
            elif tool == "search_memories":
                args.setdefault("query", item["request"])
                args.setdefault("limit", max(k, 20))
                rows = tool_result_rows(await session.call_tool("search_memories", args))
                memories.extend(r["id"] for r in rows)
            elif tool == "list_concepts":
                rows = tool_result_rows(await session.call_tool("list_concepts", args))
                concepts.extend(rows)
            elif tool == "concept":
                cids = [args["concept_id"]] if args.get("concept_id") else []
                if not cids:
                    # resolve EVERY expected concept (an item's gold can span
                    # several — an agent reads each relevant concept it found)
                    want = {(c["name"], c["kind"])
                            for c in item["expected"].get("concepts", [])}
                    cat = tool_result_rows(await session.call_tool("list_concepts", {}))
                    cids = [c["id"] for c in cat
                            if (c["name"], c["kind"]) in want][:4]
                for j, cid in enumerate(cids):
                    # each concept read is its own call -> its own interleave
                    # bucket, so one big concept can't crowd out the others
                    bucket = memories if j == 0 else []
                    if j > 0:
                        step_memories.append(bucket)
                    rows = tool_result_rows(
                        await session.call_tool("concept", {"concept_id": cid}))
                    for c in rows:
                        concepts.append(c)
                        bucket.extend(m["node_id"] for m in c.get("members", [])
                                      if m.get("member_kind") == "memory")
            elif tool == "expand":
                args.setdefault("id", resolved_entity)
                if not args.get("id"):
                    continue
                rows = tool_result_rows(await session.call_tool("expand", args))
                for r in rows:
                    groups = r.get("groups", {})
                    for g in ("memories", "similar_memories", "co_mentioned"):
                        memories.extend(m["id"] for m in (groups.get(g) or []))
                    for g in ("entities", "co_entities"):
                        entities.extend(groups.get(g) or [])
                    concepts.extend(groups.get("concepts") or [])
            # get_memory / list_doc_kinds steps carry no scoring signal — skip
        except Exception as exc:                       # noqa: BLE001
            print(f"  {item['id']}: step {tool} failed: {exc}", file=sys.stderr)
    seen, ordered = set(), []
    for tier in zip(*[b + [None] * (max(map(len, step_memories), default=0) - len(b))
                      for b in step_memories]) if step_memories else []:
        for m in tier:
            if m is not None and m not in seen:
                seen.add(m)
                ordered.append(m)
    plan_tools = {s.get("tool") for s in plan}
    return {"memories": ordered, "entities": entities, "concepts": concepts,
            "can_score_entities": bool(plan_tools & {"find_entity", "expand"}),
            "can_score_concepts": bool(plan_tools & {"list_concepts", "concept",
                                                     "expand"})}


def score_item(item: dict, got: dict, k: int) -> dict:
    exp = item["expected"]
    top = got["memories"][:k]
    out: dict = {"id": item["id"], "archetype": item["archetype"],
                 "difficulty": item.get("difficulty"),
                 "retrieved": len(got["memories"])}
    gold = exp.get("memories", [])
    must = [m["id"] for m in gold if m.get("must", True)]
    allg = [m["id"] for m in gold]
    if must:
        out["must_recall"] = round(sum(m in top for m in must) / len(must), 3)
        ranks = [got["memories"].index(m) + 1 for m in must if m in got["memories"]]
        out["first_hit_rank"] = min(ranks) if ranks else None
        out["missing_must"] = [m for m in must if m not in top]
    if allg:
        out["all_recall"] = round(sum(m in top for m in allg) / len(allg), 3)
    # Entity/concept expectations are scored only when the tool plan contains a
    # step that could produce them — otherwise the metric would grade the route
    # (a search-only plan) as a retrieval failure.
    # Entities are matched by canonical_name, not id — ids churn whenever the entity
    # layer is rebuilt (kb-reset-entities), which silently zeroed these scores before.
    # Case-insensitive because 'Vandelay' vs 'VANDELAY' is a display detail, not a
    # retrieval failure.
    eg = exp.get("entities", [])
    if eg and got["can_score_entities"]:
        hit_names = {(e.get("name") or "").lower() for e in got["entities"]}
        want = [(e.get("name") or "").lower() for e in eg]
        out["entity_recall"] = round(sum(w in hit_names for w in want) / len(want), 3)
        out["entity_any"] = any(w in hit_names for w in want)
    # Concepts match on NAME alone. The gold records a kind for readability, but the
    # consolidation layer can reclassify a concept's kind between runs, so scoring on
    # (name, kind) marks a correctly-retrieved concept wrong on a timer.
    cg = exp.get("concepts", [])
    if cg and got["can_score_concepts"]:
        pairs = {c.get("name"): c for c in got["concepts"]}
        def c_ok(c):
            row = pairs.get(c["name"])
            if row is None:
                return False
            if item["archetype"] == "goals":
                return bool((row.get("goal") or {}).get("current"))
            return True
        out["concept_recall"] = round(sum(c_ok(c) for c in cg) / len(cg), 3)
    negs = [n["id"] for n in item.get("negatives", []) if n.get("id")]
    if negs:
        # id-space comparison on both sides: a retired-duplicate concept negative
        # can only hit if that exact concept id resurfaces (names collide — an
        # active area and a retired project can share one name).
        # For goals items a concept negative only counts against the CURRENT set:
        # list_concepts deliberately returns dormant concepts too — being listed
        # is correct; being flagged a current goal is the failure.
        if item["archetype"] == "goals":
            got_concept_ids = {c.get("id") for c in got["concepts"]
                               if (c.get("goal") or {}).get("current")}
        else:
            got_concept_ids = {c.get("id") for c in got["concepts"]}
        out["neg_hits"] = sum((n in top) or (n in got_concept_ids) for n in negs)
    return out


async def run(args) -> int:
    items = [json.loads(line) for line in Path(args.golden).read_text().splitlines()
             if line.strip()]
    if args.items:
        keep = set(args.items.split(","))
        items = [i for i in items if i["id"] in keep]
    run_started_at = datetime.now(timezone.utc)
    snapshot = None
    if not args.no_restore:
        with psycopg.connect(DSN) as conn:
            snapshot = conn.execute(
                "SELECT id, access_count, last_accessed_at FROM nodes").fetchall()
    results = []
    try:
        async with streamablehttp_client(args.url) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                for it in items:
                    got = await execute_item(session, it, args.k)
                    results.append(score_item(it, got, args.k))
    finally:
        if snapshot is not None:
            with psycopg.connect(DSN) as conn:
                conn.cursor().executemany(
                    "UPDATE nodes SET access_count = %s, last_accessed_at = %s "
                    "WHERE id = %s",
                    [(ac, la, nid) for nid, ac, la in snapshot])
            print(f"restored access state for {len(snapshot)} nodes")
        # Tag this run's traces so dashboard summaries can exclude benchmark
        # floods (mcp_trace.caller IS NULL = real traffic). Time-window stamping
        # because this harness talks to the LONG-RUNNING server -- a server-side
        # env tag would mislabel everything. Single user: interleaved real
        # traffic during a run is accepted (the trace table makes the same call).
        # Unlike the access-count restore above, this isn't gated on
        # --no-restore -- the caller tag matters whenever items ran, restore
        # or not, and it must still land if execute_item raised (we're in
        # `finally`).
        with psycopg.connect(DSN) as conn:
            conn.execute(
                "UPDATE mcp_trace SET caller = 'eval' "
                "WHERE caller IS NULL AND at >= %s", (run_started_at,))

    def agg(rows, key):
        vals = [r[key] for r in rows if r.get(key) is not None]
        return round(sum(vals) / len(vals), 3) if vals else None

    def scored(rows, key="must_recall"):
        """How many rows actually contributed. agg() averages only non-None rows, so
        the printed n must not count rows it skipped (e.g. items with empty gold)."""
        return sum(1 for r in rows if r.get(key) is not None)

    def mrr(rows):
        """Mean reciprocal rank of the first must-gold hit. first_hit_rank was already
        recorded per item and never aggregated; a miss contributes 0."""
        vals = [r["first_hit_rank"] for r in rows if r.get("must_recall") is not None]
        return round(sum(1.0 / v if v else 0.0 for v in vals) / len(vals), 3) if vals else None

    by_arch: dict[str, list] = {}
    for r in results:
        by_arch.setdefault(r["archetype"], []).append(r)
    print(f"\n{'archetype':<16} {'n':>3} {'scored':>6} {'must_r@k':>9} {'all_r@k':>8} "
          f"{'MRR':>6} {'ent_r':>6} {'con_r':>6} {'negs':>5}")
    for arch, rows in sorted(by_arch.items()):
        print(f"{arch:<16} {len(rows):>3} {scored(rows):>6} "
              f"{str(agg(rows,'must_recall')):>9} "
              f"{str(agg(rows,'all_recall')):>8} {str(mrr(rows)):>6} "
              f"{str(agg(rows,'entity_recall')):>6} "
              f"{str(agg(rows,'concept_recall')):>6} "
              f"{sum(r.get('neg_hits',0) for r in rows):>5}")
    overall = agg(results, "must_recall")
    misses = [r["id"] for r in results
              if r.get("must_recall") is not None and r["must_recall"] == 0]
    print(f"\noverall must_recall@{args.k}: {overall} "
          f"(scored on {scored(results)}/{len(results)} items) | "
          f"MRR: {mrr(results)} | complete misses: {len(misses)} {misses}")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"{stamp}.json"
    path.write_text(json.dumps(
        {"ran_at": stamp, "k": args.k, "url": args.url,
         "overall_must_recall": overall, "mrr": mrr(results),
         "items_scored": scored(results), "items_total": len(results),
         "by_archetype": {a: {"n": len(rows), "scored": scored(rows),
                              "must_recall": agg(rows, "must_recall"),
                              "mrr": mrr(rows)}
                          for a, rows in sorted(by_arch.items())},
         "items": results}, indent=1))
    print(f"full results: {path}")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("golden", nargs="?",
                   default=str(_eval_data("retrieval/golden.jsonl", Path(__file__).parent / "golden.jsonl")))
    p.add_argument("--url", default="http://127.0.0.1:3000/mcp")
    # k=20 matches the deployed default (server.py search_memories limit=20). Scoring
    # at a smaller k than the harness requested understates recall and measures a
    # system nobody runs.
    p.add_argument("--k", type=int, default=20)
    p.add_argument("--items", default=None)
    p.add_argument("--no-restore", action="store_true")
    p.add_argument("--skip-validate", action="store_true",
                   help="run even if the golden set fails validation (not advised)")
    p.add_argument("--out", default=str(Path(__file__).parent / "results"))
    args = p.parse_args()

    # Gate on the drift detector, which exits non-zero on stale gold. Without the gate,
    # stale gold could rot unnoticed between runs and the score silently mean something
    # different. Warnings are fine — the entity and
    # concept layers are emergent and churn by design; errors mean stale gold.
    if not args.skip_validate:
        import subprocess
        v = subprocess.run(
            [sys.executable, str(Path(__file__).parent / "validate.py"), args.golden],
            capture_output=True, text=True)
        if v.returncode != 0:
            sys.stderr.write(v.stdout + v.stderr)
            sys.stderr.write(
                "\nrefusing to score against a golden set with validation ERRORS — "
                "fix the gold (or pass --skip-validate to override).\n")
            sys.exit(2)
        tail = [ln for ln in v.stdout.splitlines() if "warnings," in ln]
        if tail:
            print(f"golden set: {tail[-1]}")

    sys.exit(asyncio.run(run(args)))
