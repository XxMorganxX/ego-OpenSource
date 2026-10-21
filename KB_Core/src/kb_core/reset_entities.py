"""`kb-reset-entities` — wipe the derived entity layer and prep memories for requeue.

Packages the two KnowledgeBase admin operations into one guarded command:
  1. (default) a pg_dump backup of the whole DB — the wipe is irreversible, so back up first;
  2. reset_entities()    — delete every entity node and its entities/links/candidates rows;
  3. requeue_enrichment() — flag memory notes 'pending' (clearing enriched_hash) so the
                            taxonomy rebuilds on the next enrichment run.

Memories themselves are never touched — only the derived entity layer. This does NOT run
enrichment; start the ingest daemon afterwards (e.g. `kb-dev`) to actually rebuild. Operates
on an already-migrated database (data-only — it does not run migrations).

Deleting entity nodes also cascades away every concept's entity members (and any
entity-anchored concept constraints/overrides); rosters re-attach automatically once
enrichment drains and the consolidate daemon's churn trigger fires.
"""

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime

from psycopg.conninfo import conninfo_to_dict
from psycopg_pool import ConnectionPool

from kb_core.backup import pg_child
from kb_core.db import open_pool
from kb_core.entities import ENTITY_SOURCE
from kb_core.knowledge_base import KnowledgeBase
from kb_core.registry import default_registry

# The entity layer (removed) vs. memories (preserved) — shown before any change.
# concept_entity_members rides along: concept_members.node_id cascades on the entity-node
# delete, so every concept's entity roster empties too (rebuilt by the consolidate daemon
# once enrichment drains — see consolidate_daemon.py).
_REMOVED = ("entity_nodes", "entities", "entity_candidates", "entity_links",
            "concept_entity_members")


def _counts(pool: ConnectionPool) -> dict:
    """Read-only snapshot of the entity layer + the preserved memory count."""
    queries = {
        "entity_nodes": ("SELECT count(*) AS n FROM nodes WHERE source = %s", (ENTITY_SOURCE,)),
        "entities": ("SELECT count(*) AS n FROM entities", ()),
        "entity_candidates": ("SELECT count(*) AS n FROM entity_candidates", ()),
        "entity_links": ("SELECT count(*) AS n FROM entity_links", ()),
        "concept_entity_members": (
            "SELECT count(*) AS n FROM concept_members WHERE member_kind = 'entity'", ()),
        "notes": ("SELECT count(*) AS n FROM nodes WHERE type = 'note'", ()),
    }
    with pool.connection() as conn:
        return {k: conn.execute(q, p).fetchone()["n"] for k, (q, p) in queries.items()}


def _backup(dsn: str, *, out_dir: str, container: str) -> str:
    """pg_dump the whole database to `<out_dir>/<db>_pre_entity_reset_<ts>.sql`.

    Prefers a host `pg_dump`; falls back to `docker exec <container> pg_dump` when the client
    isn't on PATH (the DB commonly runs only in the container). Returns the file path.

    The password travels as PGPASSWORD in the child's environment, never in argv (which any
    user can read with `ps`); `docker exec -e PGPASSWORD` forwards it by name. The dump holds
    the whole database, so it is created 0600 inside a 0700 directory.
    """
    info = conninfo_to_dict(dsn)
    dbname = info.get("dbname", "kb")
    os.makedirs(out_dir, mode=0o700, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(out_dir, f"{dbname}_pre_entity_reset_{ts}.sql")
    argv_dsn, env = pg_child(dsn)
    old_umask = os.umask(0o077)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    finally:
        os.umask(old_umask)
    with os.fdopen(fd, "wb") as out:
        if shutil.which("pg_dump"):
            subprocess.run(["pg_dump", argv_dsn], check=True, stdout=out, env=env)
        else:
            subprocess.run(
                ["docker", "exec", "-e", "PGPASSWORD", container,
                 "pg_dump", "-U", info.get("user", "kb"), "-d", dbname],
                check=True, stdout=out, env=env,
            )
    return path


def main(argv: "list[str] | None" = None) -> int:
    p = argparse.ArgumentParser(
        prog="kb-reset-entities",
        description="Wipe the derived entity layer and flag memories for re-enrichment.")
    p.add_argument("--database-url", default=os.environ.get("KB_DATABASE_URL"))
    p.add_argument("--dry-run", action="store_true",
                   help="show what would be removed, then exit without changing anything")
    p.add_argument("-y", "--yes", action="store_true",
                   help="skip the interactive confirmation (for scripted use)")
    p.add_argument("--no-backup", action="store_true",
                   help="skip the pre-wipe pg_dump backup (NOT recommended)")
    p.add_argument("--no-requeue", action="store_true",
                   help="wipe only; don't flag memories 'pending' for re-enrichment")
    p.add_argument("--backup-dir", default="backups",
                   help="directory for the pre-wipe dump (default: ./backups)")
    p.add_argument("--db-container",
                   default=f"{os.environ.get('KB_COMPOSE_PROJECT') or 'kb_mcp'}_db",
                   help="docker container to dump from when pg_dump isn't on PATH "
                        "(default: <KB_COMPOSE_PROJECT>_db, i.e. kb_mcp_db)")
    args = p.parse_args(argv)

    if not args.database_url:
        p.error("provide --database-url or set KB_DATABASE_URL")

    pool = open_pool(args.database_url)
    try:
        before = _counts(pool)
        print("Entity layer to remove (memories are preserved):")
        for key in _REMOVED:
            print(f"  {key:24} {before[key]}")
        print(f"  {'notes (preserved)':24} {before['notes']}")
        if not args.no_requeue:
            print(f"\nWill flag {before['notes']} memory notes 'pending' for re-enrichment.")

        if args.dry_run:
            print("\n--dry-run: no changes made.")
            return 0

        if not args.yes:
            dbname = conninfo_to_dict(args.database_url).get("dbname", "")
            reply = input(
                f"\nDestructive + irreversible. Type the database name ({dbname!r}) to confirm: "
            ).strip()
            if reply != dbname:
                print("Aborted — confirmation did not match.", file=sys.stderr)
                return 1

        if not args.no_backup:
            print(f"Backup written: {_backup(args.database_url, out_dir=args.backup_dir, container=args.db_container)}")

        kb = KnowledgeBase(pool, default_registry())
        print(f"Wiped entities: {kb.reset_entities()}")
        if not args.no_requeue:
            print(f"Requeued {kb.requeue_enrichment()} memory notes (status='pending').")

        after = _counts(pool)
        print("Post-state: " + " ".join(f"{k}={after[k]}" for k in _REMOVED))
    finally:
        pool.close()

    print("\nDone. Start the ingest daemon (e.g. `kb-dev`) to rebuild the taxonomy; once "
          "enrichment drains, the consolidate daemon re-attaches entities to concepts "
          "automatically (churn trigger — no manual kb-consolidate needed).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
