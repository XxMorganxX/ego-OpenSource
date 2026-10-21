"""`kb-prune-instances` — list and remove junk-named entity instances.

A preview-then-apply cleanup for instances whose NAME is noise by the deterministic gate
(`instance_name_issue`): filenames (README.md), paths (a/b.py), task-slugs, pronouns,
placeholders. These were promoted before the staging gate existed; the gate now stops new
ones, so a delete here sticks.

Default is a dry preview (prints each offender + reason + support). Pass `--apply` to delete.
Only the named offenders are removed — real entities and classes are never touched. Operates
on an already-migrated database (data-only).
"""

import argparse
import os
import sys

from kb_core.db import open_pool
from kb_core.knowledge_base import KnowledgeBase
from kb_core.registry import default_registry


def main(argv: "list[str] | None" = None) -> int:
    p = argparse.ArgumentParser(
        prog="kb-prune-instances",
        description="List (default) or delete entity instances with junk names.")
    p.add_argument("--database-url", default=os.environ.get("KB_DATABASE_URL"))
    p.add_argument("--apply", action="store_true",
                   help="delete the listed instances (default: preview only)")
    p.add_argument("-y", "--yes", action="store_true",
                   help="skip the confirmation prompt when applying (for scripted use)")
    args = p.parse_args(argv)

    if not args.database_url:
        p.error("provide --database-url or set KB_DATABASE_URL")

    pool = open_pool(args.database_url)
    try:
        kb = KnowledgeBase(pool, default_registry())
        noisy = kb.noisy_instances()
        if not noisy:
            print("No junk-named instances found.")
            return 0

        print(f"{len(noisy)} junk-named instance(s):")
        for d in noisy:
            print(f"  {d['name']!r}  (support={d['support']}) — {d['reason']}")

        if not args.apply:
            print("\nPreview only — re-run with --apply to delete.")
            return 0

        if not args.yes:
            reply = input(f"\nDelete these {len(noisy)} instance(s)? Type 'yes' to confirm: ").strip()
            if reply != "yes":
                print("Aborted.", file=sys.stderr)
                return 1

        removed = kb.prune_noisy_instances([d["id"] for d in noisy])
        print(f"\nDeleted {removed} instance(s).")
    finally:
        pool.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
