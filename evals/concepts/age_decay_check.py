#!/usr/bin/env python3
"""Acceptance gate for the content-age decay. Read-only.

Reads the live consolidation inputs ONCE, partitions twice — decayed weights as
production now computes them, versus the same rows with factors forced flat — and
scores both against the golden set's co-membership pairs. Also prints the promotion
frontier: candidate groups whose raw support clears the threshold but whose decayed
support no longer does (these are the names decay is now holding back — eyeball that
they are genuinely dormant-thread names).

    KB_DATABASE_URL=postgresql://... \
        uv run --project KB_Core python -m evals.concepts.age_decay_check

PASS requires the decayed arm to keep every usable golden pair the flat arm keeps.
Both arms run hub_share=None and default detect_communities params — a differential
measurement of the decay alone, deliberately not a replay of production's full knob
set. One flipped pair is within the sweep's documented vertex-ordering noise floor
(evals/concepts/sweep.py CAVEATS) — rerun before reading a single flip as real."""
from __future__ import annotations

import os

from kb_core.aging import age_params, age_sql
from kb_core.concepts import MEMORY, build_graph, detect_communities
from kb_core.db import open_pool
from kb_core.knowledge_base import KnowledgeBase
from kb_core.registry import default_registry

from evals.concepts.golden_pairs import authored_pairs, usable_pairs

PROMOTION_THRESHOLD = 3

_FRONTIER_SQL = r"""
SELECT ec.entity_type, ec.normalized_name,
       count(DISTINCT ec.memory_id) AS raw_support,
       round(sum({age_mem})::numeric, 2) AS decayed_support,
       max(cls.kind) AS class_kind,
       bool_or(n.id IS NOT NULL) AS entity_exists
FROM entity_candidates ec
LEFT JOIN nodes mem ON mem.id = ec.memory_id
LEFT JOIN nodes n ON n.source = 'entity'
  AND n.external_id = 'instance:' || regexp_replace(ec.normalized_name, '[^a-z0-9]', '', 'g')
LEFT JOIN nodes cn ON cn.source = 'entity'
  AND cn.external_id = 'class:' || regexp_replace(lower(btrim(ec.entity_type)), '\s+', ' ', 'g')
LEFT JOIN entities cls ON cls.node_id = cn.id
WHERE ec.promoted = false AND ec.entity_type <> 'class'
GROUP BY ec.entity_type, ec.normalized_name
HAVING count(DISTINCT ec.memory_id) >= %(n)s AND sum({age_mem}) < %(n)s
ORDER BY raw_support DESC"""


def _kept(mention, knn, pairs):
    graph = build_graph(mention, knn, hub_share=None)
    comms = [c for c in detect_communities(graph) if c["level"] == 0]
    home: "dict[str, set[int]]" = {}
    for ci, c in enumerate(comms):
        for nid, kind, _score in c["members"]:
            if kind == MEMORY:
                home.setdefault(nid, set()).add(ci)
    return {p for p in pairs if home.get(p[0], set()) & home.get(p[1], set())}


def main() -> None:
    # The gate measures decay-ON by definition: honoring the dashboard switch would let a
    # toggled-off flag reduce the comparison to flat-vs-flat, which always passes. Forcing
    # it also lets the gate run against a live DB that predates migration 0044 (the
    # age_decay_state table) — this script must stay strictly read-only on production.
    KnowledgeBase.age_decay_enabled = lambda self: True  # type: ignore[method-assign]

    pool = open_pool(os.environ["KB_DATABASE_URL"])
    kb = KnowledgeBase(pool, default_registry())
    if not kb.policy.decay:
        raise SystemExit("age_decay_check: the loaded policy has no [[decay]] rules — "
                         "nothing to measure (KB_POLICY_CONFIG?)")
    mention = kb.concept_mention_edges()
    knn = kb.memory_knn_edges()
    live = ({str(r["src"]) for r in knn} | {str(r["dst"]) for r in knn}
            | {str(r["memory_id"]) for r in mention})
    pairs = usable_pairs(authored_pairs(), live)

    flat_mention = [dict(r, age_factor=1.0) for r in mention]
    flat_knn = [dict(r, weight=r["sim"]) for r in knn]
    kept_flat = _kept(flat_mention, flat_knn, pairs)
    kept_decayed = _kept(mention, knn, pairs)
    broken = kept_flat - kept_decayed

    print(f"usable golden pairs: {len(pairs)}")
    print(f"kept flat:           {len(kept_flat)}")
    print(f"kept decayed:        {len(kept_decayed)}")
    for a, b in sorted(broken):
        print(f"BROKEN: {a} <-> {b}")

    rules = kb.policy.decay
    sql = _FRONTIER_SQL.format(age_mem=age_sql("mem.event_at", "mem.tags", "mem.path",
                                               "mem.source", rules))
    with pool.connection() as conn:
        rows = conn.execute(sql, {"n": PROMOTION_THRESHOLD, **age_params(rules)}).fetchall()
    print(f"\npromotion frontier — held back by decay ({len(rows)} groups):")
    for r in rows:
        print(f"  {r['entity_type']} / {r['normalized_name']}: "
              f"raw {r['raw_support']} -> decayed {r['decayed_support']} "
              f"[kind={r['class_kind']}, exists={r['entity_exists']}]")

    print("\nPASS" if not broken else "\nFAIL")


if __name__ == "__main__":
    main()
