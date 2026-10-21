"""Read-only Postgres introspection for the dashboard's raw table browser.

Pure functions over a ``psycopg`` connection — they only ever SELECT, and callers run
them inside a ``SET TRANSACTION READ ONLY`` transaction. Table/schema names from the URL
are validated against ``pg_catalog`` and then quoted with ``psycopg.sql.Identifier``; a
raw name is **never** string-interpolated into SQL.
"""

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

import psycopg
from psycopg import sql

# Postgres' own plumbing — never offered as "your" tables.
_SYSTEM_SCHEMAS = ("pg_catalog", "information_schema")
# Ordinary + partitioned tables (no views/matviews/sequences): the literal stored rows.
_TABLE_RELKINDS = ("r", "p")
ROW_LIMIT_DEFAULT = 50
ROW_LIMIT_MAX = 500


def _jsonable(v: object) -> object:
    """Coerce a Postgres cell value to something ``json.dumps`` (and thus ``JSONResponse``)
    accepts, preserving an exact textual view. JSON-native scalars pass through; lists/dicts
    (arrays, json/jsonb) recurse; everything else (UUID, datetime, Decimal, ltree, pgvector,
    …) becomes its ``str``. Bytes render as Postgres' ``\\x``-hex so binary stays inspectable."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (bytes, bytearray, memoryview)):
        return "\\x" + bytes(v).hex()
    if isinstance(v, list):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (UUID, datetime, date, Decimal)):
        return str(v)
    return str(v)


def _display_type(data_type: str, udt_name: str) -> str:
    """A readable column type. information_schema reports composite/extension types as
    'USER-DEFINED' and arrays as 'ARRAY', so fall back to the underlying ``udt_name``
    (e.g. ``ltree``, ``vector``, ``_text`` -> ``text[]``)."""
    if data_type == "ARRAY":
        return f"{udt_name.lstrip('_')}[]"
    if data_type == "USER-DEFINED":
        return udt_name
    return data_type


def list_tables(conn: psycopg.Connection) -> list[dict]:
    """Every base table in non-system schemas, with its column count and an estimated row
    count (``pg_class.reltuples`` — fast; ``None`` when the table was never analyzed)."""
    rows = conn.execute(
        """
        SELECT n.nspname AS schema,
               c.relname AS name,
               c.reltuples::bigint AS est_rows,
               (SELECT count(*) FROM pg_attribute a
                  WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped)
                 AS columns
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE c.relkind = ANY(%s)
           AND n.nspname <> ALL(%s)
           AND n.nspname NOT LIKE 'pg_toast%%'
           AND n.nspname NOT LIKE 'pg_temp%%'
         ORDER BY n.nspname, c.relname
        """,
        (list(_TABLE_RELKINDS), list(_SYSTEM_SCHEMAS)),
    ).fetchall()
    return [
        {
            "schema": r["schema"],
            "name": r["name"],
            "columns": r["columns"],
            # reltuples is -1 until the table's first ANALYZE/VACUUM; surface "unknown".
            "est_rows": int(r["est_rows"]) if r["est_rows"] is not None and r["est_rows"] >= 0 else None,
        }
        for r in rows
    ]


def _relation(conn: psycopg.Connection, schema: str, name: str) -> tuple[int, list[str]] | None:
    """Resolve a (schema, name) to a base table's oid and primary-key column list, or
    ``None`` when it isn't a real table — the validation gate before any identifier is
    quoted into a query."""
    row = conn.execute(
        "SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relname = %s AND c.relkind = ANY(%s)",
        (schema, name, list(_TABLE_RELKINDS)),
    ).fetchone()
    if row is None:
        return None
    pk = [
        r["attname"]
        for r in conn.execute(
            "SELECT a.attname FROM pg_index i "
            "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
            "WHERE i.indrelid = %s AND i.indisprimary "
            "ORDER BY array_position(i.indkey, a.attnum)",
            (row["oid"],),
        ).fetchall()
    ]
    return row["oid"], pk


def describe_table(conn: psycopg.Connection, schema: str, name: str) -> dict | None:
    """Column metadata + primary-key columns for one table, or ``None`` if it doesn't exist."""
    rel = _relation(conn, schema, name)
    if rel is None:
        return None
    _oid, pk = rel
    cols = conn.execute(
        "SELECT column_name, data_type, udt_name, is_nullable, column_default "
        "FROM information_schema.columns WHERE table_schema = %s AND table_name = %s "
        "ORDER BY ordinal_position",
        (schema, name),
    ).fetchall()
    return {
        "columns": [
            {
                "name": c["column_name"],
                "type": _display_type(c["data_type"], c["udt_name"]),
                "nullable": c["is_nullable"] == "YES",
                "default": c["column_default"],
            }
            for c in cols
        ],
        "primary_key": pk,
    }


def read_rows(
    conn: psycopg.Connection, schema: str, name: str, *, limit: int, offset: int
) -> dict | None:
    """One page of a table's rows (as ``{column: value}`` dicts, values JSON-coerced) plus
    its exact total. Ordered by primary key for stable pagination, falling back to ``ctid``
    for a pkless table. Returns ``None`` if the table doesn't exist."""
    rel = _relation(conn, schema, name)
    if rel is None:
        return None
    _oid, pk = rel
    tbl = sql.Identifier(schema, name)
    order = (
        sql.SQL(", ").join(sql.Identifier(c) for c in pk) if pk else sql.SQL("ctid")
    )
    cur = conn.execute(
        sql.SQL("SELECT * FROM {tbl} ORDER BY {order} LIMIT %s OFFSET %s").format(
            tbl=tbl, order=order
        ),
        (limit, offset),
    )
    rows = [{k: _jsonable(v) for k, v in r.items()} for r in cur.fetchall()]
    total = conn.execute(
        sql.SQL("SELECT count(*) AS n FROM {tbl}").format(tbl=tbl)
    ).fetchone()["n"]
    return {"rows": rows, "total": int(total)}
