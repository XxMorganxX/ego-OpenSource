"""`kb-mcp-sync` — sync a source (Obsidian vault) into the knowledge base."""

import argparse
import os
import sys

from kb_core.adapters.obsidian.adapter import ObsidianAdapter
from kb_core.db import migrate_pool, open_pool
from kb_core.knowledge_base import KnowledgeBase
from kb_core.registry import default_registry
from kb_core.sync import SyncService


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kb-mcp-sync")
    parser.add_argument("--vault", default=os.environ.get("KB_OBSIDIAN_VAULT"))
    parser.add_argument("--database-url", default=os.environ.get("KB_DATABASE_URL"))
    parser.add_argument("--no-prune", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if not args.vault:
        parser.error("provide --vault or set KB_OBSIDIAN_VAULT")
    if not args.database_url:
        parser.error("provide --database-url or set KB_DATABASE_URL")

    pool = open_pool(args.database_url)
    try:
        migrate_pool(pool)
        kb = KnowledgeBase(pool, default_registry())
        layer = os.environ.get("KB_OBSIDIAN_LAYER", "Obsidian")
        stamp = os.environ.get("KB_OBSIDIAN_STAMP", "1").lower() not in ("0", "", "false", "no")
        result = SyncService(kb, pool).sync(
            ObsidianAdapter(args.vault, root_label=layer or None, stamp=stamp),
            prune=not args.no_prune, dry_run=args.dry_run,
        )
    finally:
        pool.close()

    suffix = " (dry-run)" if args.dry_run else ""
    print(f"[{result.source}] upserted={result.upserted} "
          f"deleted={result.deleted} errors={len(result.errors)}{suffix}")
    for err in result.errors:
        print(f"  ERROR: {err}", file=sys.stderr)
    return 1 if result.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
