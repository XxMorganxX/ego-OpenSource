#!/usr/bin/env python3
# evals/extraction/capture.py
"""Freeze a stratified sample of memories — and their prompt context — into JSONL.

The extract_entities user template interpolates {entity_classes} (the live taxonomy
block) and {existing_instances}. Both drift as the graph grows, so a harness that read
them live would measure drift instead of the model. We capture them once, here, and every
later run replays the frozen values.

Provenance rule (mirrors evals/retrieval): inputs come from direct DB inspection, never
from the pipeline being evaluated.

Usage: capture.py [--sample-n 200] [--gold-n 40] [--seed 0.42] [--out DIR [--force]]
Run with KB_Core's venv python (needs kb_core + psycopg).

The output is real corpus text, so it never lands in an exported path by
default: --out defaults to $KB_EVAL_DATA_DIR/extraction (refused when that is
unset), and any directory inside this repo but outside private/ is refused
unless --force. evals/extraction/ itself is published; its gold/sample files
there are synthetic.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from kb_core.ingest.llm import LLMEnricher, load_passes
from kb_core.knowledge_base import KnowledgeBase
from kb_core.paths import humanize_path
from kb_core.registry import default_registry

DSN = os.environ.get("KB_DATABASE_URL", "postgresql://kb_app@127.0.0.1:5433/kb_mcp")

# Every connection this pool hands out refuses writes at the session level -- not just
# the one cursor running SAMPLE_SQL. `default_transaction_read_only=on` is set via libpq
# startup options, before any query runs, so a write attempt anywhere in this script (or
# in KnowledgeBase/LLMEnricher internals) errors loudly instead of silently succeeding
# against the live production DB. This is a read-only *eval* script -- it has no business
# writing to kb_mcp, and unlike a per-cursor "SET TRANSACTION READ ONLY" this can't be
# bypassed by a connection this script didn't think to guard.
POOL_KWARGS = {
    "row_factory": dict_row,
    "options": "-c default_transaction_read_only=on",
}

# Stratify over doc kind and length so the sample isn't all short notes of one type.
# setseed() makes the sample reproducible; re-running with the same seed re-selects it.
# doc_kind, entity_eligible and dead_at are plain nodes columns (verified against the
# live schema; no facet-table join needed). doc_kind is left as a genuine SQL NULL (no
# COALESCE) so it comes through as JSON `null`, matching the gold schema's `str | None` field.
SAMPLE_SQL = """
SELECT n.id, n.title, n.path::text AS path, n.content,
       n.doc_kind,
       length(n.content) AS content_chars,
       ntile(3) OVER (ORDER BY length(n.content)) AS len_bucket
FROM nodes n
WHERE n.content IS NOT NULL
  AND length(n.content) > 200
  AND n.dead_at IS NULL
  AND n.entity_eligible
ORDER BY random()
"""


def _kind_key(doc_kind: "str | None") -> tuple:
    """Sort key that tolerates a None doc_kind mixed with strings (None sorts first)."""
    return (doc_kind is None, doc_kind or "")


def stratified(rows: list[dict], n: int) -> list[dict]:
    """Round-robin across (doc_kind, len_bucket) strata until we have n rows.

    A None doc_kind is a valid, hashable bucket key -- it just needs a None-tolerant
    sort, since `sorted()` can't compare None to str directly.
    """
    buckets: dict[tuple, list[dict]] = {}
    for r in rows:
        buckets.setdefault((r["doc_kind"], r["len_bucket"]), []).append(r)
    picked = []
    keys = sorted(buckets, key=lambda k: (_kind_key(k[0]), k[1]))
    while len(picked) < n and any(buckets[k] for k in keys):
        for k in keys:
            if buckets[k] and len(picked) < n:
                picked.append(buckets[k].pop())
    return picked


def largest_remainder(counts: dict, total: int) -> dict:
    """Apportion `total` seats across `counts` proportionally (Hamilton / largest-remainder
    method): give each key floor(share), then hand the leftover seats one each to the keys
    with the largest fractional remainder, largest first. Sums to exactly `total` (when
    `total <= sum(counts.values())`) with no arbitrary bias from truncation alone.
    """
    grand = sum(counts.values())
    if grand == 0 or total <= 0:
        return {k: 0 for k in counts}
    exact = {k: v * total / grand for k, v in counts.items()}
    alloc = {k: int(e) for k, e in exact.items()}
    remainder = total - sum(alloc.values())
    order = sorted(counts, key=lambda k: (-(exact[k] - alloc[k]), _kind_key(k)))
    for k in order[:remainder]:
        alloc[k] += 1
    return alloc


def pick_gold(rows: list[dict], n: int) -> tuple[list[dict], list[dict]]:
    """Draw `n` gold records proportionally by doc_kind over the eligible pool, using
    largest-remainder allocation so the rounding is fair, then spread each kind's picks
    across its length terciles (round-robin) so length variety survives within a kind.

    Returns (gold, remaining) -- `remaining` is what's left for the round-robin
    `stratified()` sample draw, so the two sets stay disjoint by construction.
    """
    by_kind: dict = {}
    for r in rows:
        by_kind.setdefault(r["doc_kind"], []).append(r)
    counts = {k: len(v) for k, v in by_kind.items()}
    alloc = largest_remainder(counts, n)

    gold: list[dict] = []
    for kind in sorted(by_kind, key=_kind_key):
        want = alloc.get(kind, 0)
        if want <= 0:
            continue
        by_len: dict = {}
        for r in by_kind[kind]:
            by_len.setdefault(r["len_bucket"], []).append(r)
        len_keys = sorted(by_len)
        picked_here: list[dict] = []
        while len(picked_here) < want and any(by_len[k] for k in len_keys):
            for k in len_keys:
                if by_len[k] and len(picked_here) < want:
                    picked_here.append(by_len[k].pop())
        gold.extend(picked_here)

    gold_ids = {r["id"] for r in gold}
    remaining = [r for r in rows if r["id"] not in gold_ids]
    return gold, remaining


REPO_ROOT = Path(__file__).resolve().parents[2]


class OutRefused(ValueError):
    """The output directory could put live-DB text into a published path."""


def resolve_out(arg: str | None, *, env=os.environ, force: bool = False) -> Path:
    """Where capture writes. Fail closed: no implicit in-repo default, and an
    in-repo directory must be under private/ (hard-denied from every export)
    unless the operator says --force."""
    if arg is None:
        data = env.get("KB_EVAL_DATA_DIR", "").strip()
        if not data:
            raise OutRefused("no --out and KB_EVAL_DATA_DIR is unset — set KB_EVAL_DATA_DIR "
                             "(e.g. <repo>/private/evals) or pass --out; the old default, "
                             "this script's own directory, is published")
        arg = str(Path(data) / "extraction")
    out = Path(arg).expanduser().resolve()
    root = REPO_ROOT
    if out.is_relative_to(root) and not out.is_relative_to(root / "private") and not force:
        raise OutRefused(f"{out} is inside the repo but outside private/ — captures hold real "
                         "memories and that path may be published; use private/ or a "
                         "directory outside the repo, or pass --force")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample-n", type=int, default=200)
    ap.add_argument("--gold-n", type=int, default=40)
    ap.add_argument("--seed", type=float, default=0.42)
    ap.add_argument("--out", default=None,
                    help="output dir (default: $KB_EVAL_DATA_DIR/extraction)")
    ap.add_argument("--force", action="store_true",
                    help="allow an --out inside the repo but outside private/")
    args = ap.parse_args(argv)
    try:
        out = resolve_out(args.out, force=args.force)
    except OutRefused as e:
        print(f"capture.py: REFUSED — {e}", file=sys.stderr)
        return 2

    # Read-only pool: no migrate_pool call here. This is an eval script, not a schema
    # bootstrapper -- the database is already migrated by the running services, and a
    # read-only script has no business running arbitrary DDL/DML against production.
    # `default_transaction_read_only=on` in POOL_KWARGS makes every connection this pool
    # hands out reject writes at the session level.
    pool = ConnectionPool(DSN, kwargs=POOL_KWARGS, open=True)
    try:
        kb = KnowledgeBase(pool, default_registry())
        # Reach for the private _class_list deliberately: the {entity_classes} block must be
        # byte-identical to what production builds, and re-implementing its format here would
        # let the two drift silently.
        enricher = LLMEnricher(client=None, passes=load_passes())
        classes_block = enricher._class_list(kb)

        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT setseed(%s)", (args.seed,))
            cur.execute(SAMPLE_SQL)
            rows = cur.fetchall()  # dict rows (POOL_KWARGS sets row_factory=dict_row)

        total = args.sample_n + args.gold_n
        if len(rows) < total:
            print(f"WARNING: corpus yielded {len(rows)} rows, wanted {total}", file=sys.stderr)

        # Gold is drawn proportionally to the corpus's doc_kind distribution first, so a
        # rare kind isn't given the same weight as the most common one. Sample then draws
        # from whatever's left over, using the original round-robin stratification for
        # broad doc-kind coverage -- disjoint by construction (pick_gold removes its picks
        # from the pool sample() draws from).
        gold_rows, remaining_rows = pick_gold(rows, args.gold_n)
        if len(gold_rows) < args.gold_n:
            print(f"WARNING: gold draw yielded {len(gold_rows)} rows, wanted {args.gold_n}",
                  file=sys.stderr)
        sample_rows = stratified(remaining_rows, args.sample_n)
        if len(sample_rows) < args.sample_n:
            print(f"WARNING: sample draw yielded {len(sample_rows)} rows, wanted {args.sample_n}",
                  file=sys.stderr)

        captured_at = datetime.now(timezone.utc).isoformat()

        def to_record(r: dict) -> dict:
            return {
                "id": str(r["id"]),
                "title": r["title"] or "",
                "path": r["path"],
                "location": humanize_path(r["path"]),
                "content": r["content"],
                "entity_classes": classes_block,
                "existing_instances": kb.memory_instance_summary(r["id"]),
                "doc_kind": r["doc_kind"],
                "content_chars": r["content_chars"],
                "captured_at": captured_at,
            }

        gold = [to_record(r) for r in gold_rows]
        sample = [to_record(r) for r in sample_rows]
    finally:
        pool.close()

    out.mkdir(parents=True, exist_ok=True)
    with (out / "gold.jsonl").open("w") as f:
        for rec in gold:
            f.write(json.dumps({**rec, "gold_anchors": []}) + "\n")
    with (out / "sample.jsonl").open("w") as f:
        for rec in sample:
            f.write(json.dumps(rec) + "\n")
    print(f"wrote {len(gold)} gold + {len(sample)} sample records to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
