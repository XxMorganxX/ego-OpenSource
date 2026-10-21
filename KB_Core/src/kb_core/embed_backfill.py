"""`kb-embed-backfill` — embed every node that has no vector yet (one-shot).

The ingest/recrawl daemons only embed nodes they claim through their normal paths, so the
corpus that predates the embedding feature would never get a vector. This drains
`embedding IS NULL` in batches through the same EmbeddingEnricher the daemons use. Operates on
an already-migrated DB; does not start any daemon. Point KB_INFERENCE_URL at KB_Inference
(default http://127.0.0.1:8001); errors propagate (a one-shot should fail loudly).
"""

import argparse
import os

from kb_core.db import open_pool
from kb_core.inference_routing import InferenceRouter
from kb_core.ingest.embedding import EmbeddingEnricher
from kb_core.knowledge_base import KnowledgeBase
from kb_core.registry import default_registry


def backfill(kb, enricher, *, batch: int = 100, on_progress=None) -> int:
    """Embed unembedded nodes in batches until none remain. Returns the count embedded."""
    total = 0
    while True:
        nodes = kb.unembedded_nodes(limit=batch)
        if not nodes:
            break
        for node in nodes:
            enricher.enrich(node, kb)
            total += 1
        if on_progress:
            on_progress(total)
    return total


def main(argv: "list[str] | None" = None) -> int:
    p = argparse.ArgumentParser(
        prog="kb-embed-backfill",
        description="Embed every node that has no vector yet (one-shot).")
    p.add_argument("--database-url", default=os.environ.get("KB_DATABASE_URL"))
    p.add_argument("--batch", type=int, default=100, help="nodes per claim/embed loop")
    args = p.parse_args(argv)
    if not args.database_url:
        p.error("provide --database-url or set KB_DATABASE_URL")

    embedder = InferenceRouter(os.environ).embedder()
    pool = open_pool(args.database_url)
    try:
        kb = KnowledgeBase(pool, default_registry())
        enricher = EmbeddingEnricher(embedder)
        n = backfill(kb, enricher, batch=args.batch,
                     on_progress=lambda t: print(f"[embed-backfill] {t} embedded…", flush=True))
        print(f"[embed-backfill] done: {n} node(s) embedded.")
    finally:
        pool.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
