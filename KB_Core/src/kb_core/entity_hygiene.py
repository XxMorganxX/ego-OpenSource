"""One-time entity-layer hygiene: fold typo categories, block junk categories,
backfill structural kinds, derive instance kinds. Preview-first — `plan_hygiene` is a pure
read: it computes what WOULD change and makes zero writes, so the default `kb-entity-hygiene`
run is safe against production. `--apply` (`apply_hygiene`) executes an already-computed plan.

Deliberately conservative: automatic folding only fires on deterministic typo detection
(`class_name_typo_of`); everything else (explicit merges, blocks) is a name the owner supplies
at apply time — nothing about which categories are "junk" is hard-coded here.
"""
from __future__ import annotations

import argparse
import os

from kb_core.db import migrate_pool, open_pool
from kb_core.entities import MENTIONS, class_name_typo_of
from kb_core.knowledge_base import KnowledgeBase
from kb_core.registry import default_registry


def _merge_wave_size(kb: KnowledgeBase, source: str) -> int:
    """Distinct memories that will be requeued for re-enrichment when `source` is merged away —
    mirrors `merge_classes`' own (unconditional) recrawl loop: every memory mentioning an
    instance filed under `source` re-fits its typing once the class is gone. Read-only."""
    seen: "set" = set()
    for inst in kb.instances_of(source):
        for n in kb.memories_linked(inst.node.id, link_type=MENTIONS):
            seen.add(n.id)
    return len(seen)


def _block_wave_size(kb: KnowledgeBase, name: str) -> int:
    """Distinct memories that will be requeued when `name` is blocked — mirrors `block_class`'s
    default `requeue_voters` population (`class_voters`: candidates + belongs_to). Read-only."""
    return len(kb.class_voters(name))


def _weight(row: "dict | None") -> "tuple[int, int, int]":
    """Rank a class row by how 'established' it is, for typo-fold direction: active beats
    inactive/blocked, then more belongs_to members, then more instances filed under it.
    A class name missing from the current snapshot (shouldn't happen — it comes from the
    same `list_entity_classes` call) sorts lowest."""
    if row is None:
        return (0, 0, 0)
    return (1 if row.get("status") == "active" else 0,
            row.get("members") or 0, row.get("instances") or 0)


def plan_hygiene(kb: KnowledgeBase, *, merge: "list[tuple[str, str]] | None" = None,
                 block: "list[str] | None" = None) -> dict:
    """Compute the hygiene plan — PURE READ, no writes. Returns
    {class_merges: [(source, target), ...], class_blocks: [name, ...],
     kind_backfills: [(name, kind), ...], warnings: [str, ...],
     merge_wave_sizes: {source: memory_count, ...}, block_wave_sizes: {name: memory_count, ...},
     alias_links_missing: int}.

    class_merges combines explicitly-requested folds (`merge`) with every automatic typo
    fold (`class_name_typo_of`) found among current class names. An explicit `merge`/`block`
    naming a class that doesn't exist is never executed (merge_classes/block_class would
    just no-op on it anyway) — it's reported in `warnings` instead of silently vanishing or
    raising, so a typo'd CLI arg is visible in the preview rather than crashing it.

    Operator visibility: merge_wave_sizes/block_wave_sizes preview the re-enrichment blast
    radius of each planned action — the same memory populations merge_classes/block_class would
    themselves requeue — so an operator sees the cost BEFORE running --apply. alias_links_missing
    previews `backfill_alias_links` (the alias-folding live-repair path): links already stripped
    by the pre-fix reconcile sweep (or its merge_instances sibling) that are reconstructible
    from the durable candidate ledger.
    """
    classes = kb.list_entity_classes()
    names = [c["name"] for c in classes]
    by_name = {c["name"]: c for c in classes}
    warnings: list[str] = []
    seen_pairs: set = set()
    class_merges: list[tuple[str, str]] = []

    def add_merge(source: str, target: str) -> None:
        key = frozenset((source.lower(), target.lower()))
        if key in seen_pairs:
            return
        seen_pairs.add(key)
        class_merges.append((source, target))

    # ---- explicit merges (validated; never crash on an unknown name) ----
    for src, tgt in merge or []:
        if src.lower() == tgt.lower():
            warnings.append(
                f"--merge-class {src}={tgt}: source and target are the same class, skipping")
            continue
        if kb.get_entity_class(src) is None:
            warnings.append(
                f"--merge-class {src}={tgt}: source class {src!r} does not exist, skipping")
            continue
        # A target that doesn't exist yet is NOT an error: merge_classes's rename semantics
        # (`upsert_entity_class(target, active=True)` before the fold) mean this legitimately
        # creates a new active class. Warn loudly rather than block it — an operator asking
        # to fold into a name that isn't there yet may be renaming on purpose, but a typo'd
        # target should be visible in the preview, not a silent new category.
        tgt_class = kb.get_entity_class(tgt)
        if tgt_class is None:
            warnings.append(
                f"--merge-class {src}={tgt}: merge target {tgt!r} does not exist — merge "
                f"will CREATE it as a new class")
        elif tgt_class.status == "blocked":
            warnings.append(
                f"--merge-class {src}={tgt}: merge target {tgt!r} is a blocked tombstone — "
                f"{src!r} would fold into an invisible class")
        add_merge(src, tgt)

    # ---- explicit blocks (validated; never crash on an unknown name) ----
    class_blocks: list[str] = []
    for name in block or []:
        cls = kb.get_entity_class(name)
        if cls is None:
            warnings.append(f"--block-class {name}: class {name!r} does not exist, skipping")
            continue
        if cls.status == "blocked":
            # Already tombstoned — not a pending action. block_class() would happily run
            # again (it re-fetches by name, not by status) and apply_hygiene would count it
            # as "done", which overstates what actually happened this run. Filter it out of
            # the plan here instead, so the preview doesn't list it and apply_hygiene never
            # sees it.
            warnings.append(f"--block-class {name}: class {name!r} is already blocked, skipping")
            continue
        class_blocks.append(name)

    # ---- typo folds: class_name_typo_of is roughly symmetric near a boundary case
    # (each of a pair can look like a typo of the other), so the direction is NOT "whichever
    # we happened to visit first" — it's decided by which class is more established
    # (_weight), so the same pair folds the same way regardless of iteration/DB order.
    for name in names:
        target = class_name_typo_of(name, [n for n in names if n != name])
        if target is None or target.lower() == name.lower():
            continue
        a, b = name, target
        wa, wb = _weight(by_name.get(a)), _weight(by_name.get(b))
        if wa == wb:
            source, dest = sorted((a, b))          # deterministic tie-break, direction is arbitrary
        elif wa > wb:
            source, dest = b, a
        else:
            source, dest = a, b
        add_merge(source, dest)

    # ---- merge-target/block collision: if a planned merge's target is itself scheduled to
    # be blocked THIS run, surface it. Behavior is unchanged — apply_hygiene still runs merges
    # before blocks, so the source folds into the target and then the target gets tombstoned
    # right after — but that sequence deserves to be visible in the preview, not discovered
    # after the fact.
    blocked_lower = {b.lower() for b in class_blocks}
    for source, target in class_merges:
        if target.lower() in blocked_lower:
            warnings.append(
                f"merge target {target!r} is scheduled to be blocked this run — "
                f"{source!r} will be folded into a tombstone")

    # ---- kind backfill: only classes with kind IS NULL that match a known seed. (Most
    # classes get their kind seeded at creation time by `upsert_entity_class`; this catches
    # the ones created before the structural-kind axis existed.)
    kind_for = kb.policy.taxonomy.kind_for
    kind_backfills = [(c["name"], kind_for(c["name"])) for c in classes
                      if not c.get("kind") and kind_for(c["name"])]

    # ---- Expected re-enrichment wave sizes for each planned action (read-only) ----
    merge_wave_sizes = {source: _merge_wave_size(kb, source) for source, _ in class_merges}
    block_wave_sizes = {name: _block_wave_size(kb, name) for name in class_blocks}

    return {"class_merges": class_merges, "class_blocks": class_blocks,
            "kind_backfills": kind_backfills, "warnings": warnings,
            "merge_wave_sizes": merge_wave_sizes, "block_wave_sizes": block_wave_sizes,
            "alias_links_missing": kb.count_alias_link_backfill()}


def apply_hygiene(kb: KnowledgeBase, plan: dict) -> dict:
    """Execute a plan produced by `plan_hygiene`, in order: merges -> blocks -> alias-link
    backfill -> kind backfill -> instance kind derive. Counts reflect actions that actually did
    something (a merge of an already-gone class no-ops rather than crashing, and isn't counted).

    Blocks get an extra honesty check: `block_class` resolves its target by name, not by
    status, so calling it on a class that's ALREADY `status='blocked'` still returns a
    non-None Entity (it just redundantly re-runs the tombstone steps) — counting that as
    `class_blocks` would overstate what this run actually changed. `plan_hygiene` already
    filters known-blocked explicit targets out of `class_blocks` before this ever runs, but a
    hand-built or stale plan (e.g. the class got blocked between planning and applying) could
    still hand one in, so this checks status before calling and counts a no-op transition
    separately (`class_blocks_noop`) rather than either inflating `class_blocks` or silently
    dropping the action from the report.

    `merge_reenrich_total`/`block_reenrich_total` are the ACTUAL re-enrichment wave sizes,
    measured right before each action runs (not copied from the plan — the DB may have moved
    between plan and apply, and `merge_classes` purges the source's instances/candidates as part
    of the merge, so the population must be counted before, not after).

    Ordering rationale for the alias-link backfill: it runs AFTER merges/blocks so it repairs
    against the CURRENT (post-merge) alias set — a class merge doesn't touch instance aliases,
    but running it last-of-the-writes keeps this pass reading the most up-to-date entity state
    available this run. Its position relative to kind backfill/instance-kind derivation is
    inconsequential (the two touch disjoint columns — entity_links vs. entities.kind/entities.
    kind derivation from entity_class_links), so it is placed before them simply to keep the two
    "restore/repair" steps (alias links, then kind derivation) grouped together at the end."""
    done = {"class_merges": 0, "class_blocks": 0, "class_blocks_noop": 0,
            "kind_backfills": 0, "instance_kinds": 0,
            "merge_reenrich_total": 0, "block_reenrich_total": 0, "alias_links_backfilled": 0}
    for source, target in plan["class_merges"]:
        wave = _merge_wave_size(kb, source)        # measured BEFORE the merge purges it
        if kb.merge_classes(source, target) is not None:
            done["class_merges"] += 1
            done["merge_reenrich_total"] += wave
    for name in plan["class_blocks"]:
        cls = kb.get_entity_class(name)
        if cls is not None and cls.status == "blocked":
            done["class_blocks_noop"] += 1
            continue
        wave = _block_wave_size(kb, name)           # measured BEFORE the block drops candidates
        if kb.block_class(name, purge_instances=False) is not None:
            done["class_blocks"] += 1
            done["block_reenrich_total"] += wave
    done["alias_links_backfilled"] = kb.backfill_alias_links()
    for name, kind in plan["kind_backfills"]:
        kb.set_class_kind(name, kind)
        done["kind_backfills"] += 1
    done["instance_kinds"] = kb.derive_instance_kinds()
    return done


def _parse_merge_arg(value: str) -> "tuple[str, str]":
    if "=" not in value:
        raise argparse.ArgumentTypeError(f"expected SRC=TGT, got {value!r}")
    src, _, tgt = value.partition("=")
    return src.strip(), tgt.strip()


def main() -> None:
    p = argparse.ArgumentParser(
        description="One-time entity-layer cleanup: fold typo class names, block named junk "
                    "categories, backfill structural kinds. Default previews; --apply commits.")
    p.add_argument("--apply", action="store_true", help="execute the plan (else preview only)")
    p.add_argument("--merge-class", action="append", default=[], type=_parse_merge_arg,
                   dest="merge_class", metavar="SRC=TGT",
                   help="explicitly fold SRC into TGT (repeatable)")
    p.add_argument("--block-class", action="append", default=[],
                   dest="block_class", metavar="NAME",
                   help="tombstone a named junk category (repeatable)")
    args = p.parse_args()

    db_url = os.environ.get("KB_DATABASE_URL")
    if not db_url:
        raise SystemExit("KB_DATABASE_URL is required")

    pool = open_pool(db_url)
    migrate_pool(pool)
    kb = KnowledgeBase(pool, default_registry())

    plan = plan_hygiene(kb, merge=args.merge_class, block=args.block_class)
    for w in plan["warnings"]:
        print(f"  [warn] {w}")
    for source, target in plan["class_merges"]:
        wave = plan["merge_wave_sizes"].get(source, 0)
        print(f"  merge class:    {source} -> {target}  "
              f"(~{wave} memor{'y' if wave == 1 else 'ies'} to re-enrich)")
    for name in plan["class_blocks"]:
        wave = plan["block_wave_sizes"].get(name, 0)
        print(f"  block class:    {name}  (~{wave} memor{'y' if wave == 1 else 'ies'} to re-enrich)")
    for name, kind in plan["kind_backfills"]:
        print(f"  backfill kind:  {name} -> {kind}")
    if plan["alias_links_missing"]:
        print(f"  alias links:    {plan['alias_links_missing']} mention link(s) restorable "
              f"via backfill_alias_links")
    print(f"\n{len(plan['class_merges'])} merge(s), {len(plan['class_blocks'])} block(s), "
          f"{len(plan['kind_backfills'])} kind backfill(s) planned.")

    if args.apply:
        done = apply_hygiene(kb, plan)
        print(f"Applied: {done['class_merges']} merge(s) "
              f"({done['merge_reenrich_total']} memories re-enriched), "
              f"{done['class_blocks']} block(s) "
              f"({done['block_reenrich_total']} memories re-enriched), "
              f"{done['kind_backfills']} kind backfill(s), "
              f"{done['instance_kinds']} instance kind(s) derived, "
              f"{done['alias_links_backfilled']} alias link(s) restored.")
        if done["class_blocks_noop"]:
            print(f"  ({done['class_blocks_noop']} block(s) skipped — already blocked)")
    else:
        print("Preview only — re-run with --apply to execute.")


if __name__ == "__main__":
    main()
