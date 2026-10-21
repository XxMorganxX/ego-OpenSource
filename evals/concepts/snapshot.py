"""Freeze the consolidation graph's inputs so every sweep arm reads identical data.

The corpus mutates continuously -- notes come and go overnight and entity instances can
tick up mid-query. Arms that read the DB at different moments are not comparable, so the
sweep reads once and replays.

Read-only. Uses KnowledgeBase's own queries rather than reimplementing them, so the
snapshot cannot drift from what production actually builds its graph from.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

from kb_core.db import open_pool
from kb_core.knowledge_base import KnowledgeBase
from kb_core.registry import default_registry

# Names blocked from ever becoming entities. Recurrence threshold picks out the names
# recurring in >=10 memories -- the ones whose suppression actually costs the graph
# linkage. How many names qualify drifts with the corpus; the threshold does not.
# Re-derive the count rather than trust a recorded one:
# `len({r["entity_id"] for r in blocked_mention_rows(blocked_candidates, live, min_memories=10)})`.
BLOCKED_MIN_MEMORIES = 10

_BLOCKED_SQL = """
    select ec.memory_id::text, ec.normalized_name, max(ec.confidence)::float
      from entity_candidates ec
      join entity_instance_blocks b on b.normalized_name = ec.normalized_name
      join nodes n on n.id = ec.memory_id and n.dead_at is null
     where ec.entity_type <> 'class'
     group by 1, 2
     order by 1, 2
"""

_LIVE_SQL = """
    select id::text from nodes
     where type = 'note' and dead_at is null
     order by id
"""


def capture(*, knn_k: int = 8, knn_min_sim: float = 0.55) -> dict:
    pool = open_pool(os.environ["KB_DATABASE_URL"])
    try:
        kb = KnowledgeBase(pool, default_registry())
        print(f"policy: {kb.policy.source_path or 'shipped defaults (no policy.toml)'}")
        if kb.policy.is_default:
            print("WARNING: running on shipped defaults (no decay rules, no [projects]) "
                  "— set KB_POLICY_CONFIG to mirror production")
        mention = [dict(r) if isinstance(r, dict) else r for r in kb.concept_mention_edges()]
        knn = [dict(r) if isinstance(r, dict) else r
               for r in kb.memory_knn_edges(k=knn_k, min_sim=knn_min_sim)]
        with pool.connection() as conn:
            # kb_core.db.open_pool configures the pool with psycopg's dict_row factory, so
            # rows are dicts keyed by column name, not tuples -- index by name, not position.
            # Unaliased columns take Postgres's default names: `id::text` -> "id",
            # `max(ec.confidence)::float` -> "max".
            live = [r["id"] for r in conn.execute(_LIVE_SQL)]
            blocked = [{"memory_id": r["memory_id"], "name": r["normalized_name"],
                        "confidence": r["max"]} for r in conn.execute(_BLOCKED_SQL)]
    finally:
        pool.close()
    return {
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "params": {"knn_k": knn_k, "knn_min_sim": knn_min_sim,
                   "blocked_min_memories": BLOCKED_MIN_MEMORIES},
        "mention": [{"memory_id": str(e["memory_id"]), "entity_id": str(e["entity_id"]),
                     "weight": None if e["weight"] is None else float(e["weight"]),
                     "support": int(e["support"])} for e in mention],
        "knn": [{"src": str(e["src"]), "dst": str(e["dst"]), "sim": float(e["sim"])}
                for e in knn],
        "live_ids": live,
        "blocked_candidates": blocked,
        "corpus_n": len(live),
    }


def save(snap: dict, path: "str | Path") -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snap))
    return path


def load(path: "str | Path") -> dict:
    return json.loads(Path(path).read_text())
