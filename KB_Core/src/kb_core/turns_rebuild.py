"""kb-turns-rebuild: re-derive mcp_trace.turn_id for every row at the gap in force.

Run after changing TURN_GAP_S in knowledge_base.py. The write path applies the new
constant to new rows on its own; this makes history agree with it. Idempotent. The
rebuild takes an EXCLUSIVE lock on mcp_trace for its transaction, so live trace
writes wait for it rather than race it (reads continue)."""

import argparse
import os

from kb_core.db import open_pool
from kb_core.knowledge_base import TURN_GAP_S, KnowledgeBase
from kb_core.registry import default_registry


def main(argv: "list[str] | None" = None) -> int:
    p = argparse.ArgumentParser(
        prog="kb-turns-rebuild",
        description="Re-derive mcp_trace.turn_id for every row at TURN_GAP_S (one-shot).")
    p.add_argument("--database-url", default=os.environ.get("KB_DATABASE_URL"))
    args = p.parse_args(argv)
    if not args.database_url:
        p.error("provide --database-url or set KB_DATABASE_URL")

    pool = open_pool(args.database_url)
    try:
        moved = KnowledgeBase(pool, default_registry()).rebuild_turns()
        print(f"[turns-rebuild] gap {TURN_GAP_S}s: {moved} row(s) moved.", flush=True)
    finally:
        pool.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
