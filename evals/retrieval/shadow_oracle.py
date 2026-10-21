"""Exact-rank oracle for gold documents: where would a gold id sit in EACH candidate source
if nothing were truncated? (pool problem vs representation problem)

For every requested id, one read-only query computes, under the same filter block the
engine applies: the exhaustive cosine rank of its whole-note vector, of its best chunk, and
of greatest(note, chunk) (what the kNN arm would order by with an infinite pool), plus its
rank and the match count in the AND-form and OR-form lexical arms, and how many of the
query's lexemes the note contains.

    ../../KB_Core/.venv/bin/python shadow_oracle.py --set authored --item ga-001
    ../../KB_Core/.venv/bin/python shadow_oracle.py --set traffic --item tg-001 [--tier all]

`--tier` defaults to `all` here (an item is looked up by id), unlike shadow_bench.py.
Exit codes: 0 ran, 2 could not run (shadow.run_cli).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import shadow as S  # noqa: E402

_EXACT_SQL = """
WITH f AS (
    SELECT id, importance, embedding, content_tsv, last_accessed_at, ingested_at, importance_modifiers
    FROM nodes WHERE TRUE {filters}
), notes AS (
    SELECT id, 1 - (embedding <=> %(vec)s::vector) AS s FROM f WHERE embedding IS NOT NULL
), chunks AS (
    SELECT c.node_id AS id, max(1 - (c.embedding <=> %(vec)s::vector)) AS s
    FROM node_chunks c JOIN f ON f.id = c.node_id GROUP BY c.node_id
), best AS (
    SELECT id, max(s) AS s FROM (SELECT * FROM notes UNION ALL SELECT * FROM chunks) u GROUP BY id
), rb AS (SELECT id, s, rank() OVER (ORDER BY s DESC) AS r FROM best),
   rn AS (SELECT id, s, rank() OVER (ORDER BY s DESC) AS r FROM notes),
   rc AS (SELECT id, s, rank() OVER (ORDER BY s DESC) AS r FROM chunks),
   fa AS (
    SELECT id, ts_rank(content_tsv, websearch_to_tsquery('english', %(q_and)s)) * {imp} AS sc
    FROM f WHERE content_tsv @@ websearch_to_tsquery('english', %(q_and)s)
), rfa AS (SELECT id, sc, rank() OVER (ORDER BY sc DESC, id) AS r, count(*) OVER () AS n FROM fa),
   fo AS (
    SELECT id, ts_rank(content_tsv, websearch_to_tsquery('english', %(q_or)s)) * {imp} AS sc
    FROM f WHERE content_tsv @@ websearch_to_tsquery('english', %(q_or)s)
), rfo AS (SELECT id, sc, rank() OVER (ORDER BY sc DESC, id) AS r, count(*) OVER () AS n FROM fo),
   ids AS (SELECT unnest(%(ids)s::uuid[]) AS id)
SELECT ids.id::text AS id, (f.id IS NOT NULL) AS passes_filters,
       rb.r AS exact_knn_rank, rb.s AS exact_knn_sim,
       rn.r AS note_rank, rn.s AS note_sim, rc.r AS chunk_rank, rc.s AS chunk_sim,
       rfa.r AS fts_and_rank, (SELECT max(n) FROM rfa) AS fts_and_n,
       rfo.r AS fts_or_rank, (SELECT max(n) FROM rfo) AS fts_or_n,
       (SELECT count(*) FROM best) AS n_scored
FROM ids LEFT JOIN f ON f.id = ids.id
LEFT JOIN rb ON rb.id = ids.id LEFT JOIN rn ON rn.id = ids.id LEFT JOIN rc ON rc.id = ids.id
LEFT JOIN rfa ON rfa.id = ids.id LEFT JOIN rfo ON rfo.id = ids.id
"""

_COVERAGE_SQL = """
SELECT n.id::text AS id, length(n.content) AS clen,
       (SELECT count(*) FROM node_chunks c WHERE c.node_id = n.id) AS nchunks,
       n.dead_at IS NOT NULL AS dead, n.doc_kind, n.path::text AS path,
       (SELECT count(*) FROM unnest(tsvector_to_array(n.content_tsv)) l WHERE l = ANY(%(lex)s::text[])) AS lex_hit,
       cardinality(%(lex)s::text[]) AS lex_n
FROM nodes n WHERE n.id = ANY(%(ids)s::uuid[])"""


def exact_ranks(kb, query: str, args: dict, vec, ids: list, asof=None) -> dict:
    """{id: {...}} for each id (see module docstring). `asof` adds the as-of clause."""
    kw, limit = S.norm_args(args)
    k = {**S.KNOB_DEFAULTS, "asof": asof}
    p = S._base_params(kb, kw, limit, limit, k)
    # the OR form exactly as the engine builds it (its own `query.split()`)
    p.update(vec="[" + ",".join(repr(float(x)) for x in vec) + "]", ids=list(ids),
             q_and=query, q_or=S.or_join(S.fallback_terms(query, k)))
    sql = _EXACT_SQL.format(filters=S.filters_sql(k), imp=S._engine()._IMP_FACTOR)
    with kb._pool.connection() as c:
        rows = {r["id"]: dict(r) for r in c.execute(sql, p).fetchall()}
        lex = S.query_lexemes(c, query)
        cov = {r["id"]: dict(r) for r in c.execute(_COVERAGE_SQL, {"ids": list(ids), "lex": lex}).fetchall()}
    for i in rows:
        rows[i].update(cov.get(i, {}))
    return rows


def _fmt(v, nd=3):
    if v is None:
        return "-"
    return f"{v:.{nd}f}" if isinstance(v, float) else str(v)


def main(argv=None) -> int:
    return S.run_cli("shadow_oracle", _main, argv)


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--set", choices=["authored", "traffic"], required=True)
    ap.add_argument("--item", required=True, help="item id (authored `ga-…` / traffic `tg-…`)")
    ap.add_argument("--tier", default="all", choices=["adjudicated", "single-annotator", "all"])
    ap.add_argument("--prefix", default=S.QUERY_INSTRUCTION,
                    help="query-side instruction template with {q}; default is the shipped "
                         'instruction -- pass --prefix "" for the bare vector production used '
                         "before the instruction shipped")
    a = ap.parse_args(argv)
    kb = S.kb()
    print("readonly:", S.assert_readonly(kb))
    if a.set == "authored":
        items = {it["id"]: (it, args, must) for it, args, must in S.authored_search_items(S.load_authored_gold())}
        if a.item not in items:
            raise SystemExit(f"no search_memories item {a.item!r} with must gold")
        it, args, gold = items[a.item]
        query, asof = args.pop("query"), None
    else:
        skipped = []
        items = {it["id"]: it for it in S.load_traffic_gold(a.tier, skipped=skipped)}
        if a.item in skipped:
            raise SystemExit(f"traffic item {a.item!r} was not traced from search_memories; "
                             "the replica models that tool only")
        if a.item not in items:
            raise SystemExit(f"no traffic item {a.item!r} in tier {a.tier!r}")
        it = items[a.item]
        args = {k: v for k, v in it["args"].items() if k != "query"}
        query, asof = it["query"], it["at"]
        gold = [m["id"] for m in it["expected"].get("memories", [])]
    vec = S.embedder()(query, a.prefix)
    rows = exact_ranks(kb, query, args, vec, gold, asof=asof)
    print(f"{'gold':8s} {'filt':4s} {'exactKNN':>9} {'sim':>6} {'note_r':>6} {'chunk_r':>7} "
          f"{'ftsAND':>8} {'ftsOR':>9} {'lex':>5} {'chars/chunks':>12} dead")
    for gid in gold:
        r = rows.get(gid)
        if r is None:
            print(f"{gid[:8]:8s} MISSING FROM nodes")
            continue
        print(f"{gid[:8]:8s} {'ok' if r['passes_filters'] else 'FAIL':4s} "
              f"{_fmt(r['exact_knn_rank']):>9} {_fmt(r['exact_knn_sim']):>6} {_fmt(r['note_rank']):>6} "
              f"{_fmt(r['chunk_rank']):>7} {_fmt(r['fts_and_rank'])+'/'+_fmt(r['fts_and_n'] or 0):>8} "
              f"{_fmt(r['fts_or_rank'])+'/'+_fmt(r['fts_or_n'] or 0):>9} "
              f"{_fmt(r.get('lex_hit'))+'/'+_fmt(r.get('lex_n')):>5} "
              f"{_fmt(r.get('clen'))+'/'+_fmt(r.get('nchunks')):>12} {'D' if r.get('dead') else '.'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
