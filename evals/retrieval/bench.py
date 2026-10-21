#!/usr/bin/env python3
# evals/retrieval/bench.py
"""Run one 2x2 cell (all repeats) against the live MCP surface.

    cd KB_MCP && uv run python ../evals/retrieval/bench.py --cell cell3-ag
        [--url http://127.0.0.1:3000/mcp] [--k 20] [--out results/2x2]

Protocol: plans substituted per bench_metrics.substitute_plan; top_ids
persisted; latency per tool call; access-state snapshot/restore per REPEAT (an arm);
consolidated_at must be single-valued per repeat or the repeat aborts; agentic cells
require KB_RETRIEVAL_STRICT_LLM=1 IN THE MCP SERVER (this process cannot set it)
and abort on the first tool error, leaving a resume file.
MCP wraps server-side exceptions as isError tool results; the runner treats any
isError response as a cell abort — silent degradation must be impossible.

Scoring reuses run.py's score_item verbatim — the grid must not fork the metric."""
import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).parent))
import bench_metrics as bm
from run import score_item, tool_result_meta, tool_result_rows  # noqa: E402

DSN = os.environ.get("KB_DATABASE_URL", "postgresql://kb_app@localhost:5433/kb_mcp")
SETS = {"flat": "golden.jsonl", "package": "golden_package.jsonl"}


def _eval_data(rel: str, default: Path) -> Path:
    root = os.environ.get("KB_EVAL_DATA_DIR")
    return Path(root) / rel if root else default


def exception_leaves(exc):
    """Flatten (Base)ExceptionGroup trees to their leaf exceptions — the MCP client
    wraps transport failures in TaskGroups and str() hides the cause."""
    subs = getattr(exc, "exceptions", None)
    if not subs:
        return [exc]
    return [leaf for s in subs for leaf in exception_leaves(s)]


def trace_lite(trace):
    """The trace subset worth persisting per call (full traces are large)."""
    t = trace or {}
    return {k: t.get(k) for k in
            ("crawl_mode", "llm", "degraded", "stop_reason", "consolidated_at",
             "stale_concepts", "flat_displaced", "rejected_admissions")} | {
            "hops": [{"agentic": h.get("agentic"), "by": h.get("by")}
                     for h in (t.get("hops") or [])],
            "entry_via": [e.get("via") for e in (t.get("entry") or [])]}


def version_stamp(cell_cfg, url, k):
    v = subprocess.run(["claude", "--version"], capture_output=True, text=True)
    stamp = {
        "ran_at": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "url": url, "k": k, "cell": cell_cfg,
        "git_head": subprocess.run(["git", "rev-parse", "HEAD"],
                                   capture_output=True, text=True).stdout.strip(),
        "claude_version": v.stdout.strip() or v.stderr.strip(),
        "hop_prompt_hash": None, "model": None,
    }
    try:  # hash the prompt+schema actually deployed
        sys.path.insert(0, str(Path(__file__).parents[2] / "KB_Retrieval" / "src"))
        from kb_retrieval.crawl import prompts
        stamp["hop_prompt_hash"] = hashlib.sha256(
            (json.dumps(prompts.HOP_SCHEMA, sort_keys=True)
             + prompts._SYSTEM).encode()).hexdigest()[:16]
    except Exception as exc:                                   # noqa: BLE001
        stamp["hop_prompt_hash"] = f"unavailable: {exc}"
    if cell_cfg.get("judge") == "llm":
        # Resolve the moving model alias with ONE tiny call (quota-visible, tiny).
        try:
            out = subprocess.run(
                ["claude", "-p", "OK", "--output-format", "json"],
                capture_output=True, text=True, timeout=120).stdout
            usage = json.loads(out).get("modelUsage") or {}
            stamp["model"] = next(iter(usage), None)
        except Exception as exc:                               # noqa: BLE001
            stamp["model"] = f"unresolved: {exc}"
    return stamp


def ensure_not_error(res, tool, item_id):
    """MCP wraps server exceptions as isError RESULTS (not transport errors); an
    unchecked one scores as an empty row list, which is exactly the silent
    degradation the protocol forbids."""
    if getattr(res, "isError", False):
        err = (res.content[0].text if res.content else "")[:300]
        raise RuntimeError(f"tool {tool} returned an MCP error for item {item_id}: {err}")


async def execute_item(session, item, plan, subs, k):
    """run.py's execution shape (round-robin interleave preserved via score on the
    ordered list) with search_graph dispatch, anchor threading, and per-call capture."""
    step_memories, calls = [], []
    resolved_entity = None
    traces = []
    for step, sub in zip(plan, ([None] * len(plan) if not subs else
                                _sub_map(plan, subs))):
        tool, args = step["tool"], dict(step.get("args") or {})
        memories: list = []
        step_memories.append(memories)
        if tool == "search_graph" and sub and sub["wants_anchor"] and resolved_entity:
            args.setdefault("anchor_entity", resolved_entity)
        t0 = time.perf_counter()
        try:
            res = await session.call_tool(tool, args)
        except Exception:
            # Strict-mode abort (or transport death) — surface to the cell driver.
            raise
        ms = round((time.perf_counter() - t0) * 1000)
        ensure_not_error(res, tool, item["id"])
        rows = tool_result_rows(res)
        meta = tool_result_meta(res)
        if tool == "find_entity":
            if rows and resolved_entity is None:
                resolved_entity = rows[0]["id"]
        elif tool in ("search_graph", "search_memories"):
            memories.extend(r["id"] for r in rows)
        calls.append({"tool": tool, "latency_ms": ms,
                      "cost": meta.get("cost"),
                      "trace_lite": trace_lite(meta.get("trace"))})
        if meta.get("trace"):
            traces.append(meta["trace"])
    seen, ordered = set(), []
    width = max(map(len, step_memories), default=0)
    for tier in zip(*[b + [None] * (width - len(b)) for b in step_memories]):
        for m in tier:
            if m is not None and m not in seen:
                seen.add(m)
                ordered.append(m)
    return ordered, calls, traces


def _sub_map(plan, subs):
    """Align substitution records to plan positions (subs are in producer order)."""
    it = iter(subs)
    return [next(it) if s["tool"] == "search_graph" else None for s in plan]


def consolidated_values(items):
    """Every distinct non-null consolidated_at across items' calls — used to re-seed
    the stability check across a resume boundary."""
    out = set()
    for it in items:
        for c in it.get("calls") or []:
            ca = (c.get("trace_lite") or {}).get("consolidated_at")
            if ca is not None:
                out.add(str(ca))
    return out


def write_state(path, obj):
    """Atomic state-file write: write to a sibling .tmp file then os.replace onto
    path, so a crash mid-write never leaves a truncated/corrupt resume file."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj))
    os.replace(tmp, path)


async def run_repeat(cell, cfg, items, k, url, out_dir, repeat):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    state_path = out_dir / f"{cell}-r{repeat}.state.json"
    done = {}
    if state_path.exists():
        done = json.loads(state_path.read_text())["items"]
        print(f"resuming {cell}-r{repeat}: {len(done)} items already done")

    with psycopg.connect(DSN) as conn:                      # snapshot per ARM
        snapshot = conn.execute(
            "SELECT id, access_count, last_accessed_at FROM nodes").fetchall()
    results = dict(done)
    consolidated_seen = consolidated_values(done.values())  # re-seed across resume
    if len(consolidated_seen) > 1:
        raise SystemExit(
            f"ABORT {cell}-r{repeat}: state file already spans consolidation ticks "
            f"({sorted(consolidated_seen)}) — delete {state_path} to discard and re-run")
    try:
        async with streamablehttp_client(url) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                for item in items:
                    if item["id"] in results:
                        continue
                    plan, subs = bm.substitute_plan(
                        item, cfg["strategy"], cfg["judge"])
                    ordered, calls, traces = await execute_item(
                        session, item, plan, subs, k)
                    got = {"memories": ordered, "entities": [], "concepts": [],
                           "can_score_entities": False, "can_score_concepts": False}
                    rec = score_item(item, got, k)
                    rec["top_ids"] = ordered[:max(k, 20)]
                    rec["plan_substitution"] = subs
                    rec["calls"] = calls
                    for t in traces:
                        ca = t.get("consolidated_at")
                        if ca is not None:
                            consolidated_seen.add(str(ca))
                    # Persist the triggering item BEFORE the stability check can
                    # abort — otherwise the very call that revealed the tick is
                    # discarded and a resume can never see it.
                    results[item["id"]] = rec
                    write_state(state_path, {"items": results})
                    if len(consolidated_seen) > 1:          # one consolidation tick per repeat
                        raise SystemExit(
                            f"ABORT {cell}-r{repeat}: consolidation ticked mid-run "
                            f"({sorted(consolidated_seen)}) — results are not "
                            f"comparable; re-run after pinning the daemon")
    except SystemExit:
        raise
    except Exception as exc:                                # strict abort → resume file
        write_state(state_path, {"items": results})
        print(f"CELL ABORT {cell}-r{repeat} after {len(results)}/{len(items)} items: "
              f"{'; '.join(repr(leaf) for leaf in exception_leaves(exc))[:600]}\nresume: re-run the same command (state file kept)",
              file=sys.stderr)
        raise SystemExit(3)
    finally:
        # Capture propagation status BEFORE the nested try/except below — once
        # that except clause is active, sys.exc_info() reflects ITS exception
        # (the restore failure), not whatever was already unwinding through this
        # frame, so checking exc_info() from inside that clause would always see
        # a live exception and never detect the "run was actually fine" case.
        was_propagating = sys.exc_info()[0] is not None
        try:
            with psycopg.connect(DSN) as conn:              # restore per ARM
                conn.cursor().executemany(
                    "UPDATE nodes SET access_count = %s, last_accessed_at = %s "
                    "WHERE id = %s",
                    [(ac, la, nid) for nid, ac, la in snapshot])
        except Exception as exc:                            # noqa: BLE001
            print(f"RESTORE FAILED (arm access-state contaminated): {exc}",
                  file=sys.stderr)
            if not was_propagating:
                raise SystemExit(4)
    state_path.unlink(missing_ok=True)
    return results, sorted(consolidated_seen)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--cell", required=True, choices=sorted(bm.CELLS))
    p.add_argument("--url", default="http://127.0.0.1:3000/mcp")
    p.add_argument("--k", type=int, default=20)
    p.add_argument("--out", default=str(Path(__file__).parent / "results" / "2x2"))
    p.add_argument("--repeats", type=int, default=None,
                   help="override the cell's spec repeat count (e.g. batch 2 of 3)")
    args = p.parse_args()

    cfg = bm.CELLS[args.cell]
    golden = _eval_data(f"retrieval/{SETS[cfg['set']]}", Path(__file__).parent / SETS[cfg["set"]])
    # The same gate run.py uses: refuse to score a rotten set.
    v = subprocess.run([sys.executable,
                        str(Path(__file__).parent / "validate.py"), str(golden)],
                       capture_output=True, text=True)
    if v.returncode != 0:
        sys.stderr.write(v.stdout + v.stderr + "\nrefusing to run: fix the gold\n")
        return 2
    items = [json.loads(line) for line in golden.read_text().splitlines() if line.strip()]
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = version_stamp(cfg, args.url, args.k)
    n = cfg["repeats"] if args.repeats is None else args.repeats
    for repeat in range(1, n + 1):
        out_path = out_dir / f"{args.cell}-r{repeat}.json"
        if out_path.exists():
            print(f"{out_path.name} exists — skipping (delete to re-run)")
            continue
        results, consolidated = asyncio.run(run_repeat(
            args.cell, cfg, items, args.k, args.url, out_dir, repeat))
        out_path.write_text(json.dumps(
            {"stamp": stamp, "cell": args.cell, "repeat": repeat,
             "consolidated_at": consolidated,
             "items": list(results.values())}, indent=1))
        print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
