#!/usr/bin/env python3
# evals/retrieval/off_corpus.py
"""Does CONFIDENCE_WEAK (kb_core.knowledge_base) still separate on-corpus from
off-corpus once the pool is a union? A required check, not an optional one. This
file IS the durable record of the probe set; compare DISTRIBUTIONS, not per-probe
values, and never across `confidence.basis` values.

Runs each probe through search_memories (mode A re-anchor) AND
search_graph(strategy='cluster-crawl', judge='none') (mode B, crawl-pool basis; the
judge is irrelevant to confidence — it is computed over the final rows' cosines —
so no quota is drawn). Prints per-probe {level, basis, best_similarity} and the
separation verdict.

A failure can be mode-independent, so checking mode B alone is blind to half the
evidence. The verdict therefore requires BOTH `worst_mode_a_best_similarity` and
`worst_mode_b_best_similarity` below CONFIDENCE_WEAK. Queries are embedded with the
Qwen3 query instruction and the pair is derived for it (kb_core.knowledge_base's
CURRENT CALIBRATION note).

    cd KB_MCP && uv run python ../evals/retrieval/off_corpus.py \
        [--url http://127.0.0.1:3000/mcp] [--out results/2x2/off_corpus.json]
"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).parent))
from run import tool_result_meta  # noqa: E402

sys.path.insert(0, str(Path(__file__).parents[2] / "KB_Core" / "src"))
from kb_core.knowledge_base import CONFIDENCE_WEAK  # noqa: E402  (imported, not re-declared)

DSN = os.environ.get("KB_DATABASE_URL", "postgresql://kb_app@localhost:5433/kb_mcp")

# Three classes × three probes (the surviving original string first). These strings are now
# PERMANENT calibration fixtures — do not reword them; add new ones instead.
PROBES = [
    # plausible-but-absent topics
    "my quantum cryptography patent filing in Tokyo",
    "the vineyard acquisition due diligence in Portugal",
    "notes from my submarine cable landing station tour",
    # nonsense bags
    "crimson ledger apostrophe wind tunnel biscuit",
    "orbital sourdough cadence hexagon mural",
    "velvet syllogism marmalade freckle dynamo",
    # stopword / near-stopword strings
    "the of and to in that it is was for",
    "what about the thing with the stuff",
    "some more of those from before maybe",
]


async def main_async(url, out_path):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    with psycopg.connect(DSN) as conn:
        snapshot = conn.execute(
            "SELECT id, access_count, last_accessed_at FROM nodes").fetchall()
    rows = []
    try:
        async with streamablehttp_client(url) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                for probe in PROBES:
                    a = tool_result_meta(await session.call_tool(
                        "search_memories", {"query": probe, "limit": 20}))
                    b = tool_result_meta(await session.call_tool(
                        "search_graph", {"query": probe, "limit": 20,
                                         "strategy": "cluster-crawl",
                                         "judge": "none"}))
                    rows.append({"probe": probe,
                                 "mode_a": a.get("confidence"),
                                 "mode_b": b.get("confidence")})
    finally:
        with psycopg.connect(DSN) as conn:
            conn.cursor().executemany(
                "UPDATE nodes SET access_count = %s, last_accessed_at = %s "
                "WHERE id = %s",
                [(ac, la, nid) for nid, ac, la in snapshot])

    worst_a = max((r["mode_a"] or {}).get("best_similarity") or 0.0 for r in rows)
    worst_b = max((r["mode_b"] or {}).get("best_similarity") or 0.0 for r in rows)
    verdict = "PASS" if worst_a < CONFIDENCE_WEAK and worst_b < CONFIDENCE_WEAK else "FAIL"
    out = {"probes": rows, "confidence_weak": CONFIDENCE_WEAK,
           "worst_mode_a_best_similarity": worst_a,
           "worst_mode_b_best_similarity": worst_b, "verdict": verdict,
           "note": ("FAIL means WEAK no longer separates junk on at least one basis; "
                    "that is a finding for the maintainer (crawl-specific threshold vs "
                    "basis-only disclosure vs a stale system-wide threshold), not "
                    "something this script fixes")}
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:3000/mcp")
    p.add_argument("--out", default=str(Path(__file__).parent / "results" / "2x2"
                                        / "off_corpus.json"))
    args = p.parse_args()
    sys.exit(asyncio.run(main_async(args.url, args.out)))
