#!/usr/bin/env python
"""Manual smoke test: do entity nodes actually get embedded via the LIVE /embed API?

Every automated test mocks the embed client, so this is the one check that exercises the
REAL path end to end:

    KB_Core  ->  HTTPInferenceClient.embed  ->  KB_Inference POST /embed (qwen3-embedding)
             ->  a vector persisted in entities.embedding (pgvector, dim 1024)

Run it against a live stack — Postgres up (pgvector) AND KB_Inference serving /embed with the
embed model pulled (`ollama pull qwen3-embedding:0.6b`):

    KB_DATABASE_URL=postgres://…  KB_INFERENCE_URL=http://127.0.0.1:8000 \
        uv run python scripts/smoke_entity_embed.py

    # also run the scan + LLM judge (needs the chat model too):
    … uv run python scripts/smoke_entity_embed.py --full

Like the daemon, it applies any pending migrations. It only RE-EMBEDS existing entities (and,
with --full, stages merge suggestions); it never deletes, tombstones, or merges anything.

Exit 0 = embeddings landed (or the endpoint works and there are no entities yet).
Exit 1 = something is wrong: /embed unreachable, dimension mismatch, or no vectors written.
"""

from __future__ import annotations

import argparse
import os
import sys

from kb_core.db import migrate_pool, open_pool
from kb_core.entity_resolve import run_once
from kb_core.ingest.inference import HTTPInferenceClient
from kb_core.knowledge_base import KnowledgeBase
from kb_core.registry import default_registry

EXPECTED_DIM = 1024  # must match migration 0019's `embedding vector(1024)`


def _entity_embed_stats(pool) -> tuple[int, int, int]:
    """(total entities, entities with a non-null embedding, entities still embed_dirty)."""
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) AS total, "
            "count(*) FILTER (WHERE embedding IS NOT NULL) AS embedded, "
            "count(*) FILTER (WHERE embed_dirty) AS dirty FROM entities"
        ).fetchone()
    return row["total"], row["embedded"], row["dirty"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--database-url", default=os.environ.get("KB_DATABASE_URL"))
    ap.add_argument("--inference-url", default=os.environ.get("KB_INFERENCE_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--batch", type=int, default=int(os.environ.get("KB_ENTITY_EMBED_BATCH", "32")))
    ap.add_argument("--full", action="store_true",
                    help="also run the near-neighbor scan + LLM judge (needs the chat model)")
    args = ap.parse_args()
    if not args.database_url:
        print("ERROR: set KB_DATABASE_URL (or pass --database-url)", file=sys.stderr)
        return 1

    client = HTTPInferenceClient(args.inference_url)

    # 1) Prove the live /embed endpoint returns a vector, and report its dimension.
    print(f"[smoke] probing {args.inference_url}/embed …")
    try:
        h = client.health()
        print(f"[smoke] inference health: ollama={h.get('ollama')} embedding={h.get('embedding')}")
    except Exception as exc:  # noqa: BLE001 - health is best-effort
        print(f"[smoke] WARNING: /health unreachable ({exc}); trying /embed anyway")
    try:
        vec = client.embed(["smoke test: does the embedding API return a vector?"])[0]
    except Exception as exc:  # noqa: BLE001 - surface the transport error plainly
        print(f"[smoke] FAIL: /embed call failed: {exc}", file=sys.stderr)
        return 1
    dim = len(vec)
    print(f"[smoke] /embed returned a {dim}-dim vector ✓")
    if dim != EXPECTED_DIM:
        print(f"[smoke] FAIL: dim {dim} != migration's vector({EXPECTED_DIM}); reembed cannot store these. "
              "Align OLLAMA_EMBED_DIMENSIONS (KB_Inference) or the migration's column dimension.",
              file=sys.stderr)
        return 1

    # 2) Re-embed the real entity set through the same path and confirm vectors land in the DB.
    pool = open_pool(args.database_url)
    migrate_pool(pool)
    kb = KnowledgeBase(pool, default_registry())

    total, embedded_before, dirty_before = _entity_embed_stats(pool)
    print(f"[smoke] entities before: total={total} embedded={embedded_before} dirty={dirty_before}")
    if total == 0:
        print("[smoke] PASS: /embed works; the KB has no entities yet, so nothing to embed.")
        return 0

    if args.full:
        print(f"[smoke] running full run_once (reembed + scan + judge), batch={args.batch} …")
        print(f"[smoke] run_once: {run_once(kb, client, batch=args.batch)}")
    else:
        n = kb.reembed_dirty_entities(client, batch=args.batch)
        print(f"[smoke] reembed_dirty_entities: {n} entit(y/ies) processed")

    total, embedded_after, dirty_after = _entity_embed_stats(pool)
    print(f"[smoke] entities after:  total={total} embedded={embedded_after} dirty={dirty_after}")

    # Sample one stored vector to confirm the real dimension persisted in pgvector.
    sample_dim = None
    for e in kb.list_entities():
        stored = kb.get_embedding(e.node.id)
        if stored is not None:
            sample_dim = len(stored)
            print(f"[smoke] sample stored embedding: '{e.canonical_name}' -> {sample_dim}-dim ✓")
            break

    ok = embedded_after > 0 and sample_dim == EXPECTED_DIM
    print(f"[smoke] {'PASS' if ok else 'FAIL'}: {embedded_after}/{total} entities embedded, "
          f"stored dim={sample_dim}, dirty remaining={dirty_after}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
