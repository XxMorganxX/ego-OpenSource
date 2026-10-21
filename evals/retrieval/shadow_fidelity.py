"""Fidelity proof for the shadow harness. Two parts, both read-only:

  A. replica == prod, id-for-id, on every authored-gold search query (limit 50), on
     recent real-traffic calls replayed with their own arguments, and on a fixed set of
     synthetic query-syntax cases (quoted phrases, negation, hyphens, a phrase that crosses
     the OR-fallback length cap) that real traffic has not exercised yet, run with one
     already-cached query vector. Any mismatch exits 1 -- the counterfactual knobs are only
     meaningful if the replica's baseline IS production.

  B. shadow(prod path, today) vs the result lists recorded in mcp_trace at call time:
     exact-order / same-set / Jaccard, by trace age. Divergences are expected and have known
     causes (nodes ingested or rewritten since, the access loop moving the FTS arm's
     importance multiplier, the corpus-global OR-fallback probe flipping when a later note
     AND-matches). The as-of rewind replays each divergent trace with (1) nodes that did not
     exist at the call (shadow.admissible_at) excluded from every arm AND from the fallback
     probe, (2) access_count rewound by the real-traffic hits recorded after the call --
     each returned search result and each get_memory once, including the call's own
     post-ranking bump -- and last_accessed_at rewound to the last prior hit. The access
     model is incomplete (e.g. concept-tool bumps are not in the traced results): ids
     rewound below zero are reported as `clamped`. What the rewind cannot undo (content
     rewritten since, re-embedding, those access gaps) remains as residual.

    ../../KB_Core/.venv/bin/python shadow_fidelity.py [--since-days 30] [--max-traces 60]
                                                      [--no-rewind] [--show-queries]
                                                      [--max-embeds 50]

Exit codes: 0 = replica proven == prod, 1 = a Part A mismatch, 2 = could not run
(configuration, gold, database, embedding service, or --max-embeds below what the run
needs -- a cold cache needs one embed per distinct query; the count is checked first).
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import shadow as S  # noqa: E402

_LATER_SQL = """
WITH later AS (
  SELECT at, tool, target_id, results FROM mcp_trace
  WHERE caller IS NULL AND at >= %(at)s::timestamptz
), hits AS (
  -- a get_memory row records its target in BOTH `results` and `target_id`, while the
  -- server bumps it once: count it through target_id only
  SELECT (r->>'id')::uuid AS id FROM later, jsonb_array_elements(results) r
  WHERE results IS NOT NULL AND jsonb_typeof(results) = 'array' AND tool <> 'get_memory'
  UNION ALL
  SELECT target_id FROM later WHERE tool = 'get_memory' AND target_id IS NOT NULL
)
SELECT id::text AS id, count(*) AS n FROM hits GROUP BY id"""

_CLAMPED_SQL = """
SELECT count(*) AS c FROM nodes
JOIN unnest(%(later_ids)s::uuid[], %(later_n)s::int[]) l(id, n) ON l.id = nodes.id
WHERE l.n > nodes.access_count"""

# Query-syntax edge cases for Part A (generic words only: this file is published). Each
# runs on both paths with the SAME already-cached vector, so they cost no embedding.
SYNTHETIC_QUERIES = (
    '"alpha bravo charlie delta" echo foxtrot golf hotel india',  # phrase across the length cap
    '"retrieval ranking" fusion weights',                          # short quoted phrase
    'search ranking -draft',                                       # websearch negation
    'kb-mcp server design',                                        # hyphenated token
    '"unterminated phrase search',                                 # unbalanced quote
    'harness',                                                     # single word (no fallback)
)

_PREV_SQL = """
WITH prev AS (
  SELECT at, tool, target_id, results FROM mcp_trace
  WHERE caller IS NULL AND at < %(at)s::timestamptz
), hits AS (
  SELECT (r->>'id')::uuid AS id, at FROM prev, jsonb_array_elements(results) r
  WHERE results IS NOT NULL AND jsonb_typeof(results) = 'array'
  UNION ALL
  SELECT target_id, at FROM prev WHERE tool = 'get_memory' AND target_id IS NOT NULL
)
SELECT id::text AS id, max(at) AS last FROM hits GROUP BY id"""


def _trace_args(t: dict) -> tuple:
    args = dict(t["args"])
    return args.pop("query"), args


def _traced_ids(t: dict) -> list:
    return [r["id"] for r in sorted(t["results"], key=lambda r: r["rank"])]


def _first_diff(p: list, r: list) -> str:
    i = next((i for i, (x, y) in enumerate(zip(p, r)) if x != y), None)
    return f"first diff at rank {i + 1}" if i is not None else f"length prod={len(p)} replica={len(r)}"


def part_a(kb, items, traces) -> int:
    """Count replica/prod id-order mismatches (0 == proof)."""
    bad = n = 0
    vec = None
    for it, a, _ in items:
        a = dict(a); a["limit"] = 50
        p = S.shadow_search(path="prod", **a); r = S.shadow_search(path="replica", **a); n += 1
        if p["ids"] != r["ids"]:
            bad += 1
            print(f"  MISMATCH authored {it['id']}: {_first_diff(p['ids'], r['ids'])}")
        if vec is None:
            vec = S.embedder()(a["query"], S.QUERY_INSTRUCTION)   # cached by the call above
    for t in traces:
        q, a = _trace_args(t)
        p = S.shadow_search(q, path="prod", **a); r = S.shadow_search(q, path="replica", **a); n += 1
        if p["ids"] != r["ids"]:
            bad += 1
            print(f"  MISMATCH trace #{t['id']}: {_first_diff(p['ids'], r['ids'])}")
        if vec is None:
            vec = S.embedder()(q, S.QUERY_INSTRUCTION)
    n_syn = 0
    if vec is not None:
        for q in SYNTHETIC_QUERIES:
            p = S.shadow_search(q, path="prod", embedding=vec, limit=50)
            r = S.shadow_search(q, path="replica", embedding=vec, limit=50)
            n += 1; n_syn += 1
            if p["ids"] != r["ids"]:
                bad += 1
                print(f"  MISMATCH synthetic {q!r}: {_first_diff(p['ids'], r['ids'])}")
    print(f"PART A: replica == prod id-for-id on {n - bad}/{n} queries "
          f"({len(items)} authored + {len(traces)} traces + {n_syn} synthetic query-syntax)")
    return bad


def rewound_search(kb, t: dict) -> tuple:
    """Replay a traced call with the corpus and access state rewound to the call time.
    Returns (ordered ids, number of ids whose later hits exceed their access_count, i.e.
    rewound past zero and clamped -- hits the traced access model cannot see)."""
    eng = S._engine()
    q, args = _trace_args(t)
    kw, limit = S.norm_args(args)
    pool = max(limit, eng.HYBRID_ARM_POOL)
    k = {**S.KNOB_DEFAULTS, "asof": str(t["at"]), "_limit": limit}
    vec = S.embedder()(q, S.served_prefix(t))
    with kb._pool.connection() as conn:
        later = {x["id"]: x["n"] for x in conn.execute(_LATER_SQL, {"at": t["at"]}).fetchall()}
        prev = {x["id"]: x["last"] for x in conn.execute(_PREV_SQL, {"at": t["at"]}).fetchall()}
        q_fts, fired = S.fts_query_text(kb, conn, q, kw, k)     # probe honours asof
        p = S._base_params(kb, kw, pool, pool, k)
        p.update(q=q_fts, later_ids=list(later), later_n=[later[i] for i in later],
                 prev_ids=list(prev), prev_at=[prev[i] for i in prev],
                 sat=kb._policy.importance.access_saturation)
        fts = conn.execute(f"""
            WITH later AS (SELECT unnest(%(later_ids)s::uuid[]) AS id, unnest(%(later_n)s::int[]) AS n),
                 prev AS (SELECT unnest(%(prev_ids)s::uuid[]) AS id, unnest(%(prev_at)s::timestamptz[]) AS last),
                 n AS (
                   SELECT nodes.id, content_tsv, ingested_at,
                          GREATEST(0, access_count - COALESCE(later.n, 0)) AS ac_asof, prev.last AS la_asof
                   FROM nodes LEFT JOIN later ON later.id = nodes.id LEFT JOIN prev ON prev.id = nodes.id
                   WHERE content_tsv @@ websearch_to_tsquery('english', %(q)s) {S.filters_sql(k)}
                 ), s AS (
                   SELECT id, ts_rank(content_tsv, websearch_to_tsquery('english', %(q)s)) *
                          (1.0 + (0.5 * ac_asof::float / (ac_asof + %(sat)s)) *
                             (%(floor)s + (1 - %(floor)s) *
                              exp(-%(ln2)s * EXTRACT(EPOCH FROM (%(asof)s::timestamptz - coalesce(la_asof, ingested_at))) / %(hl)s))) AS score
                   FROM n)
            SELECT id, score, row_number() OVER (ORDER BY score DESC, id) AS r FROM s ORDER BY r LIMIT %(pool)s""",
                           p).fetchall()
        knn = S.knn_arm(kb, conn, vec, kw, pool, k, annnotes=pool, annchunks=pool * eng.ANN_CHUNK_FANOUT)
        fused = S.rrf_fuse([([str(r["id"]) for r in fts], 1.0), ([str(r["id"]) for r in knn], 1.0)], eng.RRF_K)
        ordered = S.order_fused(fused, S.path_order(conn, list(fused)))
        clamped = conn.execute(_CLAMPED_SQL, p).fetchone()["c"]
    return ordered[:limit], int(clamped)


def part_b(kb, traces, rewind: bool, show_queries: bool) -> None:
    now = datetime.now().astimezone()
    buckets = {"<=7d": [], "<=30d": [], "all": []}
    divergent = []
    for t in traces:
        q, a = _trace_args(t)
        got = S.shadow_search(q, {"query_prefix": S.served_prefix(t)}, path="prod", **a)["ids"]
        traced = _traced_ids(t)
        rec = {"id": t["id"], "exact": got == traced, "set": set(got) == set(traced),
               "jaccard": len(set(got) & set(traced)) / max(1, len(set(got) | set(traced)))}
        age = (now - t["at"]).days
        for name, lim in (("<=7d", 7), ("<=30d", 30), ("all", 10 ** 6)):
            if age <= lim:
                buckets[name].append(rec)
        if not rec["exact"]:
            divergent.append((t, rec))
    print("PART B: shadow(today) vs recorded results")
    for name, recs in buckets.items():
        if recs:
            print(f"  {name:6s} n={len(recs):3d} exact={S.mean(r['exact'] for r in recs):.3f} "
                  f"same_set={S.mean(r['set'] for r in recs):.3f} jaccard={S.mean(r['jaccard'] for r in recs):.3f}")
    if not rewind or not divergent:
        return
    exact = same = clamped_traces = 0
    for t, rec in divergent:
        ordered, clamped = rewound_search(kb, t)
        traced = _traced_ids(t)
        e, s = ordered == traced, set(ordered) == set(traced)
        exact += e; same += s; clamped_traces += clamped > 0
        tail = f"  q={t['args']['query'][:60]!r}" if show_queries else ""
        print(f"    #{t['id']} {str(t['at'])[:16]} rewind exact={e} same_set={s} clamped={clamped} "
              f"(today: exact={rec['exact']} same_set={rec['set']} jaccard={rec['jaccard']:.2f}){tail}")
    print(f"  as-of rewind of {len(divergent)} divergent traces: exact {exact}, same set {same}; "
          f"{clamped_traces} rewound some ids past zero (access model incomplete: bumps the "
          f"traced results do not show); the rest of the residual = content rewritten / "
          f"re-embedded since the call")


def main(argv=None) -> int:
    return S.run_cli("shadow_fidelity", _main, argv)


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since-days", type=int, default=30)
    ap.add_argument("--max-traces", type=int, default=60)
    ap.add_argument("--no-rewind", action="store_true")
    ap.add_argument("--show-queries", action="store_true")
    ap.add_argument("--max-embeds", type=int, default=50,
                    help="budget of FRESH embedding calls (checked before any search)")
    a = ap.parse_args(argv)
    kb = S.kb()
    print("readonly:", S.assert_readonly(kb))
    S.embedder().max_calls = a.max_embeds
    items = S.authored_search_items(S.load_authored_gold())
    traces = S.recent_traces(kb, since_days=a.since_days, limit=a.max_traces)
    S.check_embed_budget([(a_["query"], S.QUERY_INSTRUCTION) for _, a_, _ in items] +
                         [(_trace_args(t)[0], S.QUERY_INSTRUCTION) for t in traces] +
                         [(_trace_args(t)[0], S.served_prefix(t)) for t in traces],
                         a.max_embeds)
    bad = part_a(kb, items, traces)
    part_b(kb, traces, rewind=not a.no_rewind, show_queries=a.show_queries)
    print(f"fresh embed calls: {S.embedder().calls}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
