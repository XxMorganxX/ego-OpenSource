"""`kb-consolidate`: one concept-consolidation pass. Default previews the communities it
would persist; --apply rebuilds the derived concept layer (safe — concepts are derived
data and the next run replaces them); --list shows the current active layer.
Mirrors denoise_instances.py: preview-first, --apply commits."""
from __future__ import annotations

import argparse
import os

from kb_core.consolidate import build_consolidation_clients, run_once
from kb_core.db import migrate_pool, open_pool
from kb_core.knowledge_base import KnowledgeBase
from kb_core.registry import default_registry


def main() -> None:
    env = os.environ
    p = argparse.ArgumentParser(
        description="Discover + label concept communities over the KB. "
                    "Default previews; --apply commits.")
    p.add_argument("--apply", action="store_true", help="persist (else dry-run preview)")
    p.add_argument("--no-label", action="store_true",
                   help="skip the LLM labeler (placeholder names from top entities)")
    p.add_argument("--list", action="store_true", help="print active concepts and exit")
    p.add_argument("--knn-k", type=int,
                   default=int(env.get("KB_CONSOLIDATE_KNN_K", "8")))
    p.add_argument("--min-sim", type=float,
                   default=float(env.get("KB_CONSOLIDATE_MIN_SIM", "0.55")))
    p.add_argument("--resolution", type=float,
                   default=float(env.get("KB_CONSOLIDATE_RESOLUTION", "1.0")))
    p.add_argument("--min-size", type=int,
                   default=int(env.get("KB_CONSOLIDATE_MIN_SIZE", "3")))
    p.add_argument("--split-cohesion", type=float,
                   default=float(env.get("KB_CONSOLIDATE_SPLIT_COHESION", "0.5")),
                   help="a sub-community becomes a level-1 child only if this "
                        "fraction of the edge weight touching it stays internal")
    p.add_argument("--entity-share", type=float,
                   default=float(env.get("KB_CONSOLIDATE_ENTITY_SHARE", "0.25")),
                   help="an entity joins every concept holding >= this share "
                        "of its mention weight")
    p.add_argument("--hub-share", type=float,
                   default=float(env.get("KB_CONSOLIDATE_HUB_SHARE", "0.10")),
                   help="FALLBACK frequency cap, used only when --min-coherence-lift is "
                        "disabled: entities mentioned in more than this share of memories "
                        "are excluded from clustering (graph stopwords, retrieval "
                        "untouched); <= 0 disables the cap")
    p.add_argument("--min-coherence-lift", type=float,
                   default=float(env.get("KB_CONSOLIDATE_MIN_COHERENCE_LIFT", "0")),
                   help="an entity is a clustering stopword when its memories are less "
                        "alike than this multiple of the corpus-average memory pair "
                        "(1.0 = must beat chance). Judges what --hub-share only proxies "
                        "for; <= 0 falls back to the frequency cap")
    p.add_argument("--entity-alpha", type=float,
                   default=float(env.get("KB_CONSOLIDATE_ENTITY_ALPHA", "1.0")),
                   help="explicit entity:embedding mixing ratio -- scales every mention "
                        "edge, leaves kNN edges alone. 1.0 (default) is the historical "
                        "behaviour, whose 25/75 weight split was never chosen but fell "
                        "out of the damping curve")
    args = p.parse_args()
    hub_share = args.hub_share if args.hub_share > 0 else None
    min_lift = args.min_coherence_lift if args.min_coherence_lift > 0 else None

    pool = open_pool(env["KB_DATABASE_URL"])
    try:
        migrate_pool(pool)
        kb = KnowledgeBase(pool, default_registry())

        if args.list:
            for c in kb.active_concepts():
                indent = "  " if c["level"] else ""
                print(f"{indent}[{c['kind']:7}] {c['name']}  ({c['member_count']} members)")
            return

        if args.no_label:
            label_client = embedding_client = None
        else:
            label_client, embedding_client = build_consolidation_clients(env)
        r = run_once(kb, label_client, embedding_client=embedding_client,
                     knn_k=args.knn_k, knn_min_sim=args.min_sim,
                     resolution=args.resolution, min_memories=args.min_size,
                     split_cohesion=args.split_cohesion,
                     entity_share=args.entity_share, hub_share=hub_share,
                     min_coherence_lift=min_lift, entity_alpha=args.entity_alpha,
                     apply=args.apply, trigger_reason="cli")
        gate = (f"coherence lift>={min_lift}" if min_lift is not None
                else f"hub_share={hub_share}" if hub_share is not None else "no cap")
        print(f"graph: {r['vertices']} vertices / {r['edges']} edges — "
              f"{r['communities']} communities, {r['kept']} kept, "
              f"{r['discarded']} discarded, {r['hub_dropped']} hub-dropped ({gate})")
        if args.apply:
            print(f"applied run {r['run_id']}: created={r['created']} "
                  f"updated={r['updated']} retired={r['retired']}")
            return
        for u in r["preview"]:
            n_mem = sum(1 for _, k, _ in u["members"] if k == "memory")
            indent = "  " if u["level"] else ""
            print(f"{indent}[{u['kind']:7}] {u['name']}  "
                  f"({n_mem} memories, {len(u['members']) - n_mem} entities)")
        print(f"\nPreview only — re-run with --apply to persist "
              f"({len(r['retire'])} would retire).")
    finally:
        pool.close()


if __name__ == "__main__":
    main()
