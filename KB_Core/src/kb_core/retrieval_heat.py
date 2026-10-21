"""Per-memory retrieval heatmap over the MCP trace log.

Every traced MCP call stores the rows it returned in `mcp_trace.results`
([{id, kind, rank, ...}]). This module counts, per memory and per time bucket,
how often agents *surfaced* a memory (any traced tool returned it) versus
*opened* it (`get_memory` returned it). Read-only: nothing here writes, so it adds
no `nodes` churn and needs no eval snapshot/restore.

Buckets are the viewer's wall-clock days/weeks: everything is computed on local
`timestamp`s (`at AT TIME ZONE tz`), so a DST change can neither skip nor
duplicate a bucket.
"""

import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psycopg

from kb_core.entities import ENTITY_ROOT, ENTITY_SOURCE

BUCKETS = ("day", "week")
MAX_LIMIT = 1000
_LTREE_PATH = re.compile(r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*$")

# The trailing window, as local timestamps. `days = NULL` means "all history":
# start at the earliest qualifying trace's bucket, or the current bucket when
# there is none.
_WINDOW = """
    WITH params AS (SELECT (now() AT TIME ZONE %(tz)s) AS local_now),
    win AS (
      SELECT date_trunc(%(bucket)s,
               CASE WHEN %(days)s::int IS NULL THEN coalesce(
                   (SELECT min(t.at) AT TIME ZONE %(tz)s FROM mcp_trace t
                    WHERE t.results IS NOT NULL
                      AND (%(include_evals)s OR t.caller IS NULL)),
                   p.local_now)
               ELSE p.local_now - make_interval(days => %(days)s::int - 1) END)
               AS start_local,
             date_trunc(%(bucket)s, p.local_now) AS end_local
      FROM params p)
"""

_BUCKETS_SQL = _WINDOW + """
    SELECT gs::date AS bucket
    FROM win, generate_series(win.start_local, win.end_local, %(step)s::interval) gs
    ORDER BY 1
"""

# hits: one row per distinct (trace, memory) -- a memory listed twice by one call
# (e.g. under two `expand` groups) counts once. The uuid regex runs in WHERE, so
# the ::uuid cast in the select list only ever sees well-formed ids.
_ROWS_SQL = _WINDOW + """,
    traces AS (
      -- Filter before the lateral: Postgres may evaluate WHERE terms in any order
      -- relative to a LATERAL function, so a non-array `results` must never reach
      -- jsonb_array_elements. `t.at >= ...` compares the instant, so it can use
      -- mcp_trace_at_idx.
      SELECT t.id, t.tool, t.at, t.results
      FROM mcp_trace t CROSS JOIN win
      WHERE t.results IS NOT NULL AND jsonb_typeof(t.results) = 'array'
        AND (%(include_evals)s OR t.caller IS NULL)
        AND t.at >= (win.start_local AT TIME ZONE %(tz)s)),
    hits AS (
      SELECT DISTINCT t.id AS trace_id, (e->>'id')::uuid AS node_id,
             t.tool = 'get_memory' AS opened,
             date_trunc(%(bucket)s, t.at AT TIME ZONE %(tz)s)::date AS bucket
      FROM traces t
      CROSS JOIN LATERAL jsonb_array_elements(t.results) e
      WHERE e->>'kind' = 'memory'
        AND e->>'id' ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'),
    per_cell AS (
      SELECT h.node_id, h.bucket,
             count(*) FILTER (WHERE NOT h.opened) AS surfaced,
             count(*) FILTER (WHERE h.opened) AS opened
      FROM hits h JOIN nodes n ON n.id = h.node_id
      WHERE n.type <> 'folder' AND n.source <> %(entity_source)s
        AND NOT (n.path <@ %(entity_root)s::ltree)
        AND (%(path)s::ltree IS NULL OR n.path <@ %(path)s::ltree)
      GROUP BY 1, 2),
    per_node AS (
      SELECT node_id, sum(surfaced)::int AS surfaced, sum(opened)::int AS opened,
             jsonb_agg(jsonb_build_object('bucket', bucket, 'surfaced', surfaced,
                                          'opened', opened) ORDER BY bucket) AS cells
      FROM per_cell GROUP BY node_id)
    SELECT pn.node_id AS id, n.title, n.path::text AS path, pn.surfaced, pn.opened,
           pn.cells, count(*) OVER () AS total_memories
    FROM per_node pn JOIN nodes n ON n.id = pn.node_id
    ORDER BY pn.surfaced + pn.opened DESC, pn.opened DESC, n.title NULLS LAST, n.id
    LIMIT %(limit)s
"""


def validate_tz(tz) -> str:
    """A zone name Python's tzdata knows, else ValueError. Shared with the skill usage
    window (kb_core.skills) so both dashboard pages accept exactly the same zones."""
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ValueError(f"unknown timezone: {tz!r}") from exc
    return tz


def _validate(days, bucket, tz, path) -> None:
    if days is not None and (not isinstance(days, int) or days < 1):
        raise ValueError("days must be a positive integer or None")
    if bucket not in BUCKETS:
        raise ValueError(f"bucket must be one of {', '.join(BUCKETS)}")
    validate_tz(tz)
    if path is not None and not _LTREE_PATH.match(path):
        raise ValueError(f"invalid folder path: {path!r}")


def retrieval_heatmap(conn: psycopg.Connection, *, days: "int | None", bucket: str,
                      tz: str, path: "str | None", limit: int,
                      include_evals: bool) -> dict:
    _validate(days, bucket, tz, path)
    params = {"days": days, "bucket": bucket, "tz": tz, "path": path,
              "limit": max(1, min(limit, MAX_LIMIT)), "include_evals": include_evals,
              "step": f"1 {bucket}", "entity_source": ENTITY_SOURCE,
              "entity_root": ENTITY_ROOT}
    try:
        buckets = [r["bucket"] for r in conn.execute(_BUCKETS_SQL, params).fetchall()]
        rows = conn.execute(_ROWS_SQL, params).fetchall()
    except psycopg.errors.InvalidParameterValue as exc:
        # A zone Python's tzdata knows but this server's does not.
        raise ValueError(f"unknown timezone: {tz!r}") from exc
    out = [{"id": str(r["id"]), "title": r["title"], "path": r["path"],
            "surfaced": r["surfaced"], "opened": r["opened"], "cells": r["cells"]}
           for r in rows]
    return {
        "window": {"days": days, "bucket": bucket, "tz": tz,
                   "start": buckets[0].isoformat(), "end": buckets[-1].isoformat()},
        "buckets": [b.isoformat() for b in buckets],
        "rows": out,
        "total_memories": rows[0]["total_memories"] if rows else 0,
        # Colour-scale domain, over the rows actually returned.
        "max_cell": max((c["surfaced"] + c["opened"] for r in out for c in r["cells"]),
                        default=0),
    }
