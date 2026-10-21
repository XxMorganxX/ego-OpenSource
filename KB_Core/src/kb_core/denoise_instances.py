"""`kb-denoise-instances`: one-time reversible cleanup of code-symbol instances.

Runs the same two filters as the going-forward gate over every ACTIVE instance:
  1. the deterministic name gate (instance_name_issue, incl. code shapes), then
  2. the LLM meaningfulness judge (when an inference client is configured).
Flagged, non-pinned instances are DEMOTED to status='inactive' (reversible; hidden from the
graph) and their names blocked from re-promotion. Default is a dry preview; --apply commits.
"""
from __future__ import annotations

import argparse
import os

from kb_core.db import migrate_pool, open_pool
from kb_core.entities import instance_name_issue, normalize_entity_name
from kb_core.inference_routing import InferenceRouter
from kb_core.knowledge_base import KnowledgeBase
from kb_core.meaning_judge import SYSTEM_FILE, MEANING_SCHEMA, build_meaning_prompt, parse_meaning
from kb_core.registry import default_registry


def select_noise(kb: KnowledgeBase, client: "object | None") -> "list[dict]":
    """Flag active, non-pinned instances that are code symbols. origin 'name' (deterministic
    gate) or 'judge' (LLM). Pinned instances are always spared.

    The judge's system prompt is resolved ONCE, before the loop, only when a client is
    configured — a broken overlay (missing file, bad/cyclic include) is a configuration
    error and must abort the whole backfill before any row is judged, not be swallowed
    per-row by the inference fail-open path below."""
    system = kb.prompt(SYSTEM_FILE) if client is not None else None
    out: list[dict] = []
    for row in kb.active_instances():
        if row["pinned"]:
            continue
        reason = instance_name_issue(row["name"])
        origin = "name"
        if reason is None:
            if client is None:
                continue
            view = kb._entity_judge_view(row["id"])
            sys_msg, usr_msg = build_meaning_prompt(
                view["name"], "", view["mentions"], system=system)
            try:
                keep, reason = parse_meaning(client.complete(sys_msg, usr_msg, schema=MEANING_SCHEMA))
            except Exception:
                continue                       # fail-open: never demote on an inference error
            if keep:
                continue
            origin = "judge"
        out.append({"id": row["id"], "name": row["name"], "support": row["support"],
                    "reason": reason, "origin": origin})
    return out


def apply_noise(kb: KnowledgeBase, rows: "list[dict]") -> int:
    """Demote each flagged instance to inactive and block its name from re-promotion."""
    n = 0
    for r in rows:
        if kb.demote_instance(r["id"]):
            n += 1
        kb.block_instance_promotion(normalize_entity_name(r["name"]), r["reason"],
                                    origin="human" if r["origin"] == "name" else "judge")
    return n


def main() -> None:
    p = argparse.ArgumentParser(
        description="Demote code-symbol instances to inactive (reversible). "
                    "Default previews; --apply commits.")
    p.add_argument("--apply", action="store_true", help="demote flagged instances (else dry-run)")
    p.add_argument("--no-judge", action="store_true", help="deterministic name gate only")
    args = p.parse_args()

    pool = open_pool(os.environ["KB_DATABASE_URL"])
    migrate_pool(pool)
    kb = KnowledgeBase(pool, default_registry())
    client = None if args.no_judge else InferenceRouter().client_for("meaning_gate")

    rows = select_noise(kb, client)
    for r in rows:
        print(f"  [{r['origin']:5}] {r['name']}  (support={r['support']}) — {r['reason']}")
    print(f"\n{len(rows)} instance(s) flagged.")
    if args.apply:
        print(f"Demoted {apply_noise(kb, rows)} instance(s) to inactive (reversible).")
    else:
        print("Preview only — re-run with --apply to demote.")


if __name__ == "__main__":
    main()
